"""Scheduled task execution engine.

Connects APScheduler triggers with the existing BackgroundTaskManager
so that dynamically-created tasks run through the normal agent pipeline.
"""

from __future__ import annotations

import asyncio
import collections.abc
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from src.infra.channel.manager import get_channel_coordinator
from src.infra.chat.user_message_timestamp import format_user_message_with_timestamp
from src.infra.logging import get_logger
from src.infra.role.storage import RoleStorage
from src.infra.scheduler.locks import (
    acquire_task_lock,
    acquire_task_slot_lock,
    release_task_lock,
)
from src.infra.scheduler.runtime import get_runtime_scheduler
from src.infra.scheduler.storage import get_scheduled_task_storage
from src.infra.session.trace_storage import get_trace_storage
from src.infra.user.storage import UserStorage
from src.infra.utils.datetime import ensure_utc, utc_now
from src.kernel.config import settings
from src.kernel.schemas.scheduled_task import (
    RunStatus,
    ScheduledTask,
    ScheduledTaskStatus,
    TaskRunRecord,
    TriggerType,
)
from src.kernel.schemas.user import TokenPayload

logger = get_logger(__name__)

# 以下是等待 agent 执行完成时的轮询参数，以及从 trace 事件中识别"助手消息"用到的
# 事件类型/角色白名单（用于渠道投递时提取正文文本，过滤掉用户消息、工具调用等其他事件）
_POLL_INTERVAL = 2  # seconds between status checks when waiting for completion
_DEFAULT_TIMEOUT = 3600  # 60 minutes
_ASSISTANT_EVENT_TYPES = {
    "message",
    "assistant:message",
    "ai:message",
    "assistant",
    "ai",
    "content",
    "message:chunk",
    "summary",
}
_ASSISTANT_ROLES = {"assistant", "ai"}

# Track detached monitor tasks so they can be drained on shutdown.
_detached_monitor_tasks: set[asyncio.Task[None]] = set()


def _spawn_monitor(coro: collections.abc.Coroutine[None, None, None]) -> asyncio.Task[None]:
    """Spawn a detached fire-and-forget task with crash-safe error handling."""

    # 之所以要"脱离"执行：run() 提交完 agent 执行请求后应尽快返回，把 APScheduler 的调用栈让出来，
    # 真正等待执行结果、写运行记录、投递渠道、失败重试、释放锁等收尾工作放到这里异步完成，
    # 不阻塞下一次触发判断。
    async def _safe_run() -> None:
        try:
            await coro
        except Exception:
            logger.exception("[Runner] detached monitor task failed (lock will expire via TTL)")

    t = asyncio.create_task(_safe_run())
    # 用模块级 set 持有任务引用，防止因为没有其他地方引用而被 GC 提前回收；
    # 任务结束（无论成功失败）后通过 done_callback 自动从 set 中移除
    _detached_monitor_tasks.add(t)
    t.add_done_callback(_detached_monitor_tasks.discard)
    return t


async def drain_detached_monitors(timeout: float = 10.0) -> None:
    """Wait for in-flight monitor tasks to finish during shutdown."""
    # 应用关闭时调用：先等待一段时间让正在跑的收尾任务自然结束；
    # 超时后强制 cancel 剩下的，避免进程退出被这些任务无限期拖住
    # （即便被强制取消，对应的分布式任务锁最终也会通过 TTL 自动过期，不会永久锁死）
    tasks = list(_detached_monitor_tasks)
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=max(0.0, float(timeout)))
    for t in pending:
        t.cancel()
    if pending:
        logger.warning(
            "[Runner] cancelled %d detached monitor task(s) on shutdown",
            len(pending),
        )
        await asyncio.gather(*pending, return_exceptions=True)


@dataclass(frozen=True)
class _AttemptResult:
    # 一次任务执行尝试的结果：status 是分类后的运行状态（成功/超时/失败），
    # result 是原始的 agent/会话结果字典，error_message 仅在非成功时填充
    status: RunStatus
    result: dict[str, Any]
    error_message: str | None = None


async def _resolve_task_owner(user_id: str) -> TokenPayload | None:
    # 把 owner_id 解析为一个带有角色/权限信息的 TokenPayload，
    # 用于代表任务所有者去调用需要鉴权的接口（校验模型访问权限、解析 persona 请求等）；
    # 找不到用户时返回 None，交由调用方决定如何处理（通常直接判定本次执行失败）
    user = await UserStorage().get_by_id(user_id)
    if not user:
        return None

    roles = await RoleStorage().get_by_names(user.roles or [])
    permissions: set[str] = set()
    for role in roles:
        for permission in role.permissions:
            permissions.add(permission if isinstance(permission, str) else permission.value)

    return TokenPayload(
        sub=user.id,
        username=user.username,
        roles=[r.name for r in roles],
        permissions=sorted(permissions),
    )


class ScheduledTaskRunner:
    """Execute a scheduled task: acquire lock → create record → run agent → record result."""

    async def run(self, task_id: str, trigger_type: str = "cron") -> dict:
        """Entry point for scheduled / manual task execution.

        Submits the agent and returns immediately. Completion monitoring
        (result recording, delivery, retries, lock release) runs in a
        detached background task.
        """
        storage = get_scheduled_task_storage()
        task = await storage.get_task_for_execution(task_id)
        if task is None:
            logger.warning("[Runner] task %s not found, skipping", task_id)
            return {"skipped": True, "reason": "task_not_found"}

        # 双重保险：即使调度器判断这次触发合法，执行前再查一次库确认任务仍然启用且处于活跃状态
        # （触发排队等待期间，任务可能已被禁用/暂停/删除）
        if not task.enabled or task.status != "active":
            return {"skipped": True, "reason": "disabled"}

        now = utc_now()
        if trigger_type != "manual":
            # 第一层去重（仅针对自动触发 cron/interval/date，手动触发不受影响）：
            # 按这次触发对应的"时间槽"计算 slot_id——同一个真实触发时刻，无论被多少个进程的
            # APScheduler 几乎同时触发，算出的 slot_id 都应该相同。抢占这个短期分布式锁，
            # 只有抢到的那个进程才会继续往下执行，其余进程直接跳过（slot_contended），
            # 从而解决"多实例各自加载同一份任务定义，导致同一次触发被执行多次"的问题。
            slot = self._build_schedule_slot(task, trigger_type, now)
            if slot is not None:
                slot_id, slot_ttl, due_at = slot
                if due_at is not None and due_at > now:
                    # 提前触发（比如调度器抖动、时钟误差）：还没到目标时间，跳过等下次
                    return {
                        "skipped": True,
                        "reason": "not_due",
                        "next_due_at": due_at.isoformat(),
                    }
                slot_claimed = await acquire_task_slot_lock(task_id, slot_id, ttl=slot_ttl)
                if not slot_claimed:
                    return {"skipped": True, "reason": "slot_contended"}

        run_id = str(uuid.uuid4())

        # 1. Acquire distributed lock (multi-instance dedup)
        # 第二层去重：task 级别的锁以 task_id 为 key，TTL 覆盖"最大重试次数 x 单次超时时间"的
        # 整个窗口，确保同一个任务不会被并发执行第二次——无论新请求来自下一次自然触发、
        # 还是管理员手动点了"立即执行"，只要上一次（含其重试过程）还没结束就会被跳过。
        # run_id 作为锁的持有者标记写入，配合下面 finally 中的 release_task_lock 校验
        # 只有真正的持有者才能释放，避免误释放掉别的执行者持有的锁。
        max_attempts = max(1, int(task.max_retries or 0) + 1)
        lock_token = await acquire_task_lock(
            task_id, run_id, ttl=task.timeout_seconds * max_attempts
        )
        if lock_token is None:
            return {
                "skipped": True,
                "reason": "lock_contended",
                "run_id": run_id,
            }

        # 2. Create execution record
        base_session_id = self._build_session_id(task_id, run_id)
        record = TaskRunRecord.model_validate(
            {
                "_id": run_id,
                "task_id": task_id,
                "agent_id": task.agent_id,
                "trigger_type": trigger_type,
                "status": RunStatus.PENDING,
                "session_id": base_session_id,
                "input_snapshot": task.input_payload,
                "started_at": now,
                "created_at": now,
            }
        )
        await storage.create_run(record)

        # 3. Spawn detached monitor and return immediately so the APScheduler
        #    handler is not blocked by agent execution time.
        _spawn_monitor(
            self._monitor_and_finalize(
                task=task,
                run_id=run_id,
                base_session_id=base_session_id,
                lock_token=lock_token,
                max_attempts=max_attempts,
                trigger_type=trigger_type,
                started_at=now,
            )
        )
        logger.info(
            "[Runner] task=%s run=%s submitted (monitor running in background)",
            task_id,
            run_id,
        )
        return {"run_id": run_id, "status": "submitted"}

    async def _monitor_and_finalize(
        self,
        *,
        task: ScheduledTask,
        run_id: str,
        base_session_id: str,
        lock_token: str,
        max_attempts: int,
        trigger_type: str,
        started_at: datetime,
    ) -> None:
        """Detached background coroutine: wait for agent completion,
        record results, deliver to channel, retry on failure, release lock."""
        storage = get_scheduled_task_storage()
        try:
            final_attempt: _AttemptResult | None = None
            # 重试循环：每次尝试用一个新的 session_id（首次用 base，后续加 _retryN 后缀，
            # 避免复用同一个会话导致历史消息串联进重试请求里），执行 agent 并把结果分类为
            # 成功/超时/失败。
            for attempt in range(max_attempts):
                session_id = (
                    base_session_id if attempt == 0 else f"{base_session_id}_retry{attempt}"
                )
                await storage.update_run(
                    run_id,
                    {
                        "status": RunStatus.RUNNING,
                        "retry_count": attempt,
                        "session_id": session_id,
                    },
                )
                try:
                    result = await self._execute_agent(task, run_id, session_id, trigger_type)
                    final_attempt = self._classify_attempt_result(result)
                except Exception as exc:
                    # 执行过程中的任何异常都统一归类为 FAILED，避免异常直接冒泡打断整个收尾流程
                    final_attempt = _AttemptResult(
                        status=RunStatus.FAILED,
                        result={},
                        error_message=str(exc),
                    )

                if final_attempt.status == RunStatus.SUCCESS:
                    # 成功，不再重试
                    break
                if final_attempt.status != RunStatus.FAILED:
                    # 非 FAILED（例如 TIMEOUT）也不重试：超时通常意味着底层会话已被取消/中断，
                    # 立即重试很可能重复触发同样的超时，因此只有明确的 FAILED 状态才进入下面的重试判断
                    break
                if attempt + 1 < max_attempts:
                    logger.warning(
                        "[Runner] task=%s run=%s attempt=%d failed status=%s, retrying",
                        task.id,
                        run_id,
                        attempt,
                        final_attempt.status.value,
                    )

            # 循环至少跑一次（max_attempts >= 1），所以 final_attempt 一定已被赋值；
            # assert 仅用于满足类型检查，不代表运行期真的会失败
            assert final_attempt is not None
            # 只有最终判定为成功时，_deliver_success_result 内部才会真正发起投递；
            # 失败/超时会直接返回 None，不产生投递记录
            delivery_result = await self._deliver_success_result(task, final_attempt, run_id)
            if delivery_result is not None:
                final_attempt.result["delivery"] = delivery_result
            finished = utc_now()
            duration = int((finished - started_at).total_seconds() * 1000)
            update_payload: dict[str, Any] = {
                "status": final_attempt.status,
                "output_result": final_attempt.result,
                "session_id": final_attempt.result.get("session_id", base_session_id),
                "trace_id": final_attempt.result.get("trace_id"),
                "error_message": final_attempt.error_message,
                "finished_at": finished,
                "duration_ms": duration,
            }
            await storage.update_run(run_id, update_payload)
            await storage.update_task_run_stats(task.id, run_id, final_attempt.status)

            if final_attempt.status == RunStatus.SUCCESS:
                logger.info(
                    "[Runner] task=%s run=%s completed in %dms",
                    task.id,
                    run_id,
                    duration,
                )
            else:
                logger.warning(
                    "[Runner] task=%s run=%s finished status=%s after %dms: %s",
                    task.id,
                    run_id,
                    final_attempt.status.value,
                    duration,
                    final_attempt.error_message,
                )

        except Exception as exc:
            # 这里捕获的是重试循环之外的异常（比如写运行记录失败、投递前的意外错误），
            # 同样要把运行记录标记为失败，不能让一次未处理异常导致这条记录永远停留在 RUNNING 状态
            finished = utc_now()
            duration = int((finished - started_at).total_seconds() * 1000)
            await storage.update_run(
                run_id,
                {
                    "status": RunStatus.FAILED,
                    "error_message": str(exc),
                    "finished_at": finished,
                    "duration_ms": duration,
                },
            )
            await storage.update_task_run_stats(task.id, run_id, RunStatus.FAILED)
            logger.exception("[Runner] task=%s run=%s failed after %dms", task.id, run_id, duration)

        finally:
            # 无论成功/失败/异常，都必须释放任务锁，否则该任务会一直被锁住直到 TTL 自然过期；
            # DATE 类型的一次性任务执行完（无论成功失败）后直接暂停并从调度器摘除，
            # 因为它的触发时间点已经用掉，不会再有下一次自然触发
            await release_task_lock(task.id, lock_token)
            if task.trigger_type == TriggerType.DATE and trigger_type == TriggerType.DATE.value:
                await storage.update_task(
                    task.id,
                    {"status": ScheduledTaskStatus.PAUSED, "enabled": False},
                )
                get_runtime_scheduler().unregister_job(task.id)

    # ── Internal ───────────────────────────────────

    @staticmethod
    def _build_session_id(task_id: str, run_id: str) -> str:
        return f"sch_{task_id}_{run_id[:8]}"

    @staticmethod
    def _build_schedule_slot(
        task: ScheduledTask,
        trigger_type: str,
        now: datetime,
    ) -> tuple[str, int, datetime | None] | None:
        """Return a distributed schedule slot id, TTL, and optional due time."""
        # 计算这次触发对应的"时间槽"标识：同一个真实触发时刻（哪怕被多个进程的 APScheduler
        # 几乎同时触发），应该算出完全相同的 slot_id，从而让 run() 里的 acquire_task_slot_lock
        # 只允许其中一个进程继续往下执行。返回 None 表示不需要 slot 去重
        # （比如触发来源的类型与任务当前配置的类型不一致，交由后续常规校验处理）。
        if task.run_on_start and task.total_runs == 0:
            # "启动执行"：任务刚创建/刚被(重新)注册时触发的那一次。slot_id 里带上任务创建时间的
            # 时间戳，保证同一个任务只有一次"启动执行"会被去重命中；TTL 给 24 小时，
            # 因为这个 slot 只应该在任务生命周期最早期出现一次，不存在周期性重复的场景。
            anchor = task.created_at or now
            return f"run_on_start:{int(ensure_utc(anchor).timestamp())}", 86400, None

        if trigger_type == TriggerType.INTERVAL.value and task.trigger_type == TriggerType.INTERVAL:
            seconds = max(1, int(task.trigger_config.get("seconds", 1)))
            interval_anchor = task.last_run_at or task.created_at
            if interval_anchor is not None:
                # 优先按锚点（last_run_at 或 created_at）+ 间隔算出精确的目标触发时间 due_at
                # 作为 slot_id，与 _build_task_trigger 中 APScheduler 实际使用的锚点算法保持一致，
                # 确保两边算出同一个时间点；due_at 还没到时说明是提前触发，直接跳过等下次再来
                due_at = ensure_utc(interval_anchor) + timedelta(seconds=seconds)
                return f"interval:{int(due_at.timestamp())}", max(seconds * 2, 60), due_at
            # 理论上不该发生（没有任何锚点可用）的兜底路径：退化为按当前时间整除间隔得到一个
            # "时间桶"作为 slot_id，桶宽度等于间隔本身，精度不如锚点版本，但仍能在同一时间
            # 窗口内做到跨进程去重
            bucket = int(now.timestamp()) // seconds
            return f"interval:{bucket}", max(seconds * 2, 60), None

        if trigger_type == TriggerType.DATE.value and task.trigger_type == TriggerType.DATE:
            # 一次性任务：slot_id 直接用配置的目标时间戳（或退化为当前时间），
            # TTL 给 24 小时足够覆盖它触发那一刻的去重窗口
            run_date = task.trigger_config.get("run_date")
            if run_date:
                due_at = ensure_utc(datetime.fromisoformat(str(run_date)))
                return f"date:{int(due_at.timestamp())}", 86400, due_at
            return f"date:{int(now.timestamp())}", 86400, None

        if trigger_type == TriggerType.CRON.value and task.trigger_type == TriggerType.CRON:
            # cron 触发不像 interval 那样能提前推算出精确的"下一次"时间点，
            # 只能用触发发生的那一刻本身（精确到秒，去掉微秒）作为 slot_id；
            # TTL 24 小时同样只是防止长期占用，不代表这个 slot 会被重复使用
            slot_time = now.replace(microsecond=0)
            return f"cron:{int(slot_time.timestamp())}", 86400, None

        # trigger_type 与任务当前实际配置的类型不一致（比如触发信息有些过时/不匹配），
        # 不在这里做 slot 去重，交由上层继续正常流程——后续的任务级锁仍会兜底防止并发执行
        return None

    async def _execute_agent(
        self,
        task: ScheduledTask,
        run_id: str,
        session_id: str,
        trigger_type: str | None = None,
    ) -> dict:
        """Execute the agent via BackgroundTaskManager in a dedicated session."""
        # 把一条定时任务的静态配置（input_payload 里存的消息/附件/agent 选项/persona/team 等）
        # 转换为一次真正的 agent 会话请求，通过 TaskManager 提交执行，并等待其运行到终态。
        # 根据 settings.TASK_BACKEND 选择走 arq 分布式任务队列，还是走进程内的执行器注册表。
        from src.infra.session.manager import SessionManager
        from src.infra.task.concurrency import get_registered_executor
        from src.infra.task.manager import get_task_manager

        task_manager = get_task_manager()
        use_arq_backend = settings.TASK_BACKEND == "arq"

        display_message = task.input_payload.get("message", "")
        if not display_message and task.input_payload.get("prompt"):
            display_message = task.input_payload["prompt"]
        display_message = str(display_message or "")
        user_timezone = task.input_payload.get("user_timezone")
        raw_attachments = task.input_payload.get("attachments")
        attachments = (
            [dict(item) for item in raw_attachments if isinstance(item, dict)]
            if isinstance(raw_attachments, list)
            else None
        )
        message = format_user_message_with_timestamp(
            display_message,
            user_timezone if isinstance(user_timezone, str) else None,
        )
        agent_options = task.input_payload.get("agent_options")
        if isinstance(agent_options, dict):
            from src.api.routes.chat import validate_agent_model_access

            # 任务配置里显式指定了 agent_options（模型/参数覆盖）时，需要以任务所有者的身份
            # 重新走一遍模型访问权限校验，防止配置在创建之后被篡改从而绕过权限限制
            user = await _resolve_task_owner(task.owner_id)
            if user is None:
                raise RuntimeError(f"Scheduled task owner '{task.owner_id}' not found")
            await validate_agent_model_access(agent_options, user)
        else:
            agent_options = None

        persona_preset_id = task.input_payload.get("persona_preset_id")
        persona_preset_id = (
            persona_preset_id if isinstance(persona_preset_id, str) and persona_preset_id else None
        )
        team_id = task.input_payload.get("team_id")
        team_id = team_id if isinstance(team_id, str) and team_id else None
        if task.agent_id != "team":
            team_id = None
        else:
            persona_preset_id = None

        persona_system_prompt: str | None = None
        enabled_skills: list[str] | None = None
        persona_snapshot: dict | None = None
        if persona_preset_id:
            from src.api.routes.chat import resolve_persona_request
            from src.kernel.schemas.agent import AgentRequest

            # 同样以任务所有者身份解析人格预设，取出系统提示词/技能开关/快照信息；
            # 快照用于会话展示——即使预设后续被修改或删除，历史会话仍能显示创建时的样子
            user = await _resolve_task_owner(task.owner_id)
            if user is None:
                raise RuntimeError(f"Scheduled task owner '{task.owner_id}' not found")
            persona_request = AgentRequest(
                message=display_message,
                persona_preset_id=persona_preset_id,
            )
            await resolve_persona_request(persona_request, user)
            persona_system_prompt = persona_request.persona_system_prompt
            enabled_skills = persona_request.enabled_skills
            if persona_request.persona_snapshot:
                persona_snapshot = persona_request.persona_snapshot.model_dump()

        # 把定时任务的来源信息写进会话元数据：标记为定时任务产生的会话，且默认从常规对话列表
        # 隐藏（hidden_from_conversation_list），避免自动化产生的会话干扰用户的常规对话历史
        session_metadata = {
            "source": "scheduled_task",
            "scheduled_task_id": task.id,
            "scheduled_task_run_id": run_id,
            "scheduled_task_trigger_type": trigger_type or task.trigger_type.value,
            "hidden_from_conversation_list": True,
        }
        if persona_preset_id:
            session_metadata["persona_preset_id"] = persona_preset_id
        if persona_snapshot:
            session_metadata["persona_preset_name"] = persona_snapshot["name"]
            session_metadata["persona_snapshot"] = persona_snapshot
            if persona_snapshot.get("avatar"):
                session_metadata["persona_avatar"] = persona_snapshot["avatar"]
        if team_id:
            session_metadata["team_id"] = team_id

        # 两种提交方式（arq 分布式队列 / 进程内执行器）参数基本一致，只是入队方式不同，
        # 按配置二选一，本函数其余逻辑不需要关心底层具体是哪种任务执行后端
        if use_arq_backend:
            _, trace_id = await task_manager.submit_arq(
                session_id=session_id,
                agent_id=task.agent_id,
                message=message,
                user_id=task.owner_id,
                executor_key="agent_stream",
                run_id=run_id,
                disabled_tools=task.input_payload.get("disabled_tools"),
                agent_options=agent_options,
                attachments=attachments,
                project_id=None,
                enabled_skills=enabled_skills,
                persona_system_prompt=persona_system_prompt,
                team_id=team_id,
                session_name=f"{task.name}",
                display_message=display_message,
                recommendation_input=display_message,
                session_metadata=session_metadata,
                auto_mode=True,
                write_user_message_immediately=True,
            )
        else:
            executor_fn = get_registered_executor("agent_stream")
            if executor_fn is None:
                raise RuntimeError(
                    "agent_stream executor not registered — "
                    "ensure the chat router is loaded before scheduled tasks run"
                )
            _, trace_id = await task_manager.submit(
                session_id=session_id,
                agent_id=task.agent_id,
                message=message,
                user_id=task.owner_id,
                executor=executor_fn,
                run_id=run_id,
                disabled_tools=task.input_payload.get("disabled_tools"),
                agent_options=agent_options,
                attachments=attachments,
                project_id=None,
                enabled_skills=enabled_skills,
                persona_system_prompt=persona_system_prompt,
                team_id=team_id,
                session_name=f"{task.name}",
                display_message=display_message,
                recommendation_input=display_message,
                session_metadata=session_metadata,
                auto_mode=True,
                write_user_message_immediately=True,
            )
        await SessionManager().update_session_metadata(
            session_id,
            session_metadata,
        )

        # 提交只是把任务塞进执行管线（arq 队列或进程内执行器），这里仍需轮询等待它真正跑完
        result = await self._wait_for_completion(
            task_manager, session_id, run_id, task.owner_id, task.timeout_seconds
        )
        result["session_id"] = session_id
        result["trace_id"] = trace_id
        return result

    async def _wait_for_completion(
        self,
        task_manager: Any,
        session_id: str,
        run_id: str,
        user_id: str,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
    ) -> dict:
        """Poll task status until completion or timeout."""
        from src.infra.task.status import TaskStatus

        # 用 monotonic 时钟计时，不受系统时间被外部调整（如 NTP 校时）影响
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            status = await task_manager.get_run_status(session_id, run_id)
            if status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.EXPIRED,
            ):
                return {
                    "session_status": (status.value if hasattr(status, "value") else str(status))
                }
            await asyncio.sleep(_POLL_INTERVAL)

        # 轮询到超时仍未结束：主动尝试取消底层任务，避免它继续在后台占用资源；
        # 取消失败也不影响这里仍然返回"超时"结果
        try:
            await task_manager.cancel_run(run_id, user_id=user_id)
        except Exception as exc:
            logger.warning(
                "[Runner] failed to cancel timed-out task run=%s session=%s: %s",
                run_id,
                session_id,
                exc,
            )
        return {"session_status": "timeout"}

    @staticmethod
    def _classify_attempt_result(result: dict[str, Any]) -> _AttemptResult:
        """Map BackgroundTaskManager terminal state into scheduled-task status."""
        # 把底层会话/任务管理器的终止状态字符串，映射为定时任务侧更明确的 RunStatus；
        # 未识别的状态统一归为 FAILED 并记录原始状态值，避免出现"既不成功也不失败"的模糊中间态
        session_status = str(result.get("session_status") or "").lower()
        if session_status == "completed":
            return _AttemptResult(status=RunStatus.SUCCESS, result=result)
        if session_status == "timeout":
            return _AttemptResult(
                status=RunStatus.TIMEOUT,
                result=result,
                error_message="Scheduled task execution timed out",
            )
        if session_status in {"failed", "cancelled", "expired"}:
            return _AttemptResult(
                status=RunStatus.FAILED,
                result=result,
                error_message=f"Agent run ended with status: {session_status}",
            )
        return _AttemptResult(
            status=RunStatus.FAILED,
            result=result,
            error_message=f"Unexpected agent run status: {session_status or 'unknown'}",
        )

    async def _deliver_success_result(
        self,
        task: ScheduledTask,
        attempt: _AttemptResult,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Send a successful scheduled-task result back to the configured channel."""
        delivery = task.delivery
        # 只有明确配置了投递渠道、开启了"成功时发送"，且这次尝试确实成功，才需要投递；
        # 其余情况直接跳过，不产生任何投递记录
        if (
            attempt.status != RunStatus.SUCCESS
            or delivery is None
            or not delivery.enabled
            or not delivery.send_on_success
        ):
            return None

        session_id = attempt.result.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            # 拿不到 session_id 就没法去查 trace 事件提取正文，只能跳过并记录原因
            return {
                "status": "skipped",
                "reason": "missing_session_id",
                "channel_type": delivery.channel_type.value,
                "chat_id": delivery.chat_id,
                "channel_instance_id": delivery.channel_instance_id,
            }

        # 从 trace 事件中还原出助手最终回复的文本内容
        events = await get_trace_storage().get_run_events(session_id, run_id)
        content = self._extract_channel_delivery_text(events, delivery.max_content_chars)
        if not content:
            # 提取不到有效文本（比如 agent 没有产生任何可展示内容）也跳过，不发送空消息
            return {
                "status": "skipped",
                "reason": "empty_result",
                "channel_type": delivery.channel_type.value,
                "chat_id": delivery.chat_id,
                "channel_instance_id": delivery.channel_instance_id,
            }

        try:
            sent = await get_channel_coordinator().send_message(
                task.owner_id,
                delivery.channel_type,
                delivery.chat_id,
                content,
                instance_id=delivery.channel_instance_id,
            )
        except Exception as exc:
            # 渠道发送失败不应该影响任务本身已经"成功"的判定，只在投递结果里记录失败原因
            logger.warning(
                "[Runner] failed to deliver task=%s result to channel=%s chat=%s: %s",
                task.id,
                delivery.channel_type.value,
                delivery.chat_id,
                exc,
            )
            return {
                "status": "failed",
                "error": str(exc),
                "channel_type": delivery.channel_type.value,
                "chat_id": delivery.chat_id,
                "channel_instance_id": delivery.channel_instance_id,
            }

        return {
            "status": "sent" if sent else "failed",
            **({} if sent else {"error": "channel_send_returned_false"}),
            "channel_type": delivery.channel_type.value,
            "chat_id": delivery.chat_id,
            "channel_instance_id": delivery.channel_instance_id,
        }

    @staticmethod
    def _extract_channel_delivery_text(
        events: list[dict[str, Any]],
        max_content_chars: int,
    ) -> str:
        """Extract assistant text from trace events for channel delivery."""
        parts: list[str] = []
        chunk_parts: list[str] = []

        # message:chunk 类型的事件是流式回复的分片，需要先攒起来，
        # 在遇到下一个非 chunk 事件（或遍历结束）时统一拼接 flush 到 parts 里，
        # 避免把分片当成一条条独立消息处理
        def flush_chunks() -> None:
            if not chunk_parts:
                return
            chunk_text = "".join(chunk_parts).strip()
            if chunk_text:
                parts.append(chunk_text)
            chunk_parts.clear()

        # 按时间顺序遍历所有 trace 事件，只保留"助手侧"的文本内容，
        # 过滤掉用户消息、工具调用等其它事件
        for event in events:
            event_type = str(event.get("event_type") or "")
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            role = str(data.get("role") or "").lower()
            if role in {"user", "human"}:
                # 跳过用户自己的消息
                continue
            if event_type == "message" and role not in _ASSISTANT_ROLES:
                # "message" 类型事件如果角色不是助手（比如系统消息），也跳过
                continue
            if event_type not in _ASSISTANT_EVENT_TYPES and role not in _ASSISTANT_ROLES:
                # 既不是已知的助手事件类型，角色也不是助手，说明不是我们关心的内容
                continue

            content = data.get("content")
            if content is None:
                content = data.get("message")
            if not isinstance(content, str) or not content.strip():
                continue

            if event_type == "message:chunk":
                # 流式分片先收集
                chunk_parts.append(content)
            else:
                # 遇到完整内容（比如整条 message/summary）时，先把之前攒的分片 flush 掉，
                # 再追加这条完整内容，保证输出顺序与事件发生顺序一致
                flush_chunks()
                parts.append(content.strip())

        # 遍历结束后别忘了把最后一批还没 flush 的分片也收尾
        flush_chunks()
        text = "\n".join(parts).strip()
        if len(text) > max_content_chars:
            # 投递渠道（如 IM）往往有消息长度限制，超长时做截断
            return text[:max_content_chars].rstrip()
        return text


# ── Singleton ──────────────────────────────────────

# 进程级单例：一个进程内只需要一个 ScheduledTaskRunner 实例
_runner: Optional[ScheduledTaskRunner] = None


def get_scheduled_task_runner() -> ScheduledTaskRunner:
    global _runner
    if _runner is None:
        _runner = ScheduledTaskRunner()
    return _runner

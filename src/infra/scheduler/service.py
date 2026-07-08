"""Business logic for scheduled task CRUD and scheduler coordination.

This service is the bridge between the API layer and the lower-level
storage, runner, and scheduler components.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.infra.logging import get_logger
from src.infra.scheduler.runner import get_scheduled_task_runner
from src.infra.scheduler.runtime import ScheduledJob, get_runtime_scheduler
from src.infra.scheduler.storage import get_scheduled_task_storage
from src.infra.session.storage import SessionStorage
from src.infra.utils.datetime import ensure_utc, utc_now
from src.kernel.schemas.scheduled_task import (
    CronTriggerConfig,
    DateTriggerConfig,
    IntervalTriggerConfig,
    ScheduledTask,
    ScheduledTaskCreate,
    ScheduledTaskResponse,
    ScheduledTaskStatus,
    ScheduledTaskUpdate,
    TaskRunResponse,
    TriggerType,
)

logger = get_logger(__name__)

# 进程级缓存：记录每个任务"上一次注册进 APScheduler 时"的配置签名（见 _scheduler_signature）。
# _register_to_scheduler 每次都会先比较签名，只有签名变化（或任务尚未注册过）才真正重新构建
# 触发器并调用 RuntimeScheduler.register_job，避免任务列表刷新时对所有未变化的任务做无谓的 reschedule。
_managed_task_signatures: dict[str, str] = {}


# 把字符串时区名转换为 ZoneInfo；缺省或空字符串按 UTC 处理，非法时区名转换为更易读的 ValueError
def _coerce_timezone(timezone_name: str | None) -> ZoneInfo:
    name = (timezone_name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid timezone: {name}") from exc


# DATE 触发器存储的时间如果没有时区信息，先按任务配置的时区补齐，再统一转换为 UTC，
# 以便与 utc_now() 直接比较
def _ensure_utc_in_timezone(dt, timezone_name: str | None):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_coerce_timezone(timezone_name))
    return ensure_utc(dt)


def clear_managed_task_signatures() -> None:
    """Release in-process scheduler registration signatures."""
    # 用于测试之间重置状态，或需要强制下一次 _register_to_scheduler 都重新注册全部任务时调用
    _managed_task_signatures.clear()


class ScheduledTaskService:
    """CRUD + scheduler orchestration for dynamic scheduled tasks."""

    def __init__(self) -> None:
        # 与 storage.get_active_tasks_marker() 配合的"变更检测缓存"：
        # 只有 marker 发生变化时，load_persisted_tasks 才会真正重新拉取并对比任务列表，
        # 否则直接返回上次记录的活跃任务数量，避免频繁调用时重复做全量同步
        self._active_tasks_marker: int | None = None
        self._active_task_count = 0

    # ── CRUD ───────────────────────────────────────

    async def create_task(
        self,
        request: ScheduledTaskCreate,
        owner_id: str,
    ) -> ScheduledTask:
        """Validate, persist, and register a new scheduled task."""
        # Validate trigger config
        # 先做一次触发器校验（不落库）：确保非法的 trigger_config 在写库之前就被拒绝
        self._build_trigger(request.trigger_type, request.trigger_config, request.timezone)

        now = utc_now()
        task_id = str(uuid4())
        task = ScheduledTask.model_validate(
            {
                "_id": task_id,
                "name": request.name,
                "description": request.description,
                "agent_id": request.agent_id,
                "trigger_type": request.trigger_type,
                "trigger_config": request.trigger_config,
                "timezone": request.timezone,
                "input_payload": request.input_payload,
                "status": ScheduledTaskStatus.ACTIVE,
                "enabled": request.enabled,
                "run_on_start": False
                if request.trigger_type == TriggerType.DATE
                else request.run_on_start,
                "max_retries": request.max_retries,
                "timeout_seconds": request.timeout_seconds,
                "owner_id": owner_id,
                "source_session_id": request.source_session_id,
                "source_run_id": request.source_run_id,
                "created_by": request.created_by,
                "delivery": request.delivery,
                "created_at": now,
                "updated_at": now,
            }
        )

        storage = get_scheduled_task_storage()
        await storage.create_task(task)
        # 写库成功后才注册进本进程的 APScheduler，保证"任务已持久化"在"任务被调度"之前完成；
        # honor_run_on_start=True：只有首次创建时才尊重 run_on_start（立即执行一次），
        # 后续更新/重新加载时不应该重复触发这次"立即执行"
        self._register_to_scheduler(task, honor_run_on_start=True)

        logger.info(
            "[Service] created task %s agent=%s trigger=%s",
            task_id,
            request.agent_id,
            request.trigger_type.value,
        )
        return task

    async def update_task(
        self, task_id: str, request: ScheduledTaskUpdate
    ) -> Optional[ScheduledTask]:
        """Update task fields and refresh the scheduler registration."""
        storage = get_scheduled_task_storage()
        task = await storage.get_task(task_id)
        if task is None:
            return None

        updates: dict[str, Any] = request.model_dump(exclude_unset=True)

        # Validate trigger changes as one atomic pair. This also supports changing
        # trigger_type and trigger_config in a single update request.
        # trigger 相关的三个字段（类型/配置/时区）作为一个整体校验：允许同一次请求里
        # 同时切换类型和配置，用"更新值或原值"拼出完整组合后再校验，任一项非法则整体拒绝
        if "trigger_type" in updates or "trigger_config" in updates or "timezone" in updates:
            trigger_type = updates.get("trigger_type", task.trigger_type)
            trigger_config = updates.get("trigger_config", task.trigger_config)
            timezone_name = updates.get("timezone", task.timezone)
            self._build_trigger(trigger_type, trigger_config, timezone_name)
            if trigger_type == TriggerType.DATE:
                # DATE 触发器只会触发一次，"启动时立即执行"这个语义没有意义，强制关闭
                updates["run_on_start"] = False

        if not updates:
            # 没有任何实际变更字段，直接返回原任务，不必打扰调度器
            return task

        await storage.update_task(task_id, updates)
        updated_task = await storage.get_task(task_id)
        if updated_task is None:
            return None

        # Refresh scheduler registration
        # 更新后仍然是"启用且活跃"才重新注册（配置是否真的变化由 _register_to_scheduler 内部按签名判断）；
        # 否则说明任务已被禁用/暂停，应该从调度器里摘掉
        if updated_task.enabled and updated_task.status == ScheduledTaskStatus.ACTIVE:
            self._register_to_scheduler(updated_task)
        else:
            self._unregister_managed_task(task_id)

        return updated_task

    async def pause_task(self, task_id: str) -> Optional[ScheduledTask]:
        """Pause a task — remove from scheduler but keep the DB record."""
        storage = get_scheduled_task_storage()
        task = await storage.get_task(task_id)
        if task is None:
            return None
        # 暂停只是把 DB 状态改为 PAUSED 且 enabled=False，不删除任务定义，之后可通过 resume 恢复
        await storage.update_task(task_id, {"status": ScheduledTaskStatus.PAUSED, "enabled": False})
        self._unregister_managed_task(task_id)
        logger.info("[Service] paused task %s", task_id)
        return await storage.get_task(task_id)

    async def resume_task(self, task_id: str) -> Optional[ScheduledTask]:
        """Resume a paused task — re-register with the scheduler."""
        storage = get_scheduled_task_storage()
        task = await storage.get_task(task_id)
        if task is None:
            return None
        await storage.update_task(task_id, {"status": ScheduledTaskStatus.ACTIVE, "enabled": True})
        updated = await storage.get_task(task_id)
        if updated is not None:
            # 重新注册会按最新状态重新计算触发器；例如 interval 任务会以当前 last_run_at/created_at
            # 重新锚定下一次触发点，而不是从暂停前的旧计划继续
            self._register_to_scheduler(updated)
        logger.info("[Service] resumed task %s", task_id)
        return updated

    async def delete_task(self, task_id: str) -> bool:
        """Physically delete a task."""
        # 先从调度器摘除再删库，避免出现"数据库已删除但 APScheduler 里还残留一个指向
        # 已删除任务的 job"这种悬空状态
        self._unregister_managed_task(task_id)
        storage = get_scheduled_task_storage()
        deleted = await storage.delete_task(task_id)
        if deleted:
            logger.info("[Service] deleted task %s", task_id)
        return deleted

    async def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        return await get_scheduled_task_storage().get_task(task_id)

    async def list_tasks(
        self,
        owner_id: Optional[str] = None,
        status: Optional[ScheduledTaskStatus] = None,
    ) -> list[ScheduledTask]:
        return await get_scheduled_task_storage().list_tasks(owner_id=owner_id, status=status)

    async def list_tasks_paginated(
        self,
        owner_id: str,
        status: Optional[ScheduledTaskStatus] = None,
        source_session_id: Optional[str] = None,
        created_by: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[ScheduledTaskResponse], int]:
        """List tasks with pagination, scoped by owner_id."""
        storage = get_scheduled_task_storage()
        tasks, total = await storage.list_tasks_paginated(
            owner_id=owner_id,
            status=status,
            source_session_id=source_session_id,
            created_by=created_by,
            skip=skip,
            limit=limit,
        )
        # 顺带查出每个任务关联会话的未读消息数，用于任务列表页展示"未读徽标"
        unread_counts = await SessionStorage().get_unread_counts_for_scheduled_tasks(
            user_id=owner_id,
            scheduled_task_ids=[task.id for task in tasks],
        )
        responses = [self.to_response(t, unread_count=unread_counts.get(t.id, 0)) for t in tasks]
        return responses, total

    async def get_task_response(self, task: ScheduledTask) -> ScheduledTaskResponse:
        """Convert a task to an API response with unread session totals."""
        unread_counts = await SessionStorage().get_unread_counts_for_scheduled_tasks(
            user_id=task.owner_id,
            scheduled_task_ids=[task.id],
        )
        return self.to_response(task, unread_count=unread_counts.get(task.id, 0))

    # ── Execution ──────────────────────────────────

    async def run_task_now(self, task_id: str) -> dict:
        """Manually trigger a task execution."""
        # trigger_type="manual" 会让 runner 跳过仅针对自动触发的检查（比如"是否命中分布式调度 slot"），
        # 直接尝试获取任务锁并执行一次
        runner = get_scheduled_task_runner()
        return await runner.run(task_id, trigger_type="manual")

    async def get_task_runs(
        self, task_id: str, limit: int = 20, offset: int = 0
    ) -> tuple[list[TaskRunResponse], int]:
        storage = get_scheduled_task_storage()
        records, total = await storage.list_runs(task_id, limit, offset)
        responses = [
            TaskRunResponse(
                id=r.id,
                task_id=r.task_id,
                agent_id=r.agent_id,
                trigger_type=r.trigger_type,
                status=r.status,
                session_id=r.session_id,
                trace_id=r.trace_id,
                input_snapshot=r.input_snapshot,
                output_result=r.output_result,
                error_message=r.error_message,
                retry_count=r.retry_count,
                started_at=r.started_at,
                finished_at=r.finished_at,
                duration_ms=r.duration_ms,
                created_at=r.created_at,
            )
            for r in records
        ]
        return responses, total

    # ── Startup ────────────────────────────────────

    async def load_persisted_tasks(self) -> int:
        """Load all active tasks from DB and register them with the scheduler.

        Called once during process startup.
        """
        # 该方法具备幂等增量刷新能力：通过比较 O(1) 的版本号 marker 判断任务定义是否发生变化，
        # 因此除了启动时调用外，也适合被上层以低频轮询的方式周期性调用，
        # 用于感知其他实例对任务列表的修改，而不用只依赖进程重启才能同步。
        storage = get_scheduled_task_storage()
        marker = await storage.get_active_tasks_marker()
        if self._active_tasks_marker == marker:
            # 版本号未变，说明自上次加载以来没有任何任务被创建/更新/删除，
            # 直接复用缓存的计数，跳过整轮任务列表拉取与调度器同步
            logger.debug("[Service] scheduled tasks unchanged; skipped scheduler reload")
            return self._active_task_count

        tasks = await storage.list_active_tasks()
        now = utc_now()
        active_task_ids: set[str] = set()
        for task in tasks:
            if self._is_expired_date_task(task, now):
                # 一次性的 DATE 任务如果到加载时刻已经过了目标执行时间（例如进程重启前错过了），
                # 不应该再被注册进调度器，而是直接标记为暂停，避免立即触发一次"迟到执行"
                await storage.update_task(
                    task.id,
                    {"status": ScheduledTaskStatus.PAUSED, "enabled": False},
                )
                self._unregister_managed_task(task.id)
                continue
            active_task_ids.add(task.id)
            self._register_to_scheduler(task)

        # 把本次不再处于"活跃"集合、但之前注册过的任务从调度器摘除
        # （对应任务被暂停/删除/禁用等场景）
        for task_id in set(_managed_task_signatures) - active_task_ids:
            self._unregister_managed_task(task_id)

        self._active_tasks_marker = marker
        self._active_task_count = len(active_task_ids)
        logger.info("[Service] loaded %d persisted tasks into scheduler", len(tasks))
        return len(tasks)

    # ── Conversion helpers ─────────────────────────

    @staticmethod
    def to_response(
        task: ScheduledTask,
        unread_count: int = 0,
    ) -> ScheduledTaskResponse:
        """Convert a ScheduledTask model to an API response."""
        return ScheduledTaskResponse(
            id=task.id,
            name=task.name,
            description=task.description,
            agent_id=task.agent_id,
            trigger_type=task.trigger_type,
            trigger_config=task.trigger_config,
            timezone=task.timezone,
            input_payload=task.input_payload,
            status=task.status,
            enabled=task.enabled,
            run_on_start=task.run_on_start,
            max_retries=task.max_retries,
            timeout_seconds=task.timeout_seconds,
            owner_id=task.owner_id,
            source_session_id=task.source_session_id,
            source_run_id=task.source_run_id,
            created_by=task.created_by,
            delivery=task.delivery,
            last_run_at=task.last_run_at,
            last_run_status=task.last_run_status,
            last_run_id=task.last_run_id,
            total_runs=task.total_runs,
            unread_count=unread_count,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )

    # ── Internal ───────────────────────────────────

    def _register_to_scheduler(
        self,
        task: ScheduledTask,
        *,
        honor_run_on_start: bool = False,
    ) -> None:
        """Register a persisted task with the in-process APScheduler."""
        # 先计算这次任务配置对应的签名
        signature = self._scheduler_signature(task)
        scheduler = get_runtime_scheduler()
        # 签名未变且调度器里确实还有这个 job，说明配置没有实质变化，跳过重复注册，
        # 避免不必要的 reschedule（尤其是 load_persisted_tasks 每次刷新都会遍历所有任务）
        if _managed_task_signatures.get(task.id) == signature and scheduler.has_job(task.id):
            return

        trigger = self._build_task_trigger(task)
        runner = get_scheduled_task_runner()
        task_id = task.id
        trigger_type_value = task.trigger_type.value

        # Capture task.id via default arg to avoid late-binding issues
        # 触发时统一调用 runner.run(task_id, trigger_type=...)；具体的加锁/执行 agent/记录结果
        # 等真正的执行逻辑都在 ScheduledTaskRunner 里，这里只负责把"触发"接到"执行"上
        job = ScheduledJob(
            id=task_id,
            name=task.name,
            trigger=trigger,
            handler=lambda: runner.run(task_id, trigger_type=trigger_type_value),
            enabled=task.enabled,
            run_on_start=bool(honor_run_on_start and task.run_on_start),
            max_instances=1,
            coalesce=True,
        )
        # register_job 对同 id 的任务是覆盖式替换（底层 APScheduler add_job replace_existing=True）
        scheduler.register_job(job)
        # 更新签名缓存，下次配置未变时即可命中上面的"跳过重复注册"分支
        _managed_task_signatures[task_id] = signature

    @staticmethod
    def _unregister_managed_task(task_id: str) -> None:
        # 从调度器摘除任务，并同步清理本地签名缓存，两者需要保持一致
        get_runtime_scheduler().unregister_job(task_id)
        _managed_task_signatures.pop(task_id, None)

    @staticmethod
    def _scheduler_signature(task: ScheduledTask) -> str:
        # 把"决定任务下次何时/如何触发"的关键字段序列化为一个签名字符串
        # （不包含与调度无关的字段，比如 owner_id、input_payload），签名相同即认为调度配置未变化。
        # 之所以包含 last_run_at：interval 触发器会以 last_run_at 为锚点重新计算下一次触发时间
        # （见 _build_task_trigger），因此 last_run_at 变化也应当触发一次 re-register。
        return json.dumps(
            {
                "trigger_type": task.trigger_type.value,
                "trigger_config": task.trigger_config,
                "timezone": task.timezone,
                "enabled": task.enabled,
                "status": task.status.value,
                "run_on_start": task.run_on_start,
                "name": task.name,
                "last_run_at": task.last_run_at,
                "created_at": task.created_at,
            },
            default=str,
            sort_keys=True,
        )

    @staticmethod
    def _build_task_trigger(task: ScheduledTask) -> BaseTrigger:
        """Build a trigger for a concrete persisted task.

        Interval tasks are anchored to persisted timestamps so multiple
        processes compute the same future fire times instead of drifting from
        each process startup time.
        """
        if task.trigger_type == TriggerType.INTERVAL:
            interval_cfg = IntervalTriggerConfig(**task.trigger_config)
            # 锚点选取：优先用上次真正运行的时间，没有则用任务创建时间，
            # 保证任务第一次被注册时也有一个确定的起点可以计算
            anchor = task.last_run_at or task.created_at
            # 下一次触发时间 = 锚点 + 一个完整间隔，而不是"现在 + 间隔"：
            # 这样无论调度器在什么时刻重新加载这个任务（比如进程重启、多实例各自加载），
            # 算出来的下一次触发时间都是一致的，不会因为加载时刻不同而产生漂移
            start_date = (
                ensure_utc(anchor) + timedelta(seconds=interval_cfg.seconds)
                if anchor is not None
                else None
            )
            return IntervalTrigger(
                seconds=interval_cfg.seconds,
                start_date=start_date,
                timezone=_coerce_timezone(task.timezone),
            )
        return ScheduledTaskService._build_trigger(
            task.trigger_type,
            task.trigger_config,
            task.timezone,
        )

    @staticmethod
    def _build_trigger(
        trigger_type: TriggerType,
        config: dict,
        timezone_name: str | None = "UTC",
    ) -> BaseTrigger:
        """Build an APScheduler trigger from the stored config dict."""
        tz = _coerce_timezone(timezone_name)
        if trigger_type == TriggerType.INTERVAL:
            interval_cfg = IntervalTriggerConfig(**config)
            return IntervalTrigger(seconds=interval_cfg.seconds, timezone=tz)
        if trigger_type == TriggerType.CRON:
            cron_cfg = CronTriggerConfig(**config)
            return CronTrigger(
                year=cron_cfg.year,
                month=cron_cfg.month,
                day=cron_cfg.day,
                week=cron_cfg.week,
                day_of_week=cron_cfg.day_of_week,
                hour=cron_cfg.hour,
                minute=cron_cfg.minute,
                second=cron_cfg.second,
                timezone=tz,
            )
        if trigger_type == TriggerType.DATE:
            date_cfg = DateTriggerConfig(**config)
            run_date = _ensure_utc_in_timezone(date_cfg.run_date, timezone_name)
            # 一次性任务的执行时间必须在未来，否则直接拒绝创建/更新，
            # 避免出现一个"配置的时间已经过去、永远不会真正触发"的任务
            if run_date <= utc_now():
                raise ValueError("date trigger run_date must be in the future")
            return DateTrigger(run_date=run_date, timezone="UTC")
        raise ValueError(f"Unsupported trigger type: {trigger_type}")

    @staticmethod
    def _is_expired_date_task(task: ScheduledTask, now=None) -> bool:
        # 判断一个 DATE 类型任务的目标执行时间是否已经过去；用于启动/刷新时把这类任务自动置为暂停，
        # 而不是让 APScheduler 一加载就立即触发一次"迟到执行"。
        # 非 DATE 类型、或触发配置解析失败时都视为"未过期"。
        if task.trigger_type != TriggerType.DATE:
            return False
        try:
            cfg = DateTriggerConfig(**task.trigger_config)
        except Exception:
            return False
        return _ensure_utc_in_timezone(cfg.run_date, task.timezone) <= (now or utc_now())

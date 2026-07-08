"""arq worker 侧的任务入口。

当 TASK_BACKEND=arq 时，独立 worker 进程从 arq 队列取到只带 run_id 的 job，在此
凭 run_id 回读 Redis 里的 payload 还原任务上下文，再复用 TaskExecutor.run_task
执行。文件末尾的 WorkerSettings 供 arq 命令行/嵌入式 runtime 加载。
"""

from __future__ import annotations

import asyncio
from importlib import import_module
from typing import Any

from src.infra.distributed_validation import validate_distributed_runtime_settings
from src.infra.logging import get_logger
from src.kernel.config import settings

from .arq_payloads import TaskArqPayloadStore
from .concurrency import get_concurrency_limiter, get_registered_executor
from .exceptions import TaskInterruptedError
from .manager import get_task_manager
from .status import TaskStatus

logger = get_logger(__name__)


# worker 启动钩子：接受任务前先校验分布式运行时配置是否自洽（如后端/队列设置）。
async def worker_startup(ctx: dict[str, Any]) -> None:
    """Validate worker runtime configuration before accepting jobs."""
    del ctx
    validate_distributed_runtime_settings(settings)


# 按 key 解析执行函数。worker 进程可能尚未 import 注册执行器的模块，因此对内置的
# "agent_stream" 做一次按需 import（加载 chat 路由触发注册）后再取一次。
def _resolve_executor(executor_key: str) -> Any:
    executor_fn = get_registered_executor(executor_key)
    if executor_fn is not None:
        return executor_fn

    if executor_key == "agent_stream":
        import_module("src.api.routes.chat")
        return get_registered_executor(executor_key)

    return None


# 判断某 run 是否为「用户主动取消」：会话已切到别的 run 则不算；否则看
# error_code=cancelled 或状态为 CANCELLED。用于 worker 在被 abort 时区分
# 「用户取消（丢弃 payload）」与「服务关闭（保留以便恢复）」。
async def _is_user_cancelled_run(task_manager: Any, session_id: str, run_id: str) -> bool:
    storage = getattr(task_manager, "storage", None)
    if storage is None:
        return False

    try:
        session = await storage.get_by_session_id(session_id)
    except Exception as e:
        logger.warning("Failed to inspect cancelled run state: %s", e)
        return False

    metadata = getattr(session, "metadata", None) or {}
    current_run_id = metadata.get("current_run_id")
    if current_run_id and str(current_run_id) != str(run_id):
        return False

    return (
        metadata.get("task_error_code") == "cancelled"
        or metadata.get("task_status") == TaskStatus.CANCELLED.value
    )


# 释放并发槽位（worker 侧封装）：无 user_id 则跳过，失败仅告警。
async def _release_concurrency_slot(user_id: str | None, run_id: str, *, dequeue: bool) -> None:
    if not user_id:
        return

    try:
        limiter = get_concurrency_limiter()
        await limiter.release(user_id, run_id, dequeue=dequeue)
    except Exception as e:
        logger.warning("Failed to release arq concurrency slot: %s", e)


# arq 任务主入口：凭 run_id 回读 payload 并执行。
# payload 缺失（如已过期/被清理）直接返回；执行函数无法解析则落 FAILED 并清理。
# 执行完成后按不同结局处理 payload 与并发槽位（见函数体内各 except 分支）。
async def run_agent_task(ctx: dict[str, Any], run_id: str) -> None:
    """Run a previously persisted LambChat task from an arq worker."""
    payload_store: TaskArqPayloadStore = ctx.get("payload_store") or TaskArqPayloadStore()
    payload = await payload_store.load(run_id)
    if payload is None:
        logger.warning("Missing arq task payload for run_id=%s", run_id)
        return

    task_manager = get_task_manager()
    task_executor = task_manager._ensure_executor()

    executor_key = str(payload["executor_key"])
    executor_fn = _resolve_executor(executor_key)
    if executor_fn is None:
        error_message = f"No executor registered for key '{executor_key}'"
        logger.error("%s: run_id=%s", error_message, run_id)
        await task_executor._update_session_status(
            payload["session_id"],
            TaskStatus.FAILED,
            error_message,
            run_id=run_id,
        )
        await payload_store.delete(run_id)
        await _release_concurrency_slot(payload.get("user_id"), run_id, dequeue=True)
        return

    task_manager._run_info[run_id] = {
        "session_id": payload["session_id"],
        "trace_id": payload.get("trace_id"),
        "agent_id": payload["agent_id"],
        "user_id": payload["user_id"],
        "user_message_written": payload.get("user_message_written", False),
    }

    try:
        await task_executor.run_task(
            session_id=payload["session_id"],
            run_id=run_id,
            agent_id=payload["agent_id"],
            message=payload["message"],
            user_id=payload["user_id"],
            executor=executor_fn,
            disabled_tools=payload.get("disabled_tools"),
            agent_options=payload.get("agent_options"),
            attachments=payload.get("attachments"),
            existing_trace_id=payload.get("trace_id"),
            user_message_written=payload.get("user_message_written", False),
            disabled_skills=payload.get("disabled_skills"),
            enabled_skills=payload.get("enabled_skills"),
            persona_system_prompt=payload.get("persona_system_prompt"),
            disabled_mcp_tools=payload.get("disabled_mcp_tools"),
            display_message=payload.get("display_message"),
            recommendation_input=payload.get("recommendation_input"),
            team_id=payload.get("team_id"),
            active_goal=payload.get("active_goal"),
            auto_mode=bool(payload.get("auto_mode", False)),
        )
    # 用户主动中断：任务已达终态，清理 payload 并释放槽位（顺带推进队列）。
    except TaskInterruptedError:
        await payload_store.delete(run_id)
        await _release_concurrency_slot(payload.get("user_id"), run_id, dequeue=True)
        logger.info("Deleted arq payload after user interruption: run_id=%s", run_id)
    # 被 arq abort / 取消：需区分两种情形——
    #   - 若是用户取消：清理 payload、释放并推进队列，正常结束；
    #   - 否则视为服务关闭：标记为「可恢复失败」，保留 payload 但不推进队列
    #     （dequeue=False），并重新抛出让 arq 感知，等重启后由恢复流程接管。
    except asyncio.CancelledError:
        if await _is_user_cancelled_run(task_manager, payload["session_id"], run_id):
            await payload_store.delete(run_id)
            await _release_concurrency_slot(payload.get("user_id"), run_id, dequeue=True)
            logger.info("Deleted arq payload after user cancellation: run_id=%s", run_id)
            return
        await task_manager._mark_run_recoverable_failure(
            payload["session_id"],
            run_id,
            "Server shutdown",
        )
        await payload_store.delete(run_id)
        await _release_concurrency_slot(payload.get("user_id"), run_id, dequeue=False)
        raise
    # 其他异常：保留 payload 以便 arq 按重试策略再次投递（不删除、不释放）。
    except Exception:
        logger.warning("Keeping arq task payload for retry: run_id=%s", run_id)
        raise
    # 正常完成：清理 payload 并释放/推进队列。
    else:
        await payload_store.delete(run_id)
        await _release_concurrency_slot(payload.get("user_id"), run_id, dequeue=True)
    # 无论何种结局都清掉本进程内该 run 的运行信息，防止内存泄漏。
    finally:
        task_manager._run_info.pop(run_id, None)


# arq 加载的 worker 配置：注册可执行函数与启动钩子。
class WorkerSettings:
    functions = [run_agent_task]
    on_startup = worker_startup

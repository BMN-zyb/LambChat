"""任务状态 / 错误信息的只读查询。

对外提供「按 session 或 run 查状态、查错误」的能力。数据来源有多处（MongoDB
session.metadata、trace 存储、内存 run_info），本模块负责按优先级依次回退，
并把底层的 trace 状态映射为统一的 TaskStatus。
"""

from __future__ import annotations

from typing import Any

from src.infra.logging import get_logger
from src.infra.session.trace_storage import get_trace_storage

from .status import TaskStatus

logger = get_logger(__name__)


class TaskStatusQueries:
    """Read task status and error details from storage backends."""

    # storage: SessionStorage；run_info: manager 维护的 run_id -> 运行信息内存表，
    # 作为查不到持久化数据时的兜底来源。
    def __init__(self, *, storage: Any, run_info: dict[str, dict[str, Any]]) -> None:
        self._storage = storage
        self._run_info = run_info

    # 查会话当前任务状态：读 session.metadata.task_status；任何异常或缺失
    # 都保守回退为 PENDING（宁可让上层认为「还在进行」也不误报终态）。
    async def get_status(self, session_id: str) -> TaskStatus:
        """Get the current task status for a session."""
        try:
            session = await self._storage.get_by_session_id(session_id)
            if session and session.metadata:
                task_status = session.metadata.get("task_status")
                if task_status:
                    return TaskStatus(task_status)
        except Exception as e:
            logger.warning("Failed to get status from session storage: %s", e)
        return TaskStatus.PENDING

    # 查某个具体 run 的状态：优先看 session.metadata；再回退到 trace 存储，
    # 取该 run 最近一条 trace 并把 running/completed/error 映射为 TaskStatus。
    async def get_run_status(self, session_id: str, run_id: str) -> TaskStatus:
        """Get status for a specific run."""
        try:
            session = await self._storage.get_by_session_id(session_id)
            if session and session.metadata:
                task_status = session.metadata.get("task_status")
                if task_status:
                    return TaskStatus(task_status)
        except Exception as e:
            logger.warning("Failed to get run status from session storage: %s", e)

        try:
            trace_storage = get_trace_storage()
            cursor = (
                trace_storage.collection.find({"run_id": run_id}, {"status": 1, "_id": 0})
                .sort("started_at", -1)
                .limit(1)
            )
            traces = await cursor.to_list(length=1)
            if traces:
                trace_status = traces[0].get("status")
                if trace_status:
                    status_map = {
                        "running": TaskStatus.RUNNING,
                        "completed": TaskStatus.COMPLETED,
                        "error": TaskStatus.FAILED,
                    }
                    return status_map.get(trace_status, TaskStatus.PENDING)
        except Exception as e:
            logger.warning("Failed to get run status from trace storage: %s", e)

        return TaskStatus.PENDING

    # 查会话最近一次任务错误信息（来自 session.metadata.task_error）。
    async def get_error(self, session_id: str) -> str | None:
        """Get the latest task error for a session."""
        try:
            session = await self._storage.get_by_session_id(session_id)
            if session and session.metadata:
                return session.metadata.get("task_error")
        except Exception as e:
            logger.warning("Failed to get error from session storage: %s", e)
        return None

    # 查某个 run 的详细错误：先看 trace.metadata.error；没有则取该 trace 的最后
    # 一条 error 事件；仍没有则回退到内存 run_info 对应 session 的 task_error。
    async def get_run_error(self, run_id: str) -> str | None:
        """Get error details for a specific run."""
        try:
            trace_storage = get_trace_storage()
            cursor = (
                trace_storage.collection.find(
                    {"run_id": run_id}, {"metadata": 1, "trace_id": 1, "_id": 0}
                )
                .sort("started_at", -1)
                .limit(1)
            )
            traces = await cursor.to_list(length=1)
            if traces:
                trace = traces[0]
                metadata = trace.get("metadata", {})
                if metadata.get("error"):
                    return metadata.get("error")
                trace_id = trace.get("trace_id") or self.get_trace_id(run_id)
                if trace_id:
                    get_last_trace_event = getattr(trace_storage, "get_last_trace_event", None)
                    if get_last_trace_event is None:
                        get_last_trace_event = trace_storage.get_first_trace_event
                    event = await get_last_trace_event(trace_id, ["error"])
                    if event:
                        data = event.get("data", {})
                        return data.get("error")
        except Exception as e:
            logger.warning("Failed to get run error from trace storage: %s", e)

        run_info = self._run_info.get(run_id)
        if run_info:
            session_id = run_info.get("session_id")
            if session_id:
                try:
                    session = await self._storage.get_by_session_id(session_id)
                    if session and session.metadata:
                        return session.metadata.get("task_error")
                except Exception as e:
                    logger.warning("Failed to get run error from session storage: %s", e)

        return None

    # 从内存 run_info 中取该 run 关联的 trace_id（同步方法，无 IO）。
    def get_trace_id(self, run_id: str) -> str | None:
        """Get the trace identifier associated with a run."""
        info = self._run_info.get(run_id)
        return info.get("trace_id") if info else None

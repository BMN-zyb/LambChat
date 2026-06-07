"""Scheduled task execution engine.

Connects APScheduler triggers with the existing BackgroundTaskManager
so that dynamically-created tasks run through the normal agent pipeline.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from src.infra.chat.user_message_timestamp import format_user_message_with_timestamp
from src.infra.logging import get_logger
from src.infra.role.storage import RoleStorage
from src.infra.scheduler.locks import acquire_task_lock, release_task_lock
from src.infra.scheduler.runtime import get_runtime_scheduler
from src.infra.scheduler.storage import get_scheduled_task_storage
from src.infra.user.storage import UserStorage
from src.infra.utils.datetime import utc_now
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

_POLL_INTERVAL = 2  # seconds between status checks when waiting for completion
_DEFAULT_TIMEOUT = 600  # 10 minutes


@dataclass(frozen=True)
class _AttemptResult:
    status: RunStatus
    result: dict[str, Any]
    error_message: str | None = None


async def _resolve_task_owner(user_id: str) -> TokenPayload | None:
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

        Returns a dict like ``{"run_id": ..., "status": ..., "result": ...}``.
        """
        storage = get_scheduled_task_storage()
        task = await storage.get_task(task_id)
        if task is None:
            logger.warning("[Runner] task %s not found, skipping", task_id)
            return {"skipped": True, "reason": "task_not_found"}

        if not task.enabled or task.status != "active":
            return {"skipped": True, "reason": "disabled"}

        run_id = str(uuid.uuid4())

        # 1. Acquire distributed lock (multi-instance dedup)
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
        now = utc_now()
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

        # 3. Execute
        try:
            final_attempt: _AttemptResult | None = None
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
                    result = await self._execute_agent(task, run_id, session_id)
                    final_attempt = self._classify_attempt_result(result)
                except Exception as exc:
                    final_attempt = _AttemptResult(
                        status=RunStatus.FAILED,
                        result={},
                        error_message=str(exc),
                    )

                if final_attempt.status == RunStatus.SUCCESS:
                    break
                if final_attempt.status != RunStatus.FAILED:
                    break
                if attempt + 1 < max_attempts:
                    logger.warning(
                        "[Runner] task=%s run=%s attempt=%d failed status=%s, retrying",
                        task_id,
                        run_id,
                        attempt,
                        final_attempt.status.value,
                    )

            assert final_attempt is not None
            finished = utc_now()
            duration = int((finished - now).total_seconds() * 1000)
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
            await storage.update_task_run_stats(task_id, run_id, final_attempt.status)

            if final_attempt.status == RunStatus.SUCCESS:
                logger.info(
                    "[Runner] task=%s run=%s completed in %dms",
                    task_id,
                    run_id,
                    duration,
                )
            else:
                logger.warning(
                    "[Runner] task=%s run=%s finished status=%s after %dms: %s",
                    task_id,
                    run_id,
                    final_attempt.status.value,
                    duration,
                    final_attempt.error_message,
                )
            return {
                "run_id": run_id,
                "status": final_attempt.status.value,
                "result": final_attempt.result,
                **({"error": final_attempt.error_message} if final_attempt.error_message else {}),
            }

        except Exception as exc:
            finished = utc_now()
            duration = int((finished - now).total_seconds() * 1000)
            await storage.update_run(
                run_id,
                {
                    "status": RunStatus.FAILED,
                    "error_message": str(exc),
                    "finished_at": finished,
                    "duration_ms": duration,
                },
            )
            await storage.update_task_run_stats(task_id, run_id, RunStatus.FAILED)
            logger.exception("[Runner] task=%s run=%s failed after %dms", task_id, run_id, duration)
            raise

        finally:
            await release_task_lock(task_id, lock_token)
            if task.trigger_type == TriggerType.DATE and trigger_type == TriggerType.DATE.value:
                await storage.update_task(
                    task_id,
                    {"status": ScheduledTaskStatus.PAUSED, "enabled": False},
                )
                get_runtime_scheduler().unregister_job(task_id)

    # ── Internal ───────────────────────────────────

    @staticmethod
    def _build_session_id(task_id: str, run_id: str) -> str:
        return f"sch_{task_id}_{run_id[:8]}"

    async def _execute_agent(self, task: ScheduledTask, run_id: str, session_id: str) -> dict:
        """Execute the agent via BackgroundTaskManager in a dedicated session."""
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
        message = format_user_message_with_timestamp(
            display_message,
            user_timezone if isinstance(user_timezone, str) else None,
        )
        agent_options = task.input_payload.get("agent_options")
        if isinstance(agent_options, dict):
            from src.api.routes.chat import validate_agent_model_access

            user = await _resolve_task_owner(task.owner_id)
            if user is None:
                raise RuntimeError(f"Scheduled task owner '{task.owner_id}' not found")
            await validate_agent_model_access(agent_options, user)
        else:
            agent_options = None

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
                project_id=None,
                session_name=f"{task.name}",
                display_message=display_message,
                recommendation_input=display_message,
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
                project_id=None,
                session_name=f"{task.name}",
                display_message=display_message,
                recommendation_input=display_message,
                write_user_message_immediately=True,
            )
        await SessionManager().update_session_metadata(
            session_id,
            {
                "source": "scheduled_task",
                "scheduled_task_id": task.id,
                "scheduled_task_run_id": run_id,
                "hidden_from_conversation_list": True,
            },
        )

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


# ── Singleton ──────────────────────────────────────

_runner: Optional[ScheduledTaskRunner] = None


def get_scheduled_task_runner() -> ScheduledTaskRunner:
    global _runner
    if _runner is None:
        _runner = ScheduledTaskRunner()
    return _runner

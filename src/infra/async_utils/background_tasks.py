"""Small bounded helpers for best-effort background work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from src.infra.logging import get_logger

logger = get_logger(__name__)


async def _noop_task() -> None:
    return None


class BestEffortTaskLimiter:
    """Track and bound fire-and-forget tasks that may be safely skipped."""

    def __init__(self, name: str, max_tasks: int) -> None:
        self._name = name
        self._max_tasks = max(0, int(max_tasks))
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def create_task(self, awaitable: Awaitable[None]) -> asyncio.Task[None]:
        async def run_best_effort() -> None:
            try:
                await awaitable
            except Exception as exc:
                logger.warning("%s background task failed: %s", self._name, exc)

        if self._max_tasks <= 0 or len(self._tasks) >= self._max_tasks:
            logger.debug(
                "Skipping %s background task because %s tasks are active and the limit is %s",
                self._name,
                len(self._tasks),
                self._max_tasks,
            )
            if hasattr(awaitable, "close"):
                awaitable.close()
            return asyncio.create_task(_noop_task())

        task = asyncio.create_task(run_best_effort())
        self._tasks.add(task)

        def on_done(done_task: asyncio.Task[None]) -> None:
            self._tasks.discard(done_task)
            if done_task.cancelled():
                return
            done_task.result()

        task.add_done_callback(on_done)
        return task

    async def drain(self, timeout: float = 10.0) -> None:
        tasks = list(self._tasks)
        if not tasks:
            return

        _, pending = await asyncio.wait(tasks, timeout=max(0.0, float(timeout)))
        for task in pending:
            task.cancel()
        if pending:
            logger.warning(
                "Cancelling %s %s background task(s) after %.1fs drain timeout",
                len(pending),
                self._name,
                timeout,
            )
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.difference_update(tasks)

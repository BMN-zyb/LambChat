"""Small bounded helpers for best-effort background work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from src.infra.logging import get_logger

logger = get_logger(__name__)


async def _noop_task() -> None:
    # 空任务占位:当因超限而跳过某个后台任务时,仍返回一个已可 await 的 Task,统一调用方接口。
    return None


class BestEffortTaskLimiter:
    """Track and bound fire-and-forget tasks that may be safely skipped."""

    def __init__(self, name: str, max_tasks: int) -> None:
        # name: 用于日志标识; max_tasks: 并发上限(<=0 表示不允许创建,全部跳过)。
        self._name = name
        self._max_tasks = max(0, int(max_tasks))
        # 追踪在跑的任务集合,用于限流判断与优雅排空。
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def active_count(self) -> int:
        # 当前在跑的后台任务数。
        return len(self._tasks)

    def create_task(self, awaitable: Awaitable[None]) -> asyncio.Task[None]:
        # 创建一个「尽力而为」的后台任务:超限则安全跳过,否则包裹执行并纳入追踪。
        async def run_best_effort() -> None:
            # 吞掉任务内异常(仅告警):best-effort 任务失败不应影响主流程。
            try:
                await awaitable
            except Exception as exc:
                logger.warning("%s background task failed: %s", self._name, exc)

        # 达到上限(或不允许):跳过。若传入的是协程,主动 close() 释放它以免"未 await 协程"告警。
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
            # 完成后移出集合;被取消则忽略,否则取 result() 触发潜在异常(此处 run_best_effort
            # 已内部吞异常,故正常不会抛出)。
            self._tasks.discard(done_task)
            if done_task.cancelled():
                return
            done_task.result()

        task.add_done_callback(on_done)
        return task

    async def drain(self, timeout: float = 10.0) -> None:
        # 排空:等待所有在跑任务至多 timeout 秒;超时未完成的一律取消并回收。
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

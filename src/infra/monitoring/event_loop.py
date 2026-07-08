"""Event loop lag monitoring for detecting accidental blocking calls."""

from __future__ import annotations

import asyncio
from typing import Any

from src.infra.logging import get_logger

logger = get_logger(__name__)


class EventLoopLagMonitor:
    """Periodically warns when the current event loop is blocked too long."""

    # 实现原理：每隔 interval_seconds 醒来一次，比较"实际经过时间"与"预期经过时间"。
    # 如果某次同步代码（比如未 await 的阻塞 IO、CPU 密集计算）占住了事件循环，
    # 下一次 sleep 唤醒时的实际时间就会明显晚于预期，由此推断出发生了"意外阻塞"。
    def __init__(
        self,
        *,
        interval_seconds: float = 1.0,
        threshold_seconds: float = 2.0,
        logger: Any = logger,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._threshold_seconds = threshold_seconds
        self._logger = logger
        self._task: asyncio.Task[None] | None = None

    # 任务存在且尚未结束（未被取消或跑完）才算处于运行中
    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        # 已在运行则直接跳过，保证同一时刻只有一个后台监控任务
        if self.is_running:
            return
        self._task = asyncio.create_task(self._run(), name="event-loop-lag-monitor")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        # expected 记录"如果完全没有阻塞，下一次应该被唤醒的时间点"（使用单调时钟 loop.time()）
        expected = loop.time() + self._interval_seconds
        try:
            while True:
                await asyncio.sleep(self._interval_seconds)
                now = loop.time()
                # lag = 实际唤醒时间 - 预期唤醒时间；lag 越大说明事件循环被阻塞得越久
                lag = max(0.0, now - expected)
                if lag >= self._threshold_seconds:
                    self._logger.warning(
                        "Event loop lag detected: %.3fs over threshold %.3fs",
                        lag,
                        self._threshold_seconds,
                    )
                # 无论是否触发告警，都基于本次实际唤醒时间重新计算下一次的期望时间点，
                # 避免误差在多次循环之间累积放大
                expected = now + self._interval_seconds
        except asyncio.CancelledError:
            # 任务被取消时应重新抛出，让 asyncio 正常走完取消流程
            raise


# 进程级单例：事件循环 lag 监控只需要一份，多次调用 get_event_loop_lag_monitor 应复用同一实例
_monitor: EventLoopLagMonitor | None = None


def get_event_loop_lag_monitor() -> EventLoopLagMonitor:
    global _monitor
    if _monitor is None:
        _monitor = EventLoopLagMonitor()
    return _monitor


async def start_event_loop_lag_monitor() -> None:
    await get_event_loop_lag_monitor().start()


async def stop_event_loop_lag_monitor() -> None:
    global _monitor
    monitor = _monitor
    # 先取出并清空单例引用，再停止，避免停止过程中其他协程仍拿到即将失效的实例
    _monitor = None
    if monitor is not None:
        await monitor.stop()

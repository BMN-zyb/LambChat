"""Controlled offloading for unavoidable synchronous IO.

Use this helper for third-party SDK calls and filesystem work that do not have
native async APIs. It keeps those calls off the FastAPI event loop and avoids
unbounded growth of the default executor.
"""

from __future__ import annotations

import asyncio
import functools
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# 线程池默认工作线程数与默认「排队上限」。
_DEFAULT_MAX_WORKERS = 8
_DEFAULT_MAX_PENDING = 16
# 允许排队(尚未有空闲线程)的最大任务数,可用环境变量覆盖;下限保护为 0。
_MAX_PENDING_BLOCKING_IO = max(
    0,
    int(os.getenv("BLOCKING_IO_MAX_PENDING", _DEFAULT_MAX_PENDING)),
)
# 全局唯一的阻塞 IO 线程池:所有 run_blocking_io 调用都在这里执行同步任务,
# 从而把无原生 async API 的第三方 SDK/文件系统调用挪出事件循环,避免阻塞。
# 用固定大小线程池,防止默认 executor 无上限增长。
_BLOCKING_IO_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.getenv("BLOCKING_IO_MAX_WORKERS", _DEFAULT_MAX_WORKERS)),
    thread_name_prefix="blocking-io",
)
# 每个事件循环一个提交信号量,做背压(限制同时提交/排队的任务数)。以 loop 为键是因为
# 信号量必须绑定到创建它的事件循环,不同 loop(如测试中多次新建)不能共用同一个信号量。
_LOOP_LIMITERS: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _get_submission_limiter(loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
    # 取(或惰性创建)当前事件循环对应的提交信号量。
    limiter = _LOOP_LIMITERS.get(loop)
    if limiter is not None:
        return limiter

    # 信号量容量 = 线程数 + 允许排队数:既能占满全部线程,又限定额外排队量,超出则调用方等待。
    max_workers = max(1, int(getattr(_BLOCKING_IO_EXECUTOR, "_max_workers", _DEFAULT_MAX_WORKERS)))
    limiter = asyncio.Semaphore(max_workers + _MAX_PENDING_BLOCKING_IO)
    _LOOP_LIMITERS[loop] = limiter
    return limiter


def _release_limiter(loop: asyncio.AbstractEventLoop, limiter: asyncio.Semaphore) -> None:
    # 线程池任务完成回调在「线程池线程」里执行,而信号量属于事件循环,
    # 故必须用 call_soon_threadsafe 跨线程安全地回到 loop 上释放;loop 已关闭则跳过。
    if loop.is_closed():
        return
    loop.call_soon_threadsafe(limiter.release)


async def run_blocking_io(
    func: Callable[..., T],
    *args: Any,
    timeout: float | None = None,
    **kwargs: Any,
) -> T:
    """Run a synchronous IO callable without blocking the current event loop."""
    # 把同步阻塞调用提交到线程池执行,不阻塞当前事件循环。可选 timeout 覆盖「等待名额 + 执行」全过程。
    loop = asyncio.get_running_loop()
    limiter = _get_submission_limiter(loop)
    start_time = loop.time()
    # 先申请信号量名额(带背压);带 timeout 时,连「等待名额」也计入总超时。
    if timeout is not None:
        await asyncio.wait_for(limiter.acquire(), timeout=timeout)
    else:
        await limiter.acquire()

    # 把位置/关键字参数固化进无参可调用,交给线程池。
    call = functools.partial(func, *args, **kwargs)
    try:
        future = _BLOCKING_IO_EXECUTOR.submit(call)
    except Exception:
        # 提交失败(如线程池已关闭)要立即归还名额,否则会永久泄漏一个信号量额度。
        limiter.release()
        raise
    # 任务无论成功/失败/取消,完成后都通过回调归还信号量名额。
    future.add_done_callback(lambda _future: _release_limiter(loop, limiter))
    wrapped = asyncio.wrap_future(future)

    try:
        if timeout is not None:
            # 扣除前面等待名额已耗费的时间,用「剩余预算」等待执行结果;预算耗尽则直接超时。
            remaining_timeout = timeout - (loop.time() - start_time)
            if remaining_timeout <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(wrapped, timeout=remaining_timeout)
        return await wrapped
    except asyncio.TimeoutError:
        # 超时时尽力取消底层 future(注意:已在运行的同步调用无法真正中断,仅尽力而为)。
        future.cancel()
        raise


def shutdown_blocking_io_executor() -> None:
    """Release worker threads during process shutdown.

    Do not wait here: shutdown runs on the application stop path and must not
    hang behind a third-party SDK or filesystem call that failed to return.
    """
    _BLOCKING_IO_EXECUTOR.shutdown(wait=False, cancel_futures=True)

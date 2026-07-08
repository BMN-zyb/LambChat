"""嵌入式 arq worker 运行时。

允许把 arq worker 直接跑在 FastAPI 进程内（而非另起独立 worker 进程），由本模块
负责它的启动/停止生命周期。仅当 TASK_BACKEND=arq 且开启 ARQ_EMBEDDED_WORKER 时
才真正启动，方便单进程部署。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable

from arq.worker import Worker

from src.infra.logging import get_logger
from src.kernel.config import settings

from .arq_payloads import TaskArqPayloadStore
from .arq_settings import build_arq_redis_settings
from .arq_worker import run_agent_task

logger = get_logger(__name__)


class EmbeddedArqRuntime:
    """Own the lifecycle of an arq worker embedded in the FastAPI process."""

    # worker_factory 可注入以便测试（默认用 arq 的 Worker）。_worker/_task 分别是
    # worker 实例与承载它的 asyncio future。
    def __init__(self, worker_factory: Callable[..., Any] = Worker) -> None:
        self._worker_factory = worker_factory
        self._worker: Any | None = None
        self._task: asyncio.Future | None = None

    # 是否正在运行：future 存在且未结束。
    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # 启动嵌入式 worker。多重前置判断（已在跑 / 非 arq 后端 / 未开启嵌入式）任一
    # 命中则直接返回。否则按配置构建 Worker（队列名、并发上限、超时、payload store
    # 等），handle_signals=False 让信号交由宿主进程统一处理，用 ensure_future 挂到
    # 事件循环后台运行。
    async def start(self) -> None:
        if self.is_running:
            return
        if getattr(settings, "TASK_BACKEND", "local") != "arq":
            return
        if not getattr(settings, "ARQ_EMBEDDED_WORKER", True):
            return

        self._worker = self._worker_factory(
            [run_agent_task],
            queue_name=settings.ARQ_QUEUE_NAME,
            redis_settings=build_arq_redis_settings(settings),
            handle_signals=False,
            max_jobs=settings.ARQ_WORKER_MAX_JOBS,
            job_timeout=settings.ARQ_JOB_TIMEOUT_SECONDS,
            ctx={"payload_store": TaskArqPayloadStore()},
            allow_abort_jobs=True,
        )
        self._task = asyncio.ensure_future(self._worker.async_run())
        logger.info("Embedded arq worker started")

    # 停止嵌入式 worker：先调 worker.close()（兼容同步/协程返回），再取消后台 future
    # 并等待其退出，最后清空引用。
    async def stop(self) -> None:
        if self._worker is not None:
            close = getattr(self._worker, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._worker = None
        self._task = None


_runtime: EmbeddedArqRuntime | None = None


# 进程内单例访问器。
def get_arq_runtime() -> EmbeddedArqRuntime:
    global _runtime
    if _runtime is None:
        _runtime = EmbeddedArqRuntime()
    return _runtime


# 应用启动钩子：启动嵌入式 worker（内部按配置判断是否真正启动）。
async def start_arq_runtime() -> None:
    await get_arq_runtime().start()


# 应用关闭钩子：停止并释放嵌入式 worker 单例。
async def stop_arq_runtime() -> None:
    global _runtime
    runtime = _runtime
    _runtime = None
    if runtime is not None:
        await runtime.stop()

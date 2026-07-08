"""
Memory Backend Base - Abstract interface & shared utilities.

All memory backends inherit from MemoryBackend.
Currently only the native MongoDB-backed backend is supported.
"""

import asyncio
import random
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from src.infra.logging import get_logger

logger = get_logger(__name__)


# ============================================================================
# Shared Concurrency Utilities
# ============================================================================

# 以下几个模块级字典/变量实现"事件循环级别的单例资源"：同一个 asyncio 事件循环内共享同一份
# 信号量/锁，不同事件循环（如测试中多次创建新循环）之间互不干扰，避免跨循环复用 asyncio 对象导致的报错。
_loop_locals: dict[int, dict[str, Any]] = {}
_loop_locals_lock: Optional[asyncio.Lock] = None
_loop_locals_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_loop_locals_lock() -> asyncio.Lock:
    """Get or create the loop-locals lock (lazy, multi-loop safe)."""
    # 若锁不存在，或锁是在别的事件循环上创建的（说明发生了循环切换），则重新创建一把绑定到当前循环的锁。
    global _loop_locals_lock, _loop_locals_lock_loop
    current_loop = asyncio.get_running_loop()
    if _loop_locals_lock is None or _loop_locals_lock_loop is not current_loop:
        _loop_locals_lock = asyncio.Lock()
        _loop_locals_lock_loop = current_loop
    return _loop_locals_lock


def _get_loop_id() -> int:
    """Get unique identifier for current event loop."""
    # 用事件循环对象的 id() 作为该循环的唯一标识；若当前没有运行中的循环则返回 0 作为兜底键。
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


async def _get_loop_local(name: str, factory: Callable[[], Any]) -> Any:
    """Get or create a loop-local resource (async-safe)."""
    # 按 (loop_id, name) 惰性创建并缓存资源；用锁保护创建过程，避免并发场景下重复创建同名资源。
    loop_id = _get_loop_id()
    async with _get_loop_locals_lock():
        if loop_id not in _loop_locals:
            _loop_locals[loop_id] = {}
        if name not in _loop_locals[loop_id]:
            _loop_locals[loop_id][name] = factory()
        return _loop_locals[loop_id][name]


async def get_request_semaphore(namespace: str, max_concurrent: int = 64) -> asyncio.Semaphore:
    """Get or create a namespaced request semaphore for current event loop."""
    # 用于限制某个命名空间（如某个记忆后端）对外部服务的最大并发请求数，防止瞬时流量打爆下游。
    return await _get_loop_local(
        f"{namespace}_semaphore", lambda: asyncio.Semaphore(max_concurrent)
    )


async def get_client_lock(namespace: str) -> asyncio.Lock:
    """Get or create a namespaced client lock for current event loop."""
    # 用于保护某个命名空间下"客户端初始化"等只应发生一次的操作，避免并发重复初始化。
    return await _get_loop_local(f"{namespace}_client_lock", lambda: asyncio.Lock())


async def with_retry(
    func: Callable[[], Any],
    *,
    semaphore: asyncio.Semaphore,
    max_retries: int = 3,
    retry_delay: float = 0.5,
    namespace: str = "Memory",
) -> Any:
    """Execute an async operation with retry logic and concurrency control."""
    # 通用重试封装：每次尝试都先获取信号量做并发限流，失败后按指数退避 + 随机抖动等待再重试；
    # 用 last_error 记录最近一次异常，重试次数耗尽后把它重新抛出，保留原始错误信息。
    last_error: BaseException | None = None
    for attempt in range(max_retries):
        try:
            async with semaphore:
                return await func()
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = retry_delay * (2**attempt) + random.uniform(0, 0.1)
                logger.warning(
                    f"[{namespace}] Retry {attempt + 1}/{max_retries} after error: {e}. "
                    f"Waiting {delay:.2f}s"
                )
                await asyncio.sleep(delay)

    if last_error is None:
        raise RuntimeError("Unexpected state: no error captured after retry loop")
    raise last_error


def clear_loop_locals(namespace: str) -> None:
    """Clear loop-local storage for a given namespace."""
    loop_id = _get_loop_id()
    if loop_id in _loop_locals:
        _loop_locals[loop_id].pop(f"{namespace}_semaphore", None)
        _loop_locals[loop_id].pop(f"{namespace}_client_lock", None)
        # Clean up empty loop entries to prevent memory accumulation
        if not _loop_locals[loop_id]:
            del _loop_locals[loop_id]


# ============================================================================
# Abstract Backend Interface
# ============================================================================


class MemoryBackend(ABC):
    """Abstract base class for memory backends."""

    # 这是记忆功能的核心可插拔协议：无论底层用什么存储/检索技术实现，
    # 只要实现 retain/recall/delete 三个抽象方法与 name 属性，就能被上层统一调度使用。

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier (e.g. 'native')."""
        ...

    @abstractmethod
    async def retain(
        self,
        user_id: str,
        content: str,
        context: Optional[str] = None,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[list[str]] = None,
        existing_memory_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Store a memory."""
        # existing_memory_id 用于"更新已有记忆"而非新建，避免同一事实被重复存储多条。
        ...

    @abstractmethod
    async def recall(
        self,
        user_id: str,
        query: str,
        max_results: int = 5,
        memory_types: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Recall memories matching the query."""
        ...

    @abstractmethod
    async def delete(
        self,
        user_id: str,
        memory_id: str,
    ) -> dict[str, Any]:
        """Delete a memory by ID."""
        ...

    async def close(self) -> None:
        """Release resources held by this backend. Default is a no-op."""
        pass


# ============================================================================
# Backend Factory
# ============================================================================


async def create_memory_backend() -> Optional[MemoryBackend]:
    """
    Create the active memory backend based on configuration.

    Returns None if memory is disabled via master switch.
    Only native (MongoDB-backed) backend is supported.
    """
    from src.kernel.config import settings

    # 记忆功能总开关：关闭时直接返回 None，上层调用方应据此跳过记忆相关逻辑。
    if not settings.ENABLE_MEMORY:
        return None

    try:
        from src.infra.memory.client.native import NativeMemoryBackend

        backend = NativeMemoryBackend()
        await backend.initialize()
        # 只有底层集合真正初始化成功才认为后端可用；否则视为初始化失败，走下面的兜底返回 None。
        if backend._collection is not None:
            return backend
    except Exception as e:
        logger.warning(f"[Memory] Failed to initialize native backend: {e}")

    return None


def is_memory_enabled() -> bool:
    """Check if memory feature is enabled (master switch)."""
    from src.kernel.config import settings

    return settings.ENABLE_MEMORY


def get_user_id_from_runtime(runtime: Any) -> Optional[str]:
    """Extract user_id from ToolRuntime context."""
    # 从工具运行时对象里逐层挖出 user_id：runtime.config["configurable"]["context"].user_id；
    # 任何一层缺失或类型不符都静默返回 None，不影响调用方的正常流程。
    if not runtime:
        return None
    try:
        if hasattr(runtime, "config"):
            config = runtime.config
            if isinstance(config, dict):
                configurable = config.get("configurable", {})
                context = configurable.get("context")
                if context and hasattr(context, "user_id"):
                    return context.user_id
    except Exception:
        pass
    return None

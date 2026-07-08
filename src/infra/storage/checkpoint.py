"""
Checkpoint 存储实现

提供 LangGraph checkpointer 的工厂函数，支持 MongoDB 和 PostgreSQL 持久化。

用户通过 CHECKPOINT_BACKEND 配置选择后端：
- "mongodb": 使用 MongoDBSaver（默认，受 16MB 文档大小限制）
- "postgres": 使用 AsyncPostgresSaver + 连接池（无文档大小限制，需 PostgreSQL 连接参数）

两者都不可用时回退到 MemorySaver（内存存储，重启丢失）。
"""

import asyncio
import copy
import inspect
import random
import time
from collections import OrderedDict
from types import MethodType
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    empty_checkpoint,
)

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)

# 当 MongoDB/PostgreSQL 都不可用时会降级到 MemorySaver（纯内存，进程重启即丢失）。
# 为避免无限增长，MemorySaver 按 thread_id 缓存在一个 LRU + TTL 的 OrderedDict 里：
# - _MEMORY_SAVER_MAX_THREADS: 缓存的最大线程数，超出后淘汰最久未访问的
# - _MEMORY_SAVER_TTL_SECONDS: 单个线程缓存的存活时间，超时视为过期
# - _MEMORY_SAVER_CLEANUP_INTERVAL: 每访问多少次才触发一次清理扫描，避免每次访问都扫描
_MEMORY_SAVER_MAX_THREADS = max(int(getattr(settings, "MEMORY_SAVER_MAX_THREADS", 200) or 0), 1)
_MEMORY_SAVER_TTL_SECONDS = max(
    int(getattr(settings, "MEMORY_SAVER_TTL_SECONDS", 3600) or 0),
    1,
)
_MEMORY_SAVER_CLEANUP_INTERVAL = max(
    int(getattr(settings, "MEMORY_SAVER_CLEANUP_INTERVAL", 50) or 0),
    1,
)
# 会话 fork（分叉重新生成）时，按页扫描历史 checkpoint 寻找分叉点，此为每页大小
_FORK_CHECKPOINT_SCAN_PAGE_SIZE = 25
# CHECKPOINT_BACKEND 配置为以下值时，视为显式关闭持久化 checkpoint 存储
_DISABLED_CHECKPOINT_BACKENDS = {"", "0", "false", "none", "off", "disabled"}

# MongoDB Checkpointer 单例
_mongo_checkpointer: Optional[BaseCheckpointSaver[Any]] = None

# PostgreSQL Checkpointer 单例
_pg_checkpointer: Optional[BaseCheckpointSaver[Any]] = None
# 与 _pg_checkpointer 配套的连接池，关闭 checkpointer 时需要一并关闭，避免连接泄漏
_pg_checkpointer_pool: Any | None = None
# 保护 PostgreSQL checkpointer 初始化/关闭过程的锁，避免并发时重复创建连接池
_pg_checkpointer_lock: asyncio.Lock | None = None


def _ensure_string_channel_version_support(
    checkpointer: BaseCheckpointSaver[Any],
) -> BaseCheckpointSaver[Any]:
    """Patch savers that still inherit LangGraph's int-only version generator."""
    # 子类如果已经自己重写了 get_next_version（说明原生支持字符串版本号），就不需要打补丁
    if type(checkpointer).get_next_version is not BaseCheckpointSaver.get_next_version:
        return checkpointer

    # 猴子补丁：把版本号生成函数替换为兼容字符串版本号的实现
    # （LangGraph 基类默认实现只会对 int 做自增，遇到字符串版本号会报错）
    def _get_next_version(self: BaseCheckpointSaver[Any], current: Any, channel: None) -> Any:
        del self, channel
        if isinstance(current, str):
            current_v = int(current.split(".", 1)[0])
            next_v = current_v + 1
            next_h = random.random()
            # 格式化为固定宽度的"序号.随机数"字符串，保证按字符串排序时也能与数值大小顺序一致
            return f"{next_v:032}.{next_h:016}"
        if current is None:
            return 1
        return current + 1

    # 用 MethodType 绑定为该实例的方法，只影响这一个 checkpointer 实例，不影响类的其他实例
    checkpointer.get_next_version = MethodType(_get_next_version, checkpointer)  # type: ignore[method-assign]
    return checkpointer


# 懒创建模块级异步锁：不能在模块导入时就直接创建 asyncio.Lock()，
# 因为那时可能还没有运行中的事件循环
def _get_pg_checkpointer_lock() -> asyncio.Lock:
    global _pg_checkpointer_lock

    if _pg_checkpointer_lock is None:
        _pg_checkpointer_lock = asyncio.Lock()
    return _pg_checkpointer_lock


# 清理 MemorySaver 兜底缓存中过期/超量的条目（TTL 淘汰 + LRU 淘汰），返回被清理的条目数。
# 缓存本身是懒挂载在 get_async_checkpointer 函数对象上的属性，这里通过 getattr 读取。
def _cleanup_memory_saver_cache(now: float | None = None) -> int:
    cache: OrderedDict[str, tuple[object, float]] | None = getattr(
        get_async_checkpointer,
        "_memory_saver_cache",
        None,
    )
    if not cache:
        return 0

    current_time = time.time() if now is None else now
    removed = 0

    # 先按 TTL 找出所有已过期（太久没被访问）的线程缓存
    stale_threads = [
        thread_id
        for thread_id, (_, last_access) in list(cache.items())
        if current_time - last_access > _MEMORY_SAVER_TTL_SECONDS
    ]
    for thread_id in stale_threads:
        cache.pop(thread_id, None)
        removed += 1

    # 再按 LRU 淘汰最久未访问的条目，直到数量回落到上限内
    # （OrderedDict 头部是最久未访问的，popitem(last=False) 即弹出头部）
    while len(cache) > _MEMORY_SAVER_MAX_THREADS:
        cache.popitem(last=False)
        removed += 1

    # 缓存清空后把属性本身也删掉，避免长期挂着一个空字典
    if not cache and hasattr(get_async_checkpointer, "_memory_saver_cache"):
        delattr(get_async_checkpointer, "_memory_saver_cache")

    return removed


def close_async_checkpointer() -> None:
    """Release MemorySaver fallback references so the process can reclaim memory."""
    # 这里操作的都是懒挂载在 get_async_checkpointer 函数对象上的"伪属性"（相当于给函数挂缓存），
    # 逐个删除后下次调用会重新创建，从而让 GC 能够回收之前的 MemorySaver 及其持有的所有 checkpoint 数据
    if hasattr(get_async_checkpointer, "_memory_saver"):
        delattr(get_async_checkpointer, "_memory_saver")
    if hasattr(get_async_checkpointer, "_memory_saver_cache"):
        delattr(get_async_checkpointer, "_memory_saver_cache")
    if hasattr(get_async_checkpointer, "_memory_saver_access_count"):
        delattr(get_async_checkpointer, "_memory_saver_access_count")


def get_checkpointer_diagnostics() -> dict[str, Any]:
    """Return lightweight checkpointer runtime state for memory diagnostics."""
    # 汇总当前进程内各 checkpointer 单例/连接池/缓存的存活状态，供内存诊断、健康检查接口使用；
    # 只反映"有没有实例、缓存多大"，不涉及具体会话数据
    cache: OrderedDict[str, tuple[object, float]] | None = getattr(
        get_async_checkpointer,
        "_memory_saver_cache",
        None,
    )
    return {
        "configured_backend": str(getattr(settings, "CHECKPOINT_BACKEND", "mongodb")),
        "backend_enabled": is_checkpoint_backend_enabled(),
        "mongo_checkpointer_active": _mongo_checkpointer is not None,
        "postgres_checkpointer_active": _pg_checkpointer is not None,
        "postgres_pool_active": _pg_checkpointer_pool is not None,
        "memory_saver_singleton_active": hasattr(get_async_checkpointer, "_memory_saver"),
        "memory_saver_cache_active": cache is not None,
        "memory_saver_cache_size": len(cache or {}),
        "memory_saver_cache_limit": _MEMORY_SAVER_MAX_THREADS,
        "memory_saver_ttl_seconds": _MEMORY_SAVER_TTL_SECONDS,
    }


async def reset_checkpointer_runtime_state() -> None:
    """Drop process-local checkpointer references after checkpoint settings change."""
    # 配置热更新（比如切换 CHECKPOINT_BACKEND）后调用：清空所有已缓存的 checkpointer/连接池引用，
    # 下次访问时会按新配置重新创建，而不是继续用旧配置建立的连接
    close_async_checkpointer()
    close_mongo_checkpointer()
    await close_pg_checkpointer()


def get_mongo_checkpointer(collection_name: str = "checkpoints") -> BaseCheckpointSaver[Any] | None:
    """
    获取 MongoDB checkpointer 单例

    复用 motor 的底层同步 MongoClient，避免创建独立的同步连接池。

    Args:
        collection_name: MongoDB collection 名称，默认为 "checkpoints"

    Returns:
        MongoDBSaver 实例，如果创建失败则返回 None
    """
    global _mongo_checkpointer
    if _mongo_checkpointer is not None:
        return _mongo_checkpointer

    try:
        from langgraph.checkpoint.mongodb import MongoDBSaver

        from src.infra.storage.mongodb import get_mongo_client

        # 复用 motor 异步客户端底层的同步 pymongo 客户端（.delegate），不另外新开一套连接池
        motor_client = get_mongo_client()
        sync_client = motor_client.delegate

        cp = MongoDBSaver(
            sync_client,
            db_name=settings.MONGODB_DB,
            checkpoint_collection_name=collection_name,
        )

        logger.info(
            f"MongoDB checkpointer created: {settings.MONGODB_DB}.{collection_name} (reusing motor connection pool)"
        )
        _mongo_checkpointer = cp
        return _mongo_checkpointer

    except ImportError as e:
        logger.warning(f"MongoDB checkpointer not available: {e}")
        return None
    except Exception as e:
        logger.warning(f"Failed to create MongoDB checkpointer: {e}")
        return None


def close_mongo_checkpointer():
    """释放 MongoDB checkpointer 单例引用，允许 GC 回收。"""
    global _mongo_checkpointer
    if _mongo_checkpointer is not None:
        _mongo_checkpointer = None
        logger.info("MongoDB checkpointer reference released")


def is_checkpoint_backend_enabled() -> bool:
    """Return whether persistent checkpoint storage is configured."""
    # CHECKPOINT_BACKEND 配置为空/"0"/"false"/"none"/"off"/"disabled" 时视为关闭持久化，
    # 调用方（如会话删除逻辑）应据此跳过对 checkpoint 后端的访问
    backend = str(getattr(settings, "CHECKPOINT_BACKEND", "mongodb") or "").strip().lower()
    return backend not in _DISABLED_CHECKPOINT_BACKENDS


async def get_pg_checkpointer() -> BaseCheckpointSaver[Any] | None:
    """
    获取 PostgreSQL checkpointer 单例（异步）

    使用 AsyncPostgresSaver + AsyncConnectionPool，无 16MB 文档大小限制。
    仅需 CHECKPOINT_BACKEND=postgres，独立于 ENABLE_POSTGRES_STORAGE。

    Returns:
        AsyncPostgresSaver 实例，如果创建失败则返回 None
    """
    global _pg_checkpointer, _pg_checkpointer_pool

    # 快速路径：已创建成功则直接返回，避免每次调用都去抢锁
    if _pg_checkpointer is not None:
        return _pg_checkpointer

    # 双重检查锁定：进锁后再判断一次，防止并发调用时重复创建连接池
    async with _get_pg_checkpointer_lock():
        if _pg_checkpointer is not None:
            return _pg_checkpointer

        return await _create_pg_checkpointer()


# 真正创建 PostgreSQL checkpointer 的内部实现；必须在持有 _pg_checkpointer_lock 的前提下调用，
# 避免并发场景下重复建立连接池导致连接泄漏
async def _create_pg_checkpointer() -> BaseCheckpointSaver[Any] | None:
    global _pg_checkpointer, _pg_checkpointer_pool

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        # open=False：先只构造池对象，不在构造时立即建立连接，
        # 以便下面手动 await pool.open() 并在失败时捕获异常、清理资源
        pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
            settings.checkpoint_postgres_url,
            min_size=settings.CHECKPOINT_PG_POOL_MIN_SIZE,
            max_size=settings.CHECKPOINT_PG_POOL_MAX_SIZE,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            open=False,
        )
        try:
            # wait=True：阻塞等待直到池中至少建立好一个可用连接
            await pool.open(wait=True)
        except Exception:
            # 打开失败时主动关闭已分配的资源，再重新抛出交给外层统一处理
            await pool.close()
            raise

        cp = AsyncPostgresSaver(pool)
        try:
            # setup() 会在数据库中创建/迁移 checkpoint 相关表结构（幂等操作）
            await cp.setup()
            logger.info(
                "PostgreSQL checkpointer created (AsyncPostgresSaver via connection pool, min=%d, max=%d)",
                settings.CHECKPOINT_PG_POOL_MIN_SIZE,
                settings.CHECKPOINT_PG_POOL_MAX_SIZE,
            )
            _pg_checkpointer_pool = pool
            _pg_checkpointer = cp
            return _pg_checkpointer
        except Exception:
            # 建表失败也要关闭连接池，避免连接泄漏
            await pool.close()
            raise

    except ImportError as e:
        # 未安装 psycopg / psycopg_pool 等依赖时会走到这里
        logger.warning(f"PostgreSQL checkpointer not available: {e}")
        return None
    except Exception as e:
        # 其余异常（连接失败、鉴权失败、建表失败等）都视为该后端不可用，返回 None 交给上层 fallback
        logger.warning(f"Failed to create PostgreSQL checkpointer: {e}")
        return None


async def close_pg_checkpointer():
    """
    关闭 PostgreSQL checkpointer（释放连接）

    应在应用关闭时调用。
    """
    global _pg_checkpointer, _pg_checkpointer_pool

    # 加锁执行，避免关闭过程中又有协程在并发创建新的 checkpointer/连接池
    async with _get_pg_checkpointer_lock():
        pool = _pg_checkpointer_pool
        if _pg_checkpointer is None and pool is None:
            return

        try:
            if pool is not None:
                await pool.close()
            logger.info("PostgreSQL checkpointer closed")
        except Exception as e:
            logger.warning(f"Error closing PostgreSQL checkpointer: {e}")
        finally:
            # 无论关闭是否报错，都清空模块级引用，避免后续代码继续用到已失效的连接池
            _pg_checkpointer = None
            _pg_checkpointer_pool = None


async def get_async_checkpointer(thread_id: str | None = None) -> BaseCheckpointSaver[Any]:
    """
    获取 checkpointer 实例（兼容异步调用）

    根据 CHECKPOINT_BACKEND 配置选择后端：
    - "postgres": 优先使用 PostgreSQL（无 16MB 限制）
    - "mongodb": 使用 MongoDB（默认）
    - 都不可用: 回退到 MemorySaver

    Args:
        thread_id: 可选的会话/线程 ID。仅在 MemorySaver fallback 时使用，
            用于按线程复用并限制进程内缓存规模。

    Returns:
        Checkpointer 实例
    """
    backend = getattr(settings, "CHECKPOINT_BACKEND", "mongodb")

    if backend == "postgres":
        logger.info("Using PostgreSQL checkpointer")
        checkpointer = await get_pg_checkpointer()
        if checkpointer is not None:
            return checkpointer
        logger.warning("PostgreSQL checkpointer unavailable, falling back")

    # MongoDB (default)
    logger.info("Using MongoDB checkpointer")
    checkpointer = get_mongo_checkpointer()
    if checkpointer is None:
        logger.warning("MongoDB checkpointer unavailable, falling back")
    if checkpointer is not None:
        # MongoDBSaver 部分版本的 get_next_version 只支持 int 版本号，这里包一层兼容补丁
        return _ensure_string_channel_version_support(checkpointer)

    # MemorySaver fallback
    from langgraph.checkpoint.memory import MemorySaver

    # 有 thread_id 时按线程维度缓存各自独立的 MemorySaver；
    # 缓存字典本身懒挂载在本函数对象的属性上，避免污染模块全局命名空间
    if thread_id:
        if not hasattr(get_async_checkpointer, "_memory_saver_cache"):
            get_async_checkpointer._memory_saver_cache = OrderedDict()  # type: ignore[attr-defined]
            get_async_checkpointer._memory_saver_access_count = 0  # type: ignore[attr-defined]
            logger.warning(
                "Using thread-scoped MemorySaver fallback cache (data will be lost on restart)"
            )

        cache: OrderedDict[str, tuple[MemorySaver, float]] = getattr(
            get_async_checkpointer,
            "_memory_saver_cache",
        )
        access_count = getattr(get_async_checkpointer, "_memory_saver_access_count", 0) + 1
        get_async_checkpointer._memory_saver_access_count = access_count  # type: ignore[attr-defined]
        # 访问计数达到清理间隔时，顺带触发一次过期/超量清理，省去专门起后台任务的开销
        if access_count % _MEMORY_SAVER_CLEANUP_INTERVAL == 0:
            _cleanup_memory_saver_cache()

        now = time.time()
        cached = cache.get(thread_id)
        if cached is not None:
            # 命中缓存：刷新访问时间并移动到 OrderedDict 末尾，标记为最近使用（LRU）
            saver, _ = cached
            cache.move_to_end(thread_id)
            cache[thread_id] = (saver, now)
            return saver

        # 未命中则新建一个 MemorySaver，写入缓存后立即触发一次清理以控制缓存总量
        saver = MemorySaver()
        cache[thread_id] = (saver, now)
        _cleanup_memory_saver_cache(now=now)
        return saver

    # 没有 thread_id 时退化为进程级单例：所有调用方共用同一个 MemorySaver
    if not hasattr(get_async_checkpointer, "_memory_saver"):
        get_async_checkpointer._memory_saver = MemorySaver()  # type: ignore[attr-defined]
        logger.warning("Using MemorySaver singleton (data will be lost on restart)")
    return get_async_checkpointer._memory_saver  # type: ignore[attr-defined]


# 用类名字符串判断消息类型，而不是 isinstance，避免因模块重复加载/循环依赖
# 导致同名类对象不一致而误判
def _is_human_message(message: object) -> bool:
    return type(message).__name__ == "HumanMessage"


def _is_ai_message(message: object) -> bool:
    return type(message).__name__ == "AIMessage"


# 从 checkpoint 的 channel_values 中取出 "messages" 通道的内容，用于统计对话轮次/定位分叉点
def _extract_checkpoint_messages(checkpoint_tuple: CheckpointTuple) -> list[object]:
    checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
    channel_values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
    messages = channel_values.get("messages", [])
    return messages if isinstance(messages, list) else []


# 判断某个历史 checkpoint 是否正好是 fork（对话分叉重新生成）要定位的边界点：
# 1) 该 checkpoint 里的用户消息数（human_count）必须正好等于目标轮次序号 turn_index；
# 2) 且最后一条消息的类型要匹配目标类型
#    （target_type="user" 表示分叉自某次用户提问之后，"assistant" 表示分叉自某次助手回复之后）
def _matches_fork_boundary(
    checkpoint_tuple: CheckpointTuple, *, turn_index: int, target_type: str
) -> bool:
    messages = _extract_checkpoint_messages(checkpoint_tuple)
    human_count = sum(1 for message in messages if _is_human_message(message))
    if human_count != turn_index or not messages:
        return False

    last_message = messages[-1]
    if target_type == "user":
        return _is_human_message(last_message)
    if target_type == "assistant":
        return _is_ai_message(last_message)
    return False


# 深拷贝待写入的 checkpoint/metadata/channel_versions，避免新 thread 与原 thread
# 共享同一份可变对象引用（否则后续任一方修改都会互相影响）。
# 调用方会用 run_blocking_io 把这个深拷贝丢到线程池执行，避免大对象拷贝阻塞事件循环。
def _copy_checkpoint_put_payload(
    checkpoint_tuple: CheckpointTuple,
) -> tuple[Checkpoint, CheckpointMetadata, ChannelVersions]:
    checkpoint = copy.deepcopy(checkpoint_tuple.checkpoint)
    metadata = copy.deepcopy(checkpoint_tuple.metadata)
    channel_versions = copy.deepcopy(checkpoint_tuple.checkpoint.get("channel_versions", {}))
    return checkpoint, metadata, channel_versions


# 从最新的 checkpoint 开始，按页（每页 _FORK_CHECKPOINT_SCAN_PAGE_SIZE 条）向历史回溯扫描，
# 直至找到匹配 fork 边界条件的 checkpoint，或扫描到没有更多历史记录为止。
# 之所以分页扫描而不是一次性拉全部历史，是因为长会话的 checkpoint 数量可能很大。
async def _find_fork_boundary_checkpoint(
    source_saver: BaseCheckpointSaver[Any],
    default_config: RunnableConfig,
    *,
    turn_index: int,
    target_type: str,
) -> CheckpointTuple | None:
    before_config: RunnableConfig | None = None

    while True:
        # 拉取一页 checkpoint（按时间倒序排列，用 before_config 作为翻页游标继续往前翻）
        page = [
            item
            async for item in source_saver.alist(
                default_config,
                before=before_config,
                limit=_FORK_CHECKPOINT_SCAN_PAGE_SIZE,
            )
        ]
        if not page:
            return None

        # 在本页范围内查找匹配边界条件的 checkpoint
        for checkpoint_tuple in page:
            if _matches_fork_boundary(
                checkpoint_tuple,
                turn_index=turn_index,
                target_type=target_type,
            ):
                return checkpoint_tuple

        # 本页没找到，取本页最后一条记录的 config 作为下一页的翻页游标
        last_config = getattr(page[-1], "config", None)
        if not last_config:
            return None
        before_config = last_config


async def clone_checkpoints_for_fork(
    source_thread_id: str,
    target_thread_id: str,
    *,
    turn_index: int,
    target_type: str,
) -> int:
    """Clone checkpoint state up to the fork boundary into a new thread."""
    # fork 场景下 source 和 target 可能落在不同的 thread_id 缓存桶里
    # （尤其是 MemorySaver fallback 时按 thread_id 分别缓存），需要分别获取各自的 checkpointer
    source_saver = await get_async_checkpointer(thread_id=source_thread_id)
    target_saver = await get_async_checkpointer(thread_id=target_thread_id)
    # checkpoint_ns 留空字符串表示使用 LangGraph 的默认命名空间
    default_config: RunnableConfig = {
        "configurable": {"thread_id": source_thread_id, "checkpoint_ns": ""}
    }
    boundary_tuple = await _find_fork_boundary_checkpoint(
        source_saver,
        default_config,
        turn_index=turn_index,
        target_type=target_type,
    )

    # 找不到边界点说明请求的轮次/类型在历史中不存在，直接报错而不是静默创建一个空会话
    if boundary_tuple is None:
        raise ValueError(
            f"Unable to locate fork checkpoint for thread={source_thread_id} turn={turn_index} type={target_type}"
        )

    cfg = boundary_tuple.config["configurable"]
    target_config: RunnableConfig = {
        "configurable": {
            "thread_id": target_thread_id,
            "checkpoint_ns": cfg.get("checkpoint_ns", ""),
        }
    }
    # 深拷贝可能涉及较大的消息历史，丢到线程池执行，避免阻塞事件循环
    checkpoint, metadata, channel_versions = await run_blocking_io(
        _copy_checkpoint_put_payload,
        boundary_tuple,
    )
    # 把复制出来的 checkpoint 写入新 thread，完成"克隆到分叉点"的效果
    await target_saver.aput(
        target_config,
        checkpoint,
        metadata,
        channel_versions,
    )
    return 1


async def seed_checkpoint_from_messages(
    target_thread_id: str,
    messages: list[object],
) -> int:
    """Seed a fork with a minimal message checkpoint when source checkpoints are absent."""
    # 没有消息可播种时直接跳过（比如源会话本身没有任何历史记录）
    if not messages:
        return 0

    target_saver = await get_async_checkpointer(thread_id=target_thread_id)
    # 构造一个空的 LangGraph checkpoint 骨架，再手动填入 messages 通道，
    # 用于在源会话没有可克隆的 checkpoint 时，仍能给新 thread 一个最小可用的起点
    checkpoint = empty_checkpoint()
    # 深拷贝消息列表丢到线程池执行，避免大列表拷贝阻塞事件循环
    copied_messages = await run_blocking_io(copy.deepcopy, messages)
    checkpoint["channel_values"] = {"messages": copied_messages}
    # 手动构造初始版本号，标记 messages 通道已经被更新过一次
    checkpoint["channel_versions"] = {"messages": "1"}
    checkpoint["versions_seen"] = {}
    checkpoint["updated_channels"] = ["messages"]

    # 写入这个"种子" checkpoint 作为新 thread 的起点
    await target_saver.aput(
        {"configurable": {"thread_id": target_thread_id, "checkpoint_ns": ""}},
        checkpoint,
        {"source": "fork", "step": 0},
        checkpoint["channel_versions"],
    )
    return 1


async def delete_checkpoints_for_thread(thread_id: str) -> None:
    """Delete persisted checkpoint state for a LangGraph thread/session."""
    # 没有 thread_id，或持久化后端本身就是关闭状态，都无需执行删除
    if not thread_id or not is_checkpoint_backend_enabled():
        return

    saver = await get_async_checkpointer(thread_id=thread_id)
    # 优先使用异步删除接口
    async_delete = getattr(saver, "adelete_thread", None)
    if callable(async_delete):
        await async_delete(thread_id)
        return

    # 没有异步接口则尝试同步接口，丢到线程池执行；
    # 某些实现的 delete_thread 内部可能仍返回协程，所以这里仍需判断是否要 await
    sync_delete = getattr(saver, "delete_thread", None)
    if callable(sync_delete):
        result = await run_blocking_io(sync_delete, thread_id)
        if inspect.isawaitable(result):
            await result
        return

    # 两种删除接口都不支持，说明该 checkpointer 类型不支持按线程删除
    # （例如 MemorySaver 或某些自定义实现），仅记录告警，不阻断流程
    logger.warning("Checkpointer does not support thread deletion: %s", type(saver).__name__)


def build_messages_from_trace_events(traces: list[dict]) -> list[object]:
    """Build a minimal chat message list from persisted trace events."""
    messages: list[object] = []
    # 按 trace（一轮或一段对话的事件记录）遍历，每个 trace 内部包含多个事件
    for trace in traces:
        assistant_chunks: list[str] = []
        for event in trace.get("events", []):
            event_type = event.get("event_type")
            data = event.get("data") or {}
            # "user:message" 事件对应一条完整的用户消息，直接还原为 HumanMessage
            if event_type == "user:message":
                content = str(data.get("content") or data.get("message") or "")
                if content:
                    messages.append(HumanMessage(content=content))
            # "message:chunk" 是助手回复的流式分片，需要先收集，等这个 trace 结束后再拼接
            elif event_type == "message:chunk":
                content = str(data.get("content") or "")
                if content:
                    assistant_chunks.append(content)

        # 把当前 trace 里所有助手流式分片拼接成一条完整的 AIMessage
        assistant_content = "".join(assistant_chunks)
        if assistant_content:
            messages.append(AIMessage(content=assistant_content))

    return messages

"""
MCP 工具缓存模块（混合缓存实现）

使用 Redis 存储配置哈希值检测变更，使用进程内内存缓存 BaseTool 对象和客户端连接

分布式支持：
- Redis 存储配置哈希， 用于跨实例检测配置变更
- 内存缓存 MCP 连接和工具对象（无法序列化）
- 配置变更时通过 Redis 通知所有实例失效缓存
"""

import asyncio
import hashlib
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Set

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.storage.redis import get_redis_client
from src.kernel.config import settings

logger = get_logger(__name__)

# 缓存过期时间（秒），默认 15 分钟
CACHE_TTL = 900

# 最大缓存条目数（防止内存泄漏）
MAX_CACHE_ENTRIES = 100

# 最大缓存锁数（防止锁泄漏，通常不需要大于缓存条目数）
MAX_CACHE_LOCKS = 200
# 单次 Redis SCAN 遍历配置哈希键的上限，防止 key 过多时无限扫描
MCP_CONFIG_HASH_SCAN_LIMIT = 500

# Redis 缓存键前缀
# Redis 只存"配置哈希"（可序列化），用于跨实例检测配置是否变化
CONFIG_HASH_KEY_PREFIX = "mcp_config_hash:"

# 进程内缓存：user_id -> CachedMCPEntry
# 工具对象与客户端连接无法序列化，故只能进程内内存缓存
_tools_cache: dict[str, "CachedMCPEntry"] = {}

# 缓存锁，防止并发初始化
# 每个用户一把锁：避免同一用户并发请求时重复创建 MCP 客户端（惊群）
_cache_locks: dict[str, asyncio.Lock] = {}

# 全局清理锁，防止并发清理
_cleanup_lock = asyncio.Lock()

# Track deferred client close tasks so shutdown can wait for resources to release.
# 记录延迟关闭客户端的后台任务，优雅停机时可等待其完成（并持有强引用防 GC）
_background_tasks: Set[asyncio.Task] = set()


def _track_background_task(task: asyncio.Task) -> None:
    # 加入集合保持引用，完成后回调自动移除
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _schedule_client_close(client: MultiServerMCPClient) -> None:
    # 异步调度关闭客户端；若当前无事件循环（RuntimeError）则放弃（例如解释器退出阶段）
    try:
        task = asyncio.create_task(_close_client(client))
    except RuntimeError:
        return
    _track_background_task(task)


async def drain_background_tasks(timeout: float = 10.0) -> None:
    """Wait for deferred MCP client close tasks during graceful shutdown."""
    # 停机时等待所有延迟关闭任务结束；超时则告警但不阻塞停机
    if not _background_tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*list(_background_tasks), return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[MCP Cache] %s background close tasks did not finish in %ss",
            len(_background_tasks),
            timeout,
        )


async def _close_client(client: MultiServerMCPClient) -> None:
    """Close MCP client connections."""
    # 尽力关闭客户端：兼容同步/异步 close() 与异步上下文 __aexit__()
    try:
        # MultiServerMCPClient may have cleanup methods
        if hasattr(client, "close"):
            result = client.close()
            if inspect.isawaitable(result):
                await result
        elif hasattr(client, "__aexit__"):
            result = client.__aexit__(None, None, None)  # type: ignore[func-returns-value]
            if inspect.isawaitable(result):
                await result
    except Exception as e:
        # 关闭失败仅调试日志，不影响主流程
        logger.debug(f"Error closing MCP client: {e}")


def _get_cache_ttl() -> int:
    # 读取有效缓存 TTL（秒）：优先配置项，回退默认值，且至少 1 秒
    return max(int(getattr(settings, "MCP_USER_CACHE_TTL_SECONDS", CACHE_TTL) or 0), 1)


def _get_max_cache_entries() -> int:
    # 读取有效最大缓存条目数：优先配置项，回退默认值，且至少 1
    return max(int(getattr(settings, "MCP_USER_CACHE_MAX_ENTRIES", MAX_CACHE_ENTRIES) or 0), 1)


async def _scan_config_hash_keys(redis_client, *, limit: int | None = None):
    # 用 SCAN（而非 KEYS）分批遍历配置哈希键，避免大 key 空间下阻塞 Redis
    if limit is None:
        limit = MCP_CONFIG_HASH_SCAN_LIMIT
    cursor = 0
    keys = []
    while True:
        cursor, batch = await redis_client.scan(
            cursor=cursor,
            match=f"{CONFIG_HASH_KEY_PREFIX}*",
            count=100,
        )
        for key in batch:
            keys.append(key)
            # 达到上限即提前返回，防止 key 过多导致扫描无界
            if len(keys) >= limit:
                logger.warning("[MCP Cache] Redis hash scan limit reached: %s", limit)
                return keys
        # cursor 归零表示遍历完成
        if cursor == 0:
            return keys


def _remove_lock_if_idle(user_id: str) -> bool:
    """Remove a cached lock only when it is currently idle."""
    # 仅当锁未被持有时才移除，避免误删正在使用中的锁造成并发保护失效
    lock = _cache_locks.get(user_id)
    if lock is None or lock.locked():
        return False
    _cache_locks.pop(user_id, None)
    return True


def _cleanup_expired_cache() -> int:
    """清理过期的缓存条目，返回清理的数量"""
    # 收集并逐个移除过期条目，同时异步关闭其客户端连接
    expired_users = [user_id for user_id, entry in _tools_cache.items() if entry.is_expired()]
    for user_id in expired_users:
        entry = _tools_cache.pop(user_id, None)
        if entry and entry.client:
            _schedule_client_close(entry.client)
        _remove_lock_if_idle(user_id)
    # 清理没有对应缓存条目的孤立 lock
    orphan_locks = [uid for uid in _cache_locks if uid not in _tools_cache]
    for uid in orphan_locks:
        _remove_lock_if_idle(uid)
    return len(expired_users)


def _cleanup_excess_cache() -> int:
    """清理超出的缓存条目（LRU），返回清理的数量"""
    max_entries = _get_max_cache_entries()
    if len(_tools_cache) <= max_entries:
        return 0

    # 按最后访问时间排序，删除最旧的
    # LRU 淘汰：last_access 越早越先被淘汰
    sorted_entries = sorted(_tools_cache.items(), key=lambda x: x[1].last_access)

    # 删除超出部分
    to_remove = len(_tools_cache) - max_entries
    for user_id, entry in sorted_entries[:to_remove]:
        _tools_cache.pop(user_id, None)
        if entry and entry.client:
            _schedule_client_close(entry.client)
        _remove_lock_if_idle(user_id)

    # 清理没有对应缓存条目的孤立 lock
    orphan_locks = [uid for uid in _cache_locks if uid not in _tools_cache]
    for uid in orphan_locks:
        _remove_lock_if_idle(uid)

    return to_remove


@dataclass
class CachedMCPEntry:
    """缓存的 MCP 工具条目（进程内）"""

    tools: list[BaseTool]
    client: MultiServerMCPClient
    # config_hash：生成这批工具时所用配置的哈希，用于命中判断
    config_hash: str
    # created_at 用于 TTL 过期；last_access 用于 LRU 淘汰
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)

    def is_expired(self, ttl: float | None = None) -> bool:
        """检查缓存是否过期"""
        # 以创建时间为基准判断是否超过 TTL
        if ttl is None:
            ttl = _get_cache_ttl()
        return time.time() - self.created_at > ttl

    def touch(self):
        """更新最后访问时间"""
        self.last_access = time.time()


def _get_cache_lock(user_id: str) -> asyncio.Lock:
    """获取指定用户的缓存锁（线程安全）

    使用 setdefault 确保原子性，防止竞态条件。
    同时定期清理过期和超出限制的缓存条目。
    """
    # 定期清理过期条目（简单触发机制）
    # 借"获取锁"这一高频入口顺带做惰性清理，省去独立后台定时器
    if len(_tools_cache) > 0 and len(_tools_cache) % 50 == 0:
        expired = _cleanup_expired_cache()
        if expired > 0:
            logger.debug(f"[MCP Cache] Auto-cleaned {expired} expired entries")

    # 检查是否超出最大条目数
    if len(_tools_cache) > _get_max_cache_entries():
        removed = _cleanup_excess_cache()
        if removed > 0:
            logger.info(f"[MCP Cache] Removed {removed} excess cache entries (LRU)")

    # 清理孤立的锁，防止 _cache_locks 无限增长
    if len(_cache_locks) > MAX_CACHE_LOCKS:
        orphan_locks = [uid for uid in _cache_locks if uid not in _tools_cache]
        for uid in orphan_locks:
            _remove_lock_if_idle(uid)
        if len(_cache_locks) > MAX_CACHE_LOCKS:
            logger.debug(
                "[MCP Cache] %s cache locks remain after cleanup (some may be in use)",
                len(_cache_locks),
            )

    # 使用 setdefault 确保原子性
    # setdefault 在单事件循环内是原子的，保证同一 user_id 只会有一把锁
    return _cache_locks.setdefault(user_id, asyncio.Lock())


def compute_config_hash(config: dict) -> str:
    """
    计算配置的哈希值，用于检测配置是否变更

    Args:
        config: MCP 配置字典，包含 mcpServers 等

    Returns:
        配置的 MD5 哈希值
    """
    # 提取 mcpServers 部分
    # 只对 mcpServers 计算指纹，其余无关字段变化不触发缓存失效
    servers = config.get("mcpServers", {})

    # 排序键以确保一致性
    # sort_keys 保证语义相同的配置得到稳定哈希；MD5 仅用于比较，非安全用途
    config_str = json.dumps(servers, sort_keys=True, default=str)
    return hashlib.md5(config_str.encode()).hexdigest()


async def _get_stored_config_hash(user_id: str) -> Optional[str]:
    """从 Redis 获取存储的配置哈希"""
    # Redis 故障不应影响主流程，失败返回 None（等价于"缓存不可信"）
    try:
        redis_client = get_redis_client()
        key = f"{CONFIG_HASH_KEY_PREFIX}{user_id}"
        return await redis_client.get(key)
    except Exception as e:
        logger.warning(f"[MCP Cache] Redis get hash failed for user {user_id}: {e}")
        return None


async def _store_config_hash(user_id: str, config_hash: str) -> None:
    """存储配置哈希到 Redis"""
    # 写入带 TTL 的配置哈希；失败仅告警，不影响本地缓存的建立
    try:
        redis_client = get_redis_client()
        key = f"{CONFIG_HASH_KEY_PREFIX}{user_id}"
        await redis_client.set(key, config_hash, ex=_get_cache_ttl())
    except Exception as e:
        logger.warning(f"[MCP Cache] Redis set hash failed for user {user_id}: {e}")


async def get_cached_tools(
    user_id: str,
    config: dict,
    create_client_func,
) -> tuple[list[BaseTool], Optional[MultiServerMCPClient]]:
    """
    获取缓存的 MCP 工具（混合缓存策略）

    1. 计算当前配置的哈希值
    2. 从 Redis 获取存储的配置哈希
    3. 如果哈希匹配且进程内有缓存，直接返回
    4. 否则重新创建工具并更新缓存

    Args:
        user_id: 用户 ID
        config: MCP 配置字典
        create_client_func: 异步函数，用于创建新的 MCP 客户端和工具
            签名: async def create_client(config: dict) -> tuple[list[BaseTool], MultiServerMCPClient]

    Returns:
        tuple: (tools, client) - 工具列表和客户端
    """
    # 先在锁外计算配置哈希（可能较慢），再取用户级锁串行化后续创建流程
    current_hash = await run_blocking_io(compute_config_hash, config)
    lock = _get_cache_lock(user_id)

    # 持用户锁：确保同一用户并发请求只创建一次客户端，其余复用
    async with lock:
        # 获取 Redis 中存储的配置哈希
        stored_hash = await _get_stored_config_hash(user_id)

        # 检查进程内缓存
        cached = _tools_cache.get(user_id)

        # 判断是否可以使用缓存
        if cached and not cached.is_expired():
            # 检查配置是否变更
            # 三重一致才算命中：Redis 哈希、缓存条目哈希、当前哈希都相等
            # （Redis 哈希用于捕捉"其他实例改了配置"的跨实例失效）
            if stored_hash == current_hash and cached.config_hash == current_hash:
                # 配置未变更，检查工具列表是否有效
                if len(cached.tools) > 0:
                    # 有工具，使用缓存
                    cached.touch()
                    logger.info(
                        f"[MCP Cache] Hit for user {user_id}, {len(cached.tools)} tools "
                        f"(hash matched)"
                    )
                    return cached.tools, cached.client
                else:
                    # 缓存的工具列表为空，可能是之前创建失败，需要重新创建
                    # 空工具不作为有效命中，避免把上次失败结果长期缓存
                    logger.info(f"[MCP Cache] Empty tools cache for user {user_id}, will recreate")
            else:
                # 配置已变更，需要重新加载
                logger.info(
                    f"[MCP Cache] Config changed for user {user_id}, "
                    f"stored_hash={stored_hash[:8] if stored_hash else 'None'}, "
                    f"current_hash={current_hash[:8]}"
                )
        else:
            # 缓存过期或不存在
            logger.info(f"[MCP Cache] Miss for user {user_id} (no valid cache)")

        # 重新创建工具
        # 调用方传入的工厂函数负责真正建立连接并加载工具
        logger.info(f"[MCP Cache] Creating tools for user {user_id}")
        tools, client = await create_client_func(config)

        old_cached = _tools_cache.get(user_id)

        # 更新进程内缓存
        _tools_cache[user_id] = CachedMCPEntry(
            tools=tools,
            client=client,
            config_hash=current_hash,
        )
        # 若旧缓存持有不同的客户端，关闭它以释放旧连接
        if old_cached and old_cached.client is not client:
            await _close_client(old_cached.client)

        # 更新 Redis 中的配置哈希
        # 回写新哈希，供其他实例据此判断是否需要失效
        await _store_config_hash(user_id, current_hash)

        logger.info(f"[MCP Cache] Cached {len(tools)} tools for user {user_id}")
        return tools, client


async def invalidate_user_cache(user_id: str) -> bool:
    """
    使指定用户的缓存失效

    同时清除 Redis 配置哈希和进程内缓存

    Args:
        user_id: 用户 ID

    Returns:
        bool: 是否成功删除缓存
    """
    # 清除 Redis 配置哈希
    # 删除 Redis 哈希会让其他实例在下次比对时判定为"配置变更"从而各自失效
    try:
        redis_client = get_redis_client()
        key = f"{CONFIG_HASH_KEY_PREFIX}{user_id}"
        await redis_client.delete(key)
        logger.info(f"[MCP Cache] Invalidated Redis hash for user {user_id}")
    except Exception as e:
        logger.warning(f"[MCP Cache] Redis delete hash failed for user {user_id}: {e}")

    # 清除进程内缓存
    had_cache = user_id in _tools_cache
    if had_cache:
        cached = _tools_cache.pop(user_id)
        if cached.client:
            # 同步关闭本地客户端，尽快释放连接
            try:
                await _close_client(cached.client)
            except Exception as e:
                logger.debug(f"[MCP Cache] Error closing client for {user_id}: {e}")
        logger.info(
            f"[MCP Cache] Invalidated memory cache for user {user_id}, {len(cached.tools)} tools"
        )
    # 无论缓存是否存在，都清理对应的 lock（防止孤立）
    _remove_lock_if_idle(user_id)
    return had_cache


async def invalidate_all_cache() -> int:
    """
    使所有用户的缓存失效

    Returns:
        int: 被失效的缓存数量
    """
    # 清除 Redis 中所有配置哈希（使用 SCAN 代替 KEYS 避免阻塞）
    try:
        redis_client = get_redis_client()
        all_keys = await _scan_config_hash_keys(redis_client)
        if all_keys:
            await redis_client.delete(*all_keys)
            logger.info(f"[MCP Cache] Invalidated {len(all_keys)} Redis hash entries")
    except Exception as e:
        logger.warning(f"[MCP Cache] Redis keys/delete failed: {e}")

    # 清除所有进程内缓存
    # 先快照再清空，随后逐个关闭客户端连接
    count = len(_tools_cache)
    cached_entries = list(_tools_cache.values())
    _tools_cache.clear()
    for cached in cached_entries:
        if cached.client:
            try:
                await _close_client(cached.client)
            except Exception as e:
                logger.debug(f"[MCP Cache] Error closing client during full invalidation: {e}")
    # 清理全部空闲锁
    for user_id in list(_cache_locks):
        _remove_lock_if_idle(user_id)
    logger.info(f"[MCP Cache] Invalidated all memory cache, {count} entries")
    return count


async def get_cache_stats() -> dict[str, Any]:
    """
    获取缓存统计信息

    Returns:
        dict: 包含缓存统计的字典
    """
    # 汇总内存缓存与 Redis 哈希键数量，供监控/诊断
    now = time.time()
    stats: dict[str, Any] = {
        "memory_cache": {
            "total_entries": len(_tools_cache),
            "entries": [],
        },
        "redis_hash_keys": 0,
    }

    # 内存缓存统计
    for user_id, cached in _tools_cache.items():
        stats["memory_cache"]["entries"].append(
            {
                "user_id": user_id,
                "tools_count": len(cached.tools),
                "age_seconds": int(now - cached.created_at),
                "is_expired": cached.is_expired(),
                "config_hash": cached.config_hash[:8],
            }
        )

    # Redis 哈希键统计（使用 SCAN 代替 KEYS 避免阻塞）
    try:
        redis_client = get_redis_client()
        stats["redis_hash_keys"] = len(await _scan_config_hash_keys(redis_client))
    except Exception as e:
        # Redis 不可用时记录错误信息但不抛出
        stats["redis_hash_error"] = str(e)

    return stats

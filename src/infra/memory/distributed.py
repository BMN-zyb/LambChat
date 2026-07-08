"""
Distributed Memory Support - Redis pub/sub for cache invalidation + distributed locks.

When a memory is modified on one instance, this publishes a Redis message so
other instances invalidate their local index cache.  A Redis-based distributed
lock prevents concurrent consolidation across instances.

Follows the same pub/sub pattern as SettingsPubSub.
"""

import json
import uuid
from typing import Any, Dict, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.pubsub_hub import get_pubsub_hub
from src.infra.storage.redis import get_redis_client

logger = get_logger(__name__)

# Lua script: only delete lock key if value matches instance_id (prevents releasing another instance's lock)
# 用 EVAL 原子执行"比较并删除"：只有 key 里存的值仍然是自己的 instance_id 才会删除。
# 这避免了以下竞态：本实例的锁因为 TTL 已经过期，被另一个实例重新抢到之后，
# 本实例才迟到执行 release，如果不做比较就直接 DEL，会误删掉别的实例刚抢到的锁。
_RELEASE_LOCK_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

# Redis channel for memory cache invalidation
MEMORY_INVALIDATION_CHANNEL = "memory:invalidated"

# Distributed lock keys
# 按 user_id 维度加锁：同一用户的记忆合并（consolidation）在整个集群中最多只有一个实例在跑，
# 避免并发合并导致重复/冲突的记忆写入；TTL 是"意外情况下锁最长占用多久会被强制释放"的兜底。
CONSOLIDATION_LOCK_KEY = "memory:consolidation_lock:{user_id}"
CONSOLIDATION_LOCK_TTL = 120  # seconds
# 全局唯一的一把锁（不区分用户），用于周期性的记忆压缩扫描任务：
# 整个集群只需要一个实例执行这一轮扫描，其余实例应当直接跳过。
COMPACTION_SCAN_LOCK_KEY = "memory:compaction_scan_lock"
# 压缩冷却标记（按用户维度）：某用户刚被压缩过之后，短期内无需再次触发压缩；
# 用一个带 TTL 的 key 是否存在来表示"是否还在冷却期"，无需额外维护"冷却结束时间"字段。
COMPACTION_COOLDOWN_KEY = "memory:compaction_cooldown:{user_id}"
# 自动记忆抓取（从对话中自动提炼记忆）按用户加锁；TTL 比 consolidation 锁短很多，
# 因为该操作预期很快完成，短 TTL 能让意外卡死的锁更快自动释放。
AUTO_CAPTURE_LOCK_KEY = "memory:auto_capture_lock:{user_id}"
AUTO_CAPTURE_LOCK_TTL = 30  # seconds

# ============================================================================
# Publisher helpers (called from NativeMemoryBackend)
# ============================================================================


async def publish_memory_invalidation(user_id: str) -> None:
    """Publish a cache invalidation message for a user.

    Called after retain, delete, and consolidate_memories so other instances
    drop stale cache entries.
    """
    # 失败只记录 debug 级别日志并吞掉异常：缓存失效通知属于"尽力而为"的优化，
    # Redis 抖动导致个别通知丢失，最多是其他实例的本地缓存多存活一段时间，
    # 不应该因为这个可选优化的失败而影响记忆写入的主流程。
    try:
        redis_client = get_redis_client()
        payload = await run_blocking_io(json.dumps, {"user_id": user_id})
        await redis_client.publish(
            MEMORY_INVALIDATION_CHANNEL,
            payload,
        )
    except Exception as e:
        logger.debug("[Memory] Failed to publish invalidation for %s: %s", user_id, e)


# ============================================================================
# Distributed lock for consolidation
# ============================================================================


async def acquire_consolidation_lock(user_id: str, instance_id: str) -> str:
    """Try to acquire a distributed lock for memory consolidation.

    Uses Redis SETNX with TTL.

    Returns one of:
    - "acquired": this instance owns the lock
    - "not_acquired": another instance already owns the lock
    - "unavailable": lock state could not be determined
    """
    # 用 SETNX（Redis SET ... NX EX）实现最基础的分布式锁：只有 key 不存在时才能设置成功，
    # 天然具备"同一时刻只有一个实例能拿到锁"的互斥性；EX 保证即使持有者进程崩溃忘记释放，
    # 锁也会在 TTL 后自动消失，不会永久卡死整个集群的该操作。
    try:
        redis_client = get_redis_client()
        lock_key = CONSOLIDATION_LOCK_KEY.format(user_id=user_id)
        acquired = await redis_client.set(lock_key, instance_id, nx=True, ex=CONSOLIDATION_LOCK_TTL)
        return "acquired" if acquired else "not_acquired"
    except Exception as e:
        logger.debug("[Memory] Failed to acquire consolidation lock for %s: %s", user_id, e)
        return "unavailable"


async def release_consolidation_lock(user_id: str, instance_id: str) -> None:
    """Release the consolidation lock (only if we own it)."""
    # 用上面的 Lua 脚本做"比较并删除"，避免误删已经因 TTL 过期被别的实例重新抢到的锁
    try:
        redis_client = get_redis_client()
        lock_key = CONSOLIDATION_LOCK_KEY.format(user_id=user_id)
        await redis_client.eval(_RELEASE_LOCK_LUA, 1, lock_key, instance_id)  # type: ignore[misc]
    except Exception as e:
        logger.debug("[Memory] Failed to release consolidation lock for %s: %s", user_id, e)


async def acquire_compaction_scan_lock(instance_id: str, ttl_seconds: int) -> str:
    """Acquire a cluster-wide scan lease for periodic memory compaction.

    This intentionally behaves like a TTL lease rather than a short critical-section
    lock. The winner keeps the lease until TTL expiration so other instances do not
    immediately run the same periodic scan after the first one finishes.
    """
    # 与 acquire_consolidation_lock 用的是同一种 SETNX+TTL 机制，区别只在于语义：
    # 这里更像一个"租约"而不是临界区锁——赢家会一直持有直到 TTL 到期，
    # 其它实例在此期间都应放弃本轮扫描，而不是等赢家做完就立刻抢占开始下一轮。
    try:
        redis_client = get_redis_client()
        ttl = max(60, int(ttl_seconds))
        acquired = await redis_client.set(
            COMPACTION_SCAN_LOCK_KEY,
            instance_id,
            nx=True,
            ex=ttl,
        )
        return "acquired" if acquired else "not_acquired"
    except Exception as e:
        logger.debug("[Memory] Failed to acquire compaction scan lock: %s", e)
        return "unavailable"


async def get_compaction_cooldown_state(user_id: str) -> str:
    """Check whether a user is in distributed compaction cooldown.

    Returns one of:
    - "active": cooldown key exists
    - "clear": no cooldown key exists
    - "unavailable": Redis state could not be determined
    """
    # 只关心 key 是否存在，不关心其值或剩余 TTL——冷却期的具体时长完全由写入时的
    # EX 决定（见 mark_compaction_cooldown），到期后 Redis 会自动删除该 key
    try:
        redis_client = get_redis_client()
        key = COMPACTION_COOLDOWN_KEY.format(user_id=user_id)
        active = await redis_client.exists(key)
        return "active" if active else "clear"
    except Exception as e:
        logger.debug("[Memory] Failed to read compaction cooldown for %s: %s", user_id, e)
        return "unavailable"


async def mark_compaction_cooldown(user_id: str, ttl_seconds: int) -> str:
    """Mark a user's compaction cooldown with a Redis TTL."""
    # ttl_seconds<=0 视为"禁用冷却"，直接跳过、不写入任何 key
    # （对应 get_compaction_cooldown_state 会读到 "clear"）
    if ttl_seconds <= 0:
        return "disabled"
    try:
        redis_client = get_redis_client()
        key = COMPACTION_COOLDOWN_KEY.format(user_id=user_id)
        await redis_client.set(key, "1", ex=max(1, int(ttl_seconds)))
        return "marked"
    except Exception as e:
        logger.debug("[Memory] Failed to mark compaction cooldown for %s: %s", user_id, e)
        return "unavailable"


async def acquire_auto_capture_lock(user_id: str, instance_id: str) -> str:
    """Try to acquire a distributed lock for background auto memory capture."""
    # 与 consolidation 锁是完全独立的一把锁（不同的 key 前缀）：
    # "自动抓取记忆"和"合并记忆"是两个可能同时运行、互不冲突的操作，各自去重即可，
    # 不需要共享同一把锁
    try:
        redis_client = get_redis_client()
        lock_key = AUTO_CAPTURE_LOCK_KEY.format(user_id=user_id)
        acquired = await redis_client.set(lock_key, instance_id, nx=True, ex=AUTO_CAPTURE_LOCK_TTL)
        return "acquired" if acquired else "not_acquired"
    except Exception as e:
        logger.debug("[Memory] Failed to acquire auto-capture lock for %s: %s", user_id, e)
        return "unavailable"


async def release_auto_capture_lock(user_id: str, instance_id: str) -> None:
    """Release the auto-capture lock (only if we own it)."""
    # 同样通过 Lua 脚本做"比较并删除"，避免释放掉已被其他实例重新抢到的锁
    try:
        redis_client = get_redis_client()
        lock_key = AUTO_CAPTURE_LOCK_KEY.format(user_id=user_id)
        await redis_client.eval(_RELEASE_LOCK_LUA, 1, lock_key, instance_id)  # type: ignore[misc]
    except Exception as e:
        logger.debug("[Memory] Failed to release auto-capture lock for %s: %s", user_id, e)


# ============================================================================
# Pub/Sub Listener
# ============================================================================


class MemoryPubSub:
    """Redis Pub/Sub listener for memory cache invalidation events.

    When another instance modifies a user's memories, this listener
    invalidates the local index cache for that user.
    """

    def __init__(self):
        self._subscription_token: Optional[str] = None
        self._running = False
        # 进程实例 ID，结构与 SettingsPubSub 一致；这里不用它来过滤自己发布的消息
        # （见 _handle_message 的说明），仅保留用于日志标识
        self._instance_id: str = uuid.uuid4().hex[:8]

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def start_listener(self) -> None:
        """Start listening for memory invalidation notifications."""
        if self._running:
            return

        hub = get_pubsub_hub()
        self._subscription_token = hub.subscribe(
            MEMORY_INVALIDATION_CHANNEL,
            self._handle_message,
        )
        await hub.start()
        self._running = True
        logger.info(
            "[MemoryPubSub] Listening on channel: %s (instance=%s)",
            MEMORY_INVALIDATION_CHANNEL,
            self._instance_id,
        )

    async def _handle_message(self, message: Dict[str, Any]) -> None:
        """Invalidate local index cache for the user mentioned in the message."""
        # 注意：这里不像 SettingsPubSub 那样跳过"自己发布的消息"——
        # 让本实例也把自己的本地索引缓存失效一次是无害的空操作（顶多下次访问多一次缓存未命中），
        # 因此不需要额外用 instance_id 过滤，实现可以更简单。
        try:
            data = await run_blocking_io(json.loads, message["data"])
            user_id = data.get("user_id")
            if not user_id:
                return

            from src.infra.memory.tools import _get_backend

            # 只有当前生效的记忆后端是 native（自带本地索引缓存的实现）时才需要处理失效通知
            backend = await _get_backend()
            if backend is None or backend.name != "native":
                return

            from src.infra.memory.client.native import NativeMemoryBackend

            if not isinstance(backend, NativeMemoryBackend):
                return
            # Invalidate the index cache for this user
            # 直接从本地索引缓存中移除该用户的条目，下次访问时会触发重新从存储层加载最新数据
            backend._index_cache.pop(user_id, None)
            logger.debug("[MemoryPubSub] Invalidated index cache for user %s", user_id)

        except Exception as e:
            logger.debug("[MemoryPubSub] Error handling message: %s", e)

    async def stop_listener(self) -> None:
        """Stop the memory pub/sub listener."""
        self._running = False

        if self._subscription_token:
            hub = get_pubsub_hub()
            hub.unsubscribe(self._subscription_token)
            self._subscription_token = None
            await hub.stop_if_idle()

    @property
    def is_running(self) -> bool:
        return self._running


# Singleton instance
# 进程级单例：整个进程只需要一个记忆缓存失效监听器
_memory_pubsub: Optional[MemoryPubSub] = None


def get_memory_pubsub() -> MemoryPubSub:
    """Get the global MemoryPubSub instance."""
    global _memory_pubsub
    if _memory_pubsub is None:
        _memory_pubsub = MemoryPubSub()
    return _memory_pubsub


async def close_memory_pubsub() -> None:
    """Stop and release the global MemoryPubSub instance if it exists."""
    global _memory_pubsub
    pubsub = _memory_pubsub
    # 先取出并清空单例引用，再停止，避免停止过程中其他协程仍拿到即将失效的实例
    _memory_pubsub = None
    if pubsub is not None:
        await pubsub.stop_listener()

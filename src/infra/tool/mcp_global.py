"""
全局 MCP 管理器 - 分布式优化版（安全锁 + 内存管理 + Pub/Sub 缓存同步）

使用全局单例 + Redis 分布式锁（Lua 脚本），避免重复初始化。
使用 Redis Pub/Sub 实现跨实例缓存失效通知。
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Set

from langchain_core.tools import BaseTool

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.pubsub_hub import get_pubsub_hub
from src.infra.storage.redis import get_redis_client
from src.kernel.config import settings

if TYPE_CHECKING:
    from src.infra.tool.mcp_client import MCPClientManager

logger = get_logger(__name__)

# 全局单例：user_id -> GlobalMCPEntry
# 进程内每用户一份 MCP 管理器+工具的缓存，避免重复初始化
_global_entries: dict[str, "GlobalMCPEntry"] = {}

# 本地异步锁（进程内）
# 每用户一把本地锁：先在进程内串行化，减少对 Redis 分布式锁的争用
_local_locks: dict[str, asyncio.Lock] = {}

# 后台任务追踪集合
# 持有锁续约、管理器关闭等后台任务的强引用，防止被 GC 提前回收
_background_tasks: Set[asyncio.Future] = set()

# 清理计数器（用于定期清理检查）
_cleanup_counter = 0

# 清理检查间隔（每 N 次访问检查一次）
CLEANUP_CHECK_INTERVAL = 50

# 分布式锁超时时间（秒）
DISTRIBUTED_LOCK_TTL = 30

# 全局缓存过期时间（秒），默认 15 分钟
GLOBAL_CACHE_TTL = 900

# 最大缓存条目数（防止内存泄漏）
MAX_GLOBAL_ENTRIES = 100
# 等待其他实例完成初始化的默认最长秒数
DEFAULT_INIT_WAIT_SECONDS = 5

# Redis 键前缀
# 初始化互斥锁键前缀（跨实例）
LOCK_KEY_PREFIX = "mcp_init_lock:"
# "初始化已完成"标记键前缀，供等待方快速得知无需再抢锁
DONE_KEY_PREFIX = "mcp_init_done:"

# MCP 缓存失效 Pub/Sub 频道
MCP_CACHE_INVALIDATE_CHANNEL = "mcp:cache:invalidate"

# 当前实例的唯一标识（用于避免处理自己发送的消息）
_INSTANCE_ID = str(uuid.uuid4())[:8]

# Lua 脚本：安全释放锁
# 用 GET+DEL 的原子 Lua 脚本，仅当锁值等于自己持有的 value 才删除，
# 防止误删已被其他实例重新获取的同名锁（经典分布式锁陷阱）
RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# 同理，仅当仍是锁持有者时才续期（EXPIRE），避免给别人的锁续命
RENEW_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class MCPGlobalCachePubSub:
    """Listen for distributed MCP cache invalidation messages."""

    def __init__(self) -> None:
        self._subscription_token: str | None = None
        self._running = False
        # 复用模块级实例 ID，用于过滤自己发出的失效消息
        self._instance_id = _INSTANCE_ID

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def start_listener(self) -> None:
        # 幂等启动：订阅失效频道并启动 hub
        if self._running:
            return

        hub = get_pubsub_hub()
        self._subscription_token = hub.subscribe(MCP_CACHE_INVALIDATE_CHANNEL, self._handle_message)
        await hub.start()
        self._running = True
        logger.info(
            "[Global MCP] Cache invalidation listener started on %s (instance=%s)",
            MCP_CACHE_INVALIDATE_CHANNEL,
            self._instance_id,
        )

    async def stop_listener(self) -> None:
        # 停止监听并在 hub 空闲时释放
        self._running = False
        if self._subscription_token:
            hub = get_pubsub_hub()
            hub.unsubscribe(self._subscription_token)
            self._subscription_token = None
            await hub.stop_if_idle()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        # 处理跨实例失效广播：按 scope 决定是失效单用户还是全部
        try:
            data = await run_blocking_io(json.loads, message["data"])
            # 忽略自己发出的消息（本地已就地失效）
            if data.get("instance_id") == self._instance_id:
                return

            scope = data.get("scope")
            if scope == "all":
                # publish=False：本次只做本地失效，避免消息风暴（无限转发）
                await invalidate_all_global_cache(publish=False)
                return

            user_id = data.get("user_id")
            if scope == "user" and user_id:
                await invalidate_global_cache(user_id, publish=False)
        except Exception as e:
            logger.warning("[Global MCP] Failed to handle distributed invalidation: %s", e)

    @property
    def is_running(self) -> bool:
        return self._running


# 模块级单例
_mcp_cache_pubsub: MCPGlobalCachePubSub | None = None


def get_mcp_cache_pubsub() -> MCPGlobalCachePubSub:
    # 惰性创建并返回失效监听单例
    global _mcp_cache_pubsub
    if _mcp_cache_pubsub is None:
        _mcp_cache_pubsub = MCPGlobalCachePubSub()
    return _mcp_cache_pubsub


async def close_mcp_cache_pubsub() -> None:
    """Stop and release the MCP cache pub/sub singleton without creating it."""
    # 停止并释放单例；先取后置空，避免关闭动作反而创建新实例
    global _mcp_cache_pubsub
    pubsub = _mcp_cache_pubsub
    _mcp_cache_pubsub = None
    if pubsub is not None:
        await pubsub.stop_listener()


@dataclass
class GlobalMCPEntry:
    """全局 MCP 缓存条目"""

    manager: "MCPClientManager"
    tools: list[BaseTool]
    # created_at 用于 TTL 过期；last_access 用于 LRU 淘汰
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)

    def is_expired(self, ttl: float | None = None) -> bool:
        """检查缓存是否过期"""
        if ttl is None:
            ttl = _get_global_cache_ttl()
        return time.time() - self.created_at > ttl

    def touch(self):
        """更新最后访问时间"""
        self.last_access = time.time()


def _get_local_lock(user_id: str) -> asyncio.Lock:
    """获取本地异步锁（带容量保护）"""
    # 如果锁数超过最大缓存条目的 2 倍，先清理孤儿锁
    # 防止大量一次性 user_id 让 _local_locks 无限增长
    if len(_local_locks) > _get_max_global_entries() * 2:
        _cleanup_orphan_locks()
    # setdefault 保证同一 user_id 只会有一把锁（单事件循环内原子）
    return _local_locks.setdefault(user_id, asyncio.Lock())


def _get_global_cache_ttl() -> int:
    # 读取有效全局缓存 TTL，优先配置项，回退默认值，至少 1 秒
    return max(int(getattr(settings, "MCP_GLOBAL_CACHE_TTL_SECONDS", GLOBAL_CACHE_TTL) or 0), 1)


def _get_max_global_entries() -> int:
    # 读取有效最大缓存条目数，优先配置项，回退默认值，至少 1
    return max(int(getattr(settings, "MCP_GLOBAL_MAX_ENTRIES", MAX_GLOBAL_ENTRIES) or 0), 1)


def _get_global_warmup_max_users() -> int:
    # 预热时最多处理的用户数上限，非法值回退 100
    value = getattr(settings, "MCP_GLOBAL_WARMUP_MAX_USERS", 100)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 100


def _get_global_init_wait_seconds() -> int:
    # 等待其他实例完成初始化的最长秒数，非法值回退默认
    value = getattr(settings, "MCP_GLOBAL_INIT_WAIT_SECONDS", DEFAULT_INIT_WAIT_SECONDS)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_INIT_WAIT_SECONDS


def _cleanup_orphan_locks() -> int:
    """Clean up local locks that have no cache entry and are not in use."""
    # 孤儿锁：无对应缓存条目的锁；仅清理当前未被持有的，避免破坏正在进行的临界区
    orphan_locks = [uid for uid in list(_local_locks) if uid not in _global_entries]
    removed = 0
    for uid in orphan_locks:
        lock = _local_locks.get(uid)
        if lock is None or lock.locked():
            continue
        _local_locks.pop(uid, None)
        removed += 1
    return removed


async def drain_background_tasks(timeout: float = 10.0) -> None:
    """等待所有后台关闭任务完成（用于优雅停机）"""
    if not _background_tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*list(_background_tasks), return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[Global MCP] {len(_background_tasks)} background tasks did not finish in {timeout}s"
        )


async def acquire_distributed_lock(
    lock_key: str, ttl: int = DISTRIBUTED_LOCK_TTL
) -> tuple[bool, str]:
    """
    获取 Redis 分布式锁

    Args:
        lock_key: 锁的键
        ttl: 锁的超时时间（秒）

    Returns:
        (是否成功获取锁, 锁的唯一标识)
    """
    # 生成随机 value 作为持有凭证，后续释放/续期都要校验它，防止误操作他人的锁
    lock_value = str(uuid.uuid4())
    try:
        redis_client = get_redis_client()
        # 使用 SET NX EX 原子操作
        # NX 保证仅在键不存在时设置；EX 设过期，避免持锁进程崩溃后死锁
        result = await redis_client.set(lock_key, lock_value, nx=True, ex=ttl)
        if result is not None:
            logger.debug(f"[Global MCP] Acquired lock: {lock_key}")
            return True, lock_value
        return False, ""
    except Exception as e:
        # Redis 异常时视为获取失败（后续会走降级路径），不抛出
        logger.warning(f"[Global MCP] Failed to acquire lock {lock_key}: {e}")
        return False, ""


async def release_distributed_lock(lock_key: str, lock_value: str) -> bool:
    """
    释放 Redis 分布式锁（只有持有者才能释放）

    使用 Lua 脚本确保原子性，防止误删其他实例的锁。

    Args:
        lock_key: 锁的键
        lock_value: 锁的唯一标识（获取锁时返回的）

    Returns:
        是否成功释放锁
    """
    try:
        redis_client = get_redis_client()
        # Redis eval 参数: (script, numkeys, *keys_and_args)
        # numkeys=1 表示有1个key
        result = redis_client.eval(RELEASE_LOCK_SCRIPT, 1, lock_key, lock_value)

        # 处理同步/异步返回值
        # 不同 Redis 客户端 eval 可能返回协程或直接返回值，统一兼容
        if hasattr(result, "__await__"):
            result = await result

        released = int(result) == 1  # type: ignore[misc]
        if released:
            logger.debug(f"[Global MCP] Released lock: {lock_key}")
        else:
            # 未持有或已释放：可能锁已过期被别人重新获取，脚本会拒绝删除
            logger.warning(f"[Global MCP] Lock not owned or already released: {lock_key}")
        return released
    except Exception as e:
        logger.warning(f"[Global MCP] Failed to release lock {lock_key}: {e}")
        return False


async def renew_distributed_lock(lock_key: str, lock_value: str, ttl: int) -> bool:
    """Renew a Redis lock only if this instance still owns it."""
    # 仅当仍是持有者时续期，配合看门狗协程延长长耗时初始化期间的锁寿命
    try:
        redis_client = get_redis_client()
        result = redis_client.eval(RENEW_LOCK_SCRIPT, 1, lock_key, lock_value, str(int(ttl)))
        if hasattr(result, "__await__"):
            result = await result
        return int(result) == 1  # type: ignore[misc]
    except Exception as e:
        logger.warning(f"[Global MCP] Failed to renew lock {lock_key}: {e}")
        return False


def _get_lock_renew_interval(ttl: int) -> float:
    # 续期间隔取 TTL/3，并夹在 [1s, 10s]：既能在过期前多次续期，又不过于频繁
    return max(1.0, min(float(ttl) / 3.0, 10.0))


async def _renew_lock_until_stopped(
    lock_key: str,
    lock_value: str,
    ttl: int,
    stop_event: asyncio.Event,
) -> None:
    # 看门狗协程：周期性续期锁，直到 stop_event 被置位或续期失败（不再持有）
    interval = _get_lock_renew_interval(ttl)
    while True:
        try:
            # 用带超时的 wait 实现"每 interval 秒续期一次，同时可被立即唤醒退出"
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            # 超时即到达续期时点；若续期失败说明锁已易主，停止看门狗
            if not await renew_distributed_lock(lock_key, lock_value, ttl):
                logger.warning("[Global MCP] Stopped renewing lock no longer owned: %s", lock_key)
                return


async def check_init_done(user_id: str) -> bool:
    """检查其他实例是否已完成初始化"""
    # 通过 done 标记键判断：存在即代表某实例已完成，等待方可据此提前结束等待
    try:
        redis_client = get_redis_client()
        done_key = f"{DONE_KEY_PREFIX}{user_id}"
        result = await redis_client.exists(done_key)
        return result > 0
    except Exception as e:
        logger.warning(f"[Global MCP] Failed to check init done for {user_id}: {e}")
        return False


async def mark_init_done(user_id: str) -> None:
    """标记初始化完成"""
    try:
        redis_client = get_redis_client()
        done_key = f"{DONE_KEY_PREFIX}{user_id}"
        # 设置 30 秒过期，足够让其他实例看到
        # 仅作短期信号，无需长期保留；过期后自动清理
        await redis_client.set(done_key, "1", ex=30)
    except Exception as e:
        logger.warning(f"[Global MCP] Failed to mark init done for {user_id}: {e}")


def _track_background_task(task: asyncio.Future) -> None:
    """追踪后台任务，完成后自动从集合中移除"""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _schedule_manager_close(manager: "MCPClientManager") -> None:
    """Schedule manager cleanup only when an event loop is available."""
    # 无事件循环（如同步清理路径/解释器退出）时安全跳过，避免 create_task 报错
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        task = asyncio.ensure_future(manager.close())
        _track_background_task(task)
    except Exception:
        pass


def _cleanup_expired_entries() -> int:
    """清理过期的缓存条目，返回清理的数量"""
    expired_users = [user_id for user_id, entry in _global_entries.items() if entry.is_expired()]
    for user_id in expired_users:
        entry = _global_entries.pop(user_id, None)
        if entry:
            # 异步关闭其管理器，释放底层连接
            _schedule_manager_close(entry.manager)
        # 同步清理本地锁
        _local_locks.pop(user_id, None)

    if expired_users:
        logger.info(f"[Global MCP] Cleaned up {len(expired_users)} expired entries")

    return len(expired_users)


def _cleanup_excess_entries() -> int:
    """清理超出的缓存条目（LRU），返回清理的数量"""
    max_entries = _get_max_global_entries()
    if len(_global_entries) <= max_entries:
        return 0

    # 按最后访问时间排序，删除最旧的
    sorted_entries = sorted(_global_entries.items(), key=lambda x: x[1].last_access)

    # 删除超出部分
    to_remove = len(_global_entries) - max_entries
    for user_id, entry in sorted_entries[:to_remove]:
        _global_entries.pop(user_id, None)
        # 同步清理本地锁
        _local_locks.pop(user_id, None)
        _schedule_manager_close(entry.manager)

    logger.info(f"[Global MCP] Removed {to_remove} excess entries (LRU)")
    return to_remove


async def get_global_mcp_tools(
    user_id: str,
) -> tuple[list[BaseTool], Optional["MCPClientManager"]]:
    """
    获取全局 MCP 工具（单例 + 缓存 + 分布式锁）

    1. 检查进程内全局单例
    2. 使用本地锁防止并发
    3. 使用 Redis 分布式锁防止跨实例并发
    4. 使用 Redis 标记检测其他实例是否已完成初始化

    Args:
        user_id: 用户 ID

    Returns:
        (tools, manager) - 工具列表和管理器
    """
    global _cleanup_counter

    # 定期清理过期条目（使用计数器避免竞态条件）
    # 借高频入口做惰性清理：每 CLEANUP_CHECK_INTERVAL 次访问触发一次
    _cleanup_counter += 1
    if _cleanup_counter >= CLEANUP_CHECK_INTERVAL:
        _cleanup_counter = 0
        _cleanup_expired_entries()
        _cleanup_excess_entries()
        removed = _cleanup_orphan_locks()
        if removed:
            logger.debug(f"[Global MCP] Cleaned up {removed} orphan local locks")

    # 1. 快速路径：检查全局单例
    # 无锁命中：绝大多数请求走到这里直接返回，避免加锁开销
    if user_id in _global_entries:
        entry = _global_entries[user_id]
        if entry.manager._initialized and not entry.is_expired():
            entry.touch()
            logger.info(f"[Global MCP] Hit singleton for user {user_id}, {len(entry.tools)} tools")
            return entry.tools, entry.manager

    # 2. 获取本地锁（防止同一进程内并发）
    local_lock = _get_local_lock(user_id)
    async with local_lock:
        # 3. 再次检查（double-check locking）
        # 双重检查：可能在等待本地锁期间已被其他协程填充缓存
        if user_id in _global_entries:
            entry = _global_entries[user_id]
            if entry.manager._initialized and not entry.is_expired():
                entry.touch()
                logger.info(f"[Global MCP] Hit singleton (double-check) for user {user_id}")
                return entry.tools, entry.manager

        # 4. 获取 Redis 分布式锁
        # 跨实例互斥：只让一个实例真正做初始化，其余等待其结果
        lock_key = f"{LOCK_KEY_PREFIX}{user_id}"
        logger.info(f"[Global MCP] Attempting to acquire lock: {lock_key}")
        lock_acquired, lock_value = await acquire_distributed_lock(lock_key)
        logger.info(f"[Global MCP] Lock result: {lock_acquired} for {lock_key}")

        if not lock_acquired:
            # 其他实例正在初始化，等待其完成标记
            logger.info(
                f"[Global MCP] Waiting for other instance (lock held by someone else): {user_id}"
            )

            max_wait_seconds = _get_global_init_wait_seconds()
            # 等待完成标记；上限可配置，避免请求路径长期占用协程。
            # 轮询等待：每秒检查本地缓存与 Redis 完成标记，最多等 max_wait_seconds 秒
            for attempt in range(max_wait_seconds):
                logger.info(
                    "[Global MCP] Waiting... attempt %s/%s for %s",
                    attempt + 1,
                    max_wait_seconds,
                    user_id,
                )
                await asyncio.sleep(1)

                # 检查本实例是否已有缓存（可能通过其他协程获取）
                if user_id in _global_entries:
                    entry = _global_entries[user_id]
                    if entry.manager._initialized:
                        entry.touch()
                        logger.info(
                            f"[Global MCP] Got cache after waiting {attempt + 1}s: {user_id}"
                        )
                        return entry.tools, entry.manager

                # 检查其他实例是否已完成
                if await check_init_done(user_id):
                    # 等待一小段时间让本地缓存更新（如果有的话）
                    # 注意：完成标记在 Redis，但工具缓存是各实例进程内的，
                    # 本实例并不会自动拿到别人的缓存，故这里仍可能需要自己再建一份
                    await asyncio.sleep(0.5)
                    if user_id in _global_entries:
                        entry = _global_entries[user_id]
                        if entry.manager._initialized:
                            entry.touch()
                            logger.info(f"[Global MCP] Got cache after init done: {user_id}")
                            return entry.tools, entry.manager
                    # 其他实例完成但本地没有缓存，创建一个新的
                    break

            # 超时或未获取到缓存，尝试初始化（降级）
            # 降级路径：未拿到锁也未等到结果，本实例自行初始化，保证请求不被卡死
            logger.warning(f"[Global MCP] Timeout waiting, creating new: {user_id}")

        # 若持有分布式锁，则启动看门狗协程周期续期，防止初始化耗时超过锁 TTL 被抢占
        renew_stop_event: asyncio.Event | None = None
        renew_task: asyncio.Task | None = None
        if lock_acquired and lock_value:
            renew_stop_event = asyncio.Event()
            renew_task = asyncio.create_task(
                _renew_lock_until_stopped(
                    lock_key,
                    lock_value,
                    DISTRIBUTED_LOCK_TTL,
                    renew_stop_event,
                )
            )
            _track_background_task(renew_task)

        try:
            # 5. 再次检查（triple-check）
            # 三重检查：等待/抢锁期间缓存可能已被填充
            if user_id in _global_entries:
                entry = _global_entries[user_id]
                if entry.manager._initialized and not entry.is_expired():
                    entry.touch()
                    return entry.tools, entry.manager

            # 6. 创建新的 MCPClientManager
            logger.info(f"[Global MCP] Creating manager for user {user_id}")
            # 延迟导入避免循环依赖
            from src.infra.tool.mcp_client import MCPClientManager

            manager = MCPClientManager(
                config_path=None,
                user_id=user_id,
                use_database=True,
            )
            logger.info(f"[Global MCP] Initializing manager for {user_id}...")
            await manager.initialize()
            logger.info(f"[Global MCP] Getting tools for {user_id}...")
            tools = await manager.get_tools()
            logger.info(f"[Global MCP] Got {len(tools)} tools for {user_id}")

            # 7. 保存到全局单例
            _global_entries[user_id] = GlobalMCPEntry(
                manager=manager,
                tools=tools,
            )

            # 8. 标记初始化完成（通知其他实例）
            await mark_init_done(user_id)

            # 9. 检查是否超出最大条目数
            if len(_global_entries) > _get_max_global_entries():
                _cleanup_excess_entries()

            logger.info(f"[Global MCP] Created manager for user {user_id}, {len(tools)} tools")
            return tools, manager

        finally:
            # 无论成功与否都要停止看门狗并释放分布式锁，避免锁泄漏
            if renew_stop_event is not None:
                renew_stop_event.set()
            if renew_task is not None:
                try:
                    await renew_task
                except Exception:
                    pass
            # 10. 释放 Redis 锁（如果获取了）
            if lock_acquired and lock_value:
                await release_distributed_lock(lock_key, lock_value)


async def _publish_mcp_cache_invalidation(scope: str, *, user_id: str | None = None) -> None:
    # 向失效频道广播一条消息，携带本实例 ID 供接收端过滤自身
    try:
        redis_client = get_redis_client()
        payload = await run_blocking_io(
            json.dumps,
            {
                "instance_id": get_mcp_cache_pubsub().instance_id,
                "scope": scope,
                "user_id": user_id,
            },
        )
        await redis_client.publish(MCP_CACHE_INVALIDATE_CHANNEL, payload)
    except Exception as e:
        # 广播失败仅告警：本地失效通常已完成，跨实例同步失败不致命
        logger.warning("[Global MCP] Failed to publish invalidation: %s", e)


async def invalidate_global_cache(user_id: str, *, publish: bool = True) -> None:
    """
    使全局缓存失效

    Args:
        user_id: 用户 ID
    """
    # publish 参数用于打断"广播->处理->再广播"的循环：
    # 由本地主动触发时 publish=True 通知他人；由收到广播而触发时 publish=False
    # 清除进程内缓存
    if user_id in _global_entries:
        entry = _global_entries.pop(user_id)
        try:
            await entry.manager.close()
        except Exception as e:
            logger.warning(f"[Global MCP] Failed to close manager: {e}")
        logger.info(f"[Global MCP] Invalidated singleton for user {user_id}")

    # 清除本地锁
    if user_id in _local_locks:
        del _local_locks[user_id]

    # 清除 Redis 完成标记
    # 一并删除 done 标记，确保下次请求会重新初始化而非误判为"已完成"
    try:
        redis_client = get_redis_client()
        done_key = f"{DONE_KEY_PREFIX}{user_id}"
        await redis_client.delete(done_key)
    except Exception:
        pass

    if publish:
        await _publish_mcp_cache_invalidation("user", user_id=user_id)


async def invalidate_all_global_cache(*, publish: bool = True) -> int:
    """
    使所有全局缓存失效

    Returns:
        被失效的缓存数量
    """
    count = len(_global_entries)

    # 关闭所有 manager
    # 逐个关闭以释放底层连接，单个失败不影响整体清空
    for user_id, entry in list(_global_entries.items()):
        try:
            await entry.manager.close()
        except Exception:
            pass

    _global_entries.clear()
    _local_locks.clear()

    logger.info(f"[Global MCP] Invalidated all cache, {count} entries")
    if publish:
        await _publish_mcp_cache_invalidation("all")
    return count


async def close_global_mcp_cache() -> int:
    """Close and clear every cached global MCP manager for process shutdown."""
    # 停机专用：清空全部缓存但不广播（其他实例各自管理自己的生命周期）
    return await invalidate_all_global_cache(publish=False)


async def warmup_global_cache(user_ids: list[str]) -> None:
    """
    预热全局缓存（后台任务）

    Args:
        user_ids: 要预热的用户 ID 列表
    """
    if not user_ids:
        logger.info("[Global MCP] No users to warm up, skipping")
        return
    # 预热用户数上限保护，超出只取前 max_users 个
    max_users = _get_global_warmup_max_users()
    if len(user_ids) > max_users:
        logger.warning(
            "[Global MCP] Warmup requested for %s users; only warming first %s",
            len(user_ids),
            max_users,
        )
        user_ids = user_ids[:max_users]

    logger.info(f"[Global MCP] Warming up cache for {len(user_ids)} users")
    start_time = time.time()

    async def _warmup_user(user_id: str):
        # 预热单个用户：复用正常获取路径，把缓存填好；失败不影响其他用户
        try:
            tools, _ = await get_global_mcp_tools(user_id)
            logger.info(f"[Global MCP] Warmed up {len(tools)} tools for user {user_id}")
        except Exception as e:
            logger.warning(f"[Global MCP] Warmup failed for user {user_id}: {e}")

    # next_index + lock 构成工作队列游标，供多个 worker 抢占取用户
    next_index = 0
    lock = asyncio.Lock()
    # 并发度受配置与用户数共同约束，避免预热压垮下游
    worker_count = min(
        max(1, int(getattr(settings, "MCP_GLOBAL_WARMUP_CONCURRENCY", 5) or 1)),
        len(user_ids),
    )

    async def _warmup_worker() -> None:
        # 工作协程：循环领取用户直至取尽
        nonlocal next_index
        while True:
            async with lock:
                if next_index >= len(user_ids):
                    return
                user_id = user_ids[next_index]
                next_index += 1
            await _warmup_user(user_id)

    await asyncio.gather(*(_warmup_worker() for _ in range(worker_count)))

    elapsed = time.time() - start_time
    logger.info(f"[Global MCP] Warmup complete in {elapsed:.2f}s for {len(user_ids)} users")


async def warmup_active_users_mcp(limit: int = 10) -> None:
    """
    预热所有用户的 MCP 缓存

    获取所有用户 ID，并预热他们的 MCP 配置。
    这可以显著减少首次请求的延迟。

    优化策略：
    - 限制并发数，避免资源耗尽
    - 后台执行，不阻塞应用启动
    - 失败的用户不影响其他用户

    Args:
        limit: 最多预热多少个用户（默认 10 个，0 表示无限制）
    """
    logger.info("[Global MCP] Starting MCP warmup for all users")
    start_time = time.time()

    try:
        # 获取所有用户 ID
        from src.infra.storage.mongodb import get_mongo_client
        from src.kernel.config import settings

        client = get_mongo_client()
        db = client[settings.MONGODB_DB]
        users_collection = db["users"]

        # limit<=0 表示不限制，回退到预热上限配置
        effective_limit = limit
        if effective_limit <= 0:
            effective_limit = _get_global_warmup_max_users()

        # 查询用户（去重）
        # 用聚合按 _id 分组去重并限量，避免重复用户与全表拉取
        pipeline: list[dict[str, Any]] = [
            {"$group": {"_id": "$_id"}},
            {"$limit": effective_limit},
        ]

        cursor = users_collection.aggregate(pipeline)
        user_ids: list[str] = []
        async for doc in cursor:
            user_ids.append(str(doc["_id"]))

        if not user_ids:
            logger.info("[Global MCP] No users found, skipping warmup")
            return

        logger.info(f"[Global MCP] Found {len(user_ids)} users to warm up")

        # 预热这些用户的 MCP 缓存
        await warmup_global_cache(user_ids)

        elapsed = time.time() - start_time
        logger.info(f"[Global MCP] Warmup completed in {elapsed:.2f}s for {len(user_ids)} users")

    except Exception as e:
        # 预热是尽力而为的优化，整体失败只告警不抛出
        logger.warning(f"[Global MCP] Failed to warmup users: {e}")


def get_cache_stats() -> dict:
    """获取缓存统计信息"""
    # 返回全局缓存概览（用户数、上限、TTL 及每用户明细），供监控/诊断
    now = time.time()
    return {
        "total_users": len(_global_entries),
        "max_users": _get_max_global_entries(),
        "ttl_seconds": _get_global_cache_ttl(),
        "users": [
            {
                "user_id": user_id,
                "tools_count": len(entry.tools),
                "age_seconds": int(now - entry.created_at),
                "is_expired": entry.is_expired(),
                "last_access_seconds": int(now - entry.last_access),
            }
            for user_id, entry in _global_entries.items()
        ],
    }

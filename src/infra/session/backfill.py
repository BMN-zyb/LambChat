"""Distributed, throttled session search backfill worker."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from src.infra.logging import get_logger
from src.infra.session.storage import SessionStorage
from src.infra.storage.redis import create_redis_client

logger = get_logger(__name__)

# 分布式锁的 Redis key：保证多实例部署下同一时间只有一个 worker 在回填
BACKFILL_LOCK_KEY = "session:search_backfill:lock"
# 锁的过期时间（秒）：持有者崩溃后锁会自动释放，避免死锁
BACKFILL_LOCK_TTL_SECONDS = 30
# 每批处理的会话数量
BACKFILL_BATCH_SIZE = 20
# 批与批之间的间隔（秒），用于限速、降低对数据库的压力
BACKFILL_BATCH_DELAY_SECONDS = 0.25
# 锁续期间隔：取 TTL 的三分之一，确保在过期前有多次续约机会
BACKFILL_LOCK_RENEW_INTERVAL_SECONDS = BACKFILL_LOCK_TTL_SECONDS / 3

# 释放锁的 Lua 脚本：仅当锁值等于本实例写入的值时才删除，避免误删别人的锁（原子操作）
_RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# 续期锁的 Lua 脚本：仅当锁仍属于本实例时才重设过期时间（原子操作）
_RENEW_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class SessionSearchBackfillWorker:
    """Backfill stale session search indexes one batch at a time."""

    def __init__(
        self,
        *,
        storage: SessionStorage | None = None,
        redis_client: Any | None = None,
        batch_size: int = BACKFILL_BATCH_SIZE,
        batch_delay_seconds: float = BACKFILL_BATCH_DELAY_SECONDS,
        lock_ttl_seconds: int = BACKFILL_LOCK_TTL_SECONDS,
        renew_interval_seconds: float = BACKFILL_LOCK_RENEW_INTERVAL_SECONDS,
    ) -> None:
        self._storage = storage or SessionStorage()
        self._redis = redis_client
        self._batch_size = batch_size
        self._batch_delay_seconds = batch_delay_seconds
        self._lock_ttl_seconds = lock_ttl_seconds
        self._renew_interval_seconds = renew_interval_seconds
        # 当前持有锁时写入的锁值（等于实例 id），用于校验锁归属
        self._lock_value: str | None = None
        # 本 worker 实例的唯一标识，作为锁值防止误删他人锁
        self._instance_id = str(uuid.uuid4())
        # 后台锁续期任务句柄
        self._renew_task: asyncio.Task[None] | None = None

    async def run_once(self) -> int:
        """Backfill a single batch if this instance owns the distributed lock."""
        # 抢锁失败说明已有别的实例在跑，直接返回 0
        acquired = await self._acquire_lock()
        if not acquired:
            return 0

        # 抢到锁后启动后台续期，确保处理期间锁不过期；结束时务必停续期并释放锁
        self._start_lock_renewal()
        try:
            return await self._storage.backfill_search_indexes(batch_size=self._batch_size)
        finally:
            await self._stop_lock_renewal()
            await self._release_lock()

    async def run_until_complete(self) -> int:
        """Run batches until no stale sessions remain."""
        # 循环跑批，直到某批返回 0（没有过期会话可处理）为止
        rebuilt = 0
        while True:
            batch_count = await self.run_once()
            if batch_count <= 0:
                return rebuilt
            rebuilt += batch_count
            # 批间限速休眠
            await asyncio.sleep(self._batch_delay_seconds)

    async def close(self) -> None:
        # 关闭 worker：停续期并释放独立的 Redis 连接
        await self._stop_lock_renewal()
        redis_client = self._redis
        self._redis = None
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:
                return

    async def _acquire_lock(self) -> bool:
        # 用 SET NX EX 原子地抢锁：key 不存在时才写入并附带 TTL
        redis_client = self._get_redis()
        try:
            self._lock_value = self._instance_id
            acquired = await redis_client.set(
                BACKFILL_LOCK_KEY,
                self._lock_value,
                nx=True,
                ex=self._lock_ttl_seconds,
            )
            return bool(acquired)
        except Exception as exc:
            # Redis 异常时视为未抢到锁，交由下次重试
            logger.warning("Failed to acquire session backfill lock: %s", exc)
            return False

    def _start_lock_renewal(self) -> None:
        # 启动后台续期任务（若续期间隔<=0 则不启用）
        if self._renew_interval_seconds <= 0:
            return
        if self._renew_task is None or self._renew_task.done():
            self._renew_task = asyncio.create_task(self._renew_lock_loop())

    async def _stop_lock_renewal(self) -> None:
        # 取消续期任务并等待其结束
        renew_task = self._renew_task
        self._renew_task = None
        if renew_task is None:
            return
        renew_task.cancel()
        try:
            await renew_task
        except asyncio.CancelledError:
            pass

    async def _renew_lock_loop(self) -> None:
        # 续期循环：定期续约，直到被取消
        try:
            while True:
                await asyncio.sleep(self._renew_interval_seconds)
                await self._renew_lock()
        except asyncio.CancelledError:
            return

    async def _renew_lock(self) -> None:
        # 通过 Lua 脚本原子续约；若发现锁已不属于自己则告警（可能被其他实例抢走）
        redis_client = self._redis
        lock_value = self._lock_value
        if redis_client is None or not lock_value:
            return
        try:
            renewed = await redis_client.eval(
                _RENEW_LOCK_LUA,
                1,
                BACKFILL_LOCK_KEY,
                lock_value,
                self._lock_ttl_seconds,
            )  # type: ignore[misc]
            if not renewed:
                logger.warning("Session backfill lock was lost before renewal")
        except Exception as exc:
            logger.warning("Failed to renew session backfill lock: %s", exc)

    async def _release_lock(self) -> None:
        # 通过 Lua 脚本原子释放锁：只删自己持有的锁
        redis_client = self._redis
        lock_value = self._lock_value
        self._lock_value = None
        if redis_client is None or not lock_value:
            return
        try:
            await redis_client.eval(_RELEASE_LOCK_LUA, 1, BACKFILL_LOCK_KEY, lock_value)  # type: ignore[misc]
        except Exception as exc:
            logger.warning("Failed to release session backfill lock: %s", exc)

    def _get_redis(self):
        # 延迟创建独立连接池的 Redis 客户端（隔离池避免与主业务连接互相影响）
        if self._redis is None:
            self._redis = create_redis_client(isolated_pool=True)
        return self._redis

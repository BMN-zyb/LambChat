"""Distributed locks for scheduled task execution across multiple instances."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from typing import Any, Optional, cast

from src.infra.logging import get_logger
from src.infra.storage.redis import get_redis_client

logger = get_logger(__name__)

# 多实例部署下，同一个定时任务会在每个实例都被 APScheduler 触发，这里用两种
# Redis 锁做去重：
#   - 执行锁（task_lock）：任务开跑时抢，跑完后释放。同一时刻只允许一个实例执行；
#   - slot 锁（task_slot）：针对某个「具体触发时刻」的占用，抢到后【故意不释放】，
#     用来挡住「延迟到达的其他实例」——即便第一个实例已跑完并释放了执行锁，慢半
#     拍的实例也不会把同一触发时刻的任务再跑一遍。
_LOCK_PREFIX = "scheduler:task_lock:"
_SLOT_PREFIX = "scheduler:task_slot:"
_LOCK_TTL = 600  # 10 min default TTL
_SLOT_TTL = 86400  # Keep completed schedule slots long enough to dedupe delayed peers.

# Lua: atomic compare-and-delete to avoid releasing another instance's lock
_RELEASE_LOCK_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

# Lua: atomic compare-and-expire to extend a lock we still own
_EXTEND_LOCK_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""


async def acquire_task_lock(
    task_id: str,
    run_id: str,
    ttl: int = _LOCK_TTL,
) -> Optional[str]:
    """Try to acquire the execution lock for a scheduled task.

    Uses Redis SET NX EX for atomic acquire.

    Returns:
        A token string on success, or ``None`` if the lock is already held
        (meaning another instance is executing this task).
    """
    redis = get_redis_client()
    lock_key = f"{_LOCK_PREFIX}{task_id}"
    token = f"{run_id}:{uuid.uuid4().hex[:8]}"
    acquired = await redis.set(lock_key, token, nx=True, ex=ttl)
    if acquired:
        logger.debug("[SchedulerLock] acquired lock for task=%s run=%s", task_id, run_id)
        return token
    logger.debug("[SchedulerLock] lock contested for task=%s, skipping", task_id)
    return None


async def acquire_task_slot_lock(
    task_id: str,
    slot_id: str,
    ttl: int = _SLOT_TTL,
) -> bool:
    """Claim a scheduled fire slot across all scheduler instances.

    Unlike the execution lock, this claim is intentionally not released after
    the run. It prevents delayed peers from executing the same schedule slot
    after the first instance has finished and released the execution lock.
    """
    redis = get_redis_client()
    lock_key = f"{_SLOT_PREFIX}{task_id}:{slot_id}"
    acquired = await redis.set(lock_key, "1", nx=True, ex=ttl)
    if acquired:
        logger.debug("[SchedulerSlot] claimed slot=%s for task=%s", slot_id, task_id)
        return True
    logger.debug("[SchedulerSlot] slot contested for task=%s slot=%s", task_id, slot_id)
    return False


async def release_task_lock(task_id: str, token: str) -> None:
    """Release the execution lock (only if *token* matches the current holder)."""
    redis = get_redis_client()
    lock_key = f"{_LOCK_PREFIX}{task_id}"
    await cast(Awaitable[Any], redis.eval(_RELEASE_LOCK_LUA, 1, lock_key, token))
    logger.debug("[SchedulerLock] released lock for task=%s", task_id)


async def extend_task_lock(task_id: str, token: str, extra_seconds: int = 300) -> bool:
    """Extend the lock TTL for a long-running task.

    Returns ``True`` if the lock was still owned and extended.
    """
    redis = get_redis_client()
    lock_key = f"{_LOCK_PREFIX}{task_id}"
    result = await cast(
        Awaitable[Any],
        redis.eval(_EXTEND_LOCK_LUA, 1, lock_key, token, str(extra_seconds)),
    )
    return bool(result)

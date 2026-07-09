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

# 释放执行锁的 Lua 脚本：先 GET 比对键里存的 value 是否等于自己的 token，
# 一致才执行 DEL。用 Lua 让「读取 + 比较 + 删除」在 Redis 内一次性原子完成，
# 避免「本实例锁已过期、别的实例刚抢到同名锁」时把别人的锁误删掉。
# Lua: atomic compare-and-delete to avoid releasing another instance's lock
_RELEASE_LOCK_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

# 续期执行锁的 Lua 脚本：仅当 value 仍等于自己的 token（锁还在自己手里）时，
# 才对键执行 EXPIRE 延长 TTL。同样借助 Lua 保证「比较 + 续期」的原子性，
# 防止在续期瞬间锁已被他人接管却仍被本实例延长。
# Lua: atomic compare-and-expire to extend a lock we still own
_EXTEND_LOCK_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""


# 尝试为一个定时任务抢占「执行锁」（任务真正开跑之前调用）。
# 用 Redis 的 SET NX EX 原子地「键不存在才写入并同时设置过期」，天然实现互斥获取。
# 参数：task_id 定时任务标识；run_id 本次触发的运行标识（用于组装 token）；
#       ttl 锁的自动过期秒数（防止持锁实例崩溃后锁永不释放）。
# 返回：抢到返回唯一 token 字符串（后续释放/续期需凭它校验持有者身份），
#       未抢到返回 None（说明已有其他实例正在执行该任务，本次应直接跳过）。
# 副作用：向 Redis 写入 task_lock 键。
# 为什么 token 带随机后缀：让每次持有都唯一，配合 compare-and-delete 避免误删他人的锁。
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


# 为某个「具体触发时刻（slot）」抢占占位锁，跨所有调度实例去重。
# 与执行锁的关键区别：这把锁抢到后【故意不释放】——即使第一个实例已跑完并释放了
# 执行锁，慢半拍才到达的其他实例也会因为 slot 锁仍在而放弃，从而避免「同一触发时刻
# 被执行两次」。TTL 取得较长（默认一天），只要足够挡住延迟到达的同伴即可。
# 参数：task_id 定时任务标识；slot_id 触发时刻标识；ttl 该占位记录的保留秒数。
# 返回：True 表示本实例成功认领该触发时刻（可继续执行），False 表示已被他人认领。
# 副作用：向 Redis 写入一个长期保留的 task_slot 键。
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


# 释放执行锁（任务跑完时调用）。通过 _RELEASE_LOCK_LUA 做「比对 token 一致才删除」，
# 因此只有锁的真正持有者才能释放，天然避免误删别的实例后来抢到的同名锁。
# 参数：task_id 定时任务标识；token 抢锁时拿到的持有凭据。
# 副作用：token 匹配时删除 Redis 中的 task_lock 键；不匹配则静默不动。
async def release_task_lock(task_id: str, token: str) -> None:
    """Release the execution lock (only if *token* matches the current holder)."""
    redis = get_redis_client()
    lock_key = f"{_LOCK_PREFIX}{task_id}"
    await cast(Awaitable[Any], redis.eval(_RELEASE_LOCK_LUA, 1, lock_key, token))
    logger.debug("[SchedulerLock] released lock for task=%s", task_id)


# 为长时间运行的任务续期执行锁的 TTL，防止任务还没跑完锁就先过期、被别的实例抢走。
# 通过 _EXTEND_LOCK_LUA 做「仍持有本锁才 EXPIRE」，只有当前持有者能续期。
# 参数：task_id 定时任务标识；token 持有凭据；extra_seconds 续期后的新 TTL 秒数。
# 返回：True 表示锁仍归本实例所有并已成功续期；False 表示已不再持有（不应再继续执行）。
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

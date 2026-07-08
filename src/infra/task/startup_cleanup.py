"""服务启动时的任务对账（僵尸/排队任务恢复）。

进程重启后，MongoDB 里可能残留一批「看起来还在跑」的任务（RUNNING/PENDING/
QUEUED、或标记为可恢复的 FAILED），但它们所在的实例已经没了。本模块在启动时
扫描这些会话，用「有无 Redis 心跳」判断任务是否真的还活着：
  - 还有心跳 -> 说明在别的实例上跑着，跳过；
  - 没有心跳 -> 判定为僵尸任务，交给恢复流程拉起新一轮，或标记为 EXPIRED。
为避免多实例同时重启时重复对账，用一把带续租的分布式 lease 保证同一时刻只有
一个实例执行清理。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar, cast

from src.infra.async_utils.blocking import run_blocking_io
from src.infra.logging import get_logger
from src.kernel.config import settings

from .status import TaskStatus

logger = get_logger(__name__)

T = TypeVar("T")
QUEUE_SCAN_PAGE_SIZE = 100
QUEUE_REWRITE_CHUNK_SIZE = 100
STALE_SESSION_SCAN_PAGE_SIZE = 100
# 启动清理分布式租约的 key 与默认 TTL：抢到 lease 的实例才执行清理。
STARTUP_CLEANUP_LEASE_KEY = "chat:startup-cleanup:lease"
STARTUP_CLEANUP_LEASE_TTL_SECONDS = 600

# Lua：比对 token 一致才删除，避免误删他人的 lease。
_RELEASE_LEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""

# Lua：比对 token 一致才续期，仅在仍持有 lease 时延长其 TTL。
_RENEW_LEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
end
return 0
"""


# 启动清理租约的持有凭据：redis 客户端 + token（token 为 None 表示无需/无法用锁，
# 视为直接放行）。
@dataclass(slots=True)
class _StartupCleanupLease:
    redis: Any
    token: str | None


# 有界并发地执行一批「协程工厂」并按原顺序收集结果。用固定数量的 worker 从共享
# 游标取任务，避免一次性 gather 成百上千个协程压垮下游（Redis/Mongo）。并发度取
# TASK_STARTUP_CLEANUP_CONCURRENCY（默认 16）与任务数的较小值。
async def _gather_limited(
    factories: list[Callable[[], Awaitable[T]]],
    *,
    limit: int | None = None,
) -> list[T]:
    if not factories:
        return []

    results: list[T | None] = [None] * len(factories)
    next_index = 0
    lock = asyncio.Lock()
    worker_count = min(
        max(1, int(limit or getattr(settings, "TASK_STARTUP_CLEANUP_CONCURRENCY", 16) or 1)),
        len(factories),
    )

    async def _worker() -> None:
        nonlocal next_index
        while True:
            async with lock:
                if next_index >= len(factories):
                    return
                index = next_index
                next_index += 1
            results[index] = await factories[index]()

    await asyncio.gather(*(_worker() for _ in range(worker_count)))
    return cast(list[T], results)


# 分页遍历一个 Redis List（LRANGE 逐页），避免一次性读入超长队列。
async def _iter_redis_list(redis: Any, key: str, page_size: int = QUEUE_SCAN_PAGE_SIZE):
    start = 0
    while True:
        end = start + page_size - 1
        entries = await redis.lrange(key, start, end)
        if not entries:
            return
        for entry in entries:
            yield entry
        if len(entries) < page_size:
            return
        start += page_size


# 分块 RPUSH：把 values 按 chunk_size 拆批写入，避免单条命令参数过多。
async def _rpush_in_chunks(
    redis: Any,
    key: str,
    values: list[Any],
    *,
    chunk_size: int | None = None,
) -> None:
    if not values:
        return

    chunk_size = max(1, chunk_size or QUEUE_REWRITE_CHUNK_SIZE)
    for start in range(0, len(values), chunk_size):
        await redis.rpush(key, *values[start : start + chunk_size])


async def _queue_entry_json_loads(entry: Any) -> Any:
    return await run_blocking_io(json.loads, entry)


# 剔除队列中已超过 queue_timeout 的过期条目：先数一遍是否有过期项（没有就直接返回，
# 避免无谓重写）；有则用「临时 key 重写 + RENAME」原子替换，全过期则直接删除原 key。
# 返回被剔除的条目数。
async def _rewrite_queue_without_expired(
    redis: Any,
    key: str,
    *,
    queue_timeout: float,
) -> int:
    expired = 0
    async for entry in _iter_redis_list(redis, key):
        data = await _queue_entry_json_loads(entry)
        if time.time() - data.get("queued_at", 0) > queue_timeout:
            expired += 1

    if not expired:
        return 0

    tmp_key = f"chat:queue-cleanup:{{{key}}}:{uuid.uuid4().hex}"
    valid_buffer: list[Any] = []
    wrote_valid = False

    async for entry in _iter_redis_list(redis, key):
        data = await _queue_entry_json_loads(entry)
        if time.time() - data.get("queued_at", 0) > queue_timeout:
            continue

        valid_buffer.append(entry)
        if len(valid_buffer) >= QUEUE_REWRITE_CHUNK_SIZE:
            await _rpush_in_chunks(redis, tmp_key, valid_buffer)
            valid_buffer.clear()
            wrote_valid = True

    if valid_buffer:
        await _rpush_in_chunks(redis, tmp_key, valid_buffer)
        valid_buffer.clear()
        wrote_valid = True

    if wrote_valid:
        await redis.rename(tmp_key, key)
    else:
        await redis.delete(key)
    return expired


# 在队列里查找一批 wanted_run_ids 中哪些确实还排在队列中，找齐即提前结束。
# 用于回放阶段区分「仍在队列 vs 已丢失」。
async def _find_queued_run_ids(
    redis: Any,
    key: str,
    wanted_run_ids: set[str],
    page_size: int = QUEUE_SCAN_PAGE_SIZE,
) -> set[str]:
    if not wanted_run_ids:
        return set()

    found: set[str] = set()
    async for entry in _iter_redis_list(redis, key, page_size=page_size):
        try:
            data = await _queue_entry_json_loads(entry)
        except Exception:
            continue
        queued_run_id = data.get("run_id")
        if queued_run_id is None:
            continue
        queued_run_id = str(queued_run_id)
        if queued_run_id in wanted_run_ids:
            found.add(queued_run_id)
            if len(found) >= len(wanted_run_ids):
                break
    return found


# 分批消费 Mongo 游标（每批 page_size 条），避免一次性 to_list 全量结果。
async def _iter_cursor_batches(cursor: Any, page_size: int | None = None):
    page_size = page_size or STALE_SESSION_SCAN_PAGE_SIZE
    while True:
        docs = await cursor.to_list(length=page_size)
        if not docs:
            return
        yield docs
        if len(docs) < page_size:
            return


# 读取 lease TTL 配置（下限 30s，防误配置成过小值）。
def _startup_cleanup_lease_ttl_seconds() -> int:
    value = getattr(
        settings,
        "TASK_STARTUP_CLEANUP_LEASE_TTL_SECONDS",
        STARTUP_CLEANUP_LEASE_TTL_SECONDS,
    )
    return max(30, int(value or STARTUP_CLEANUP_LEASE_TTL_SECONDS))


# 抢占启动清理租约（SET NX EX）。抢到返回带 token 的 lease；未抢到说明别的实例
# 正在清理，返回 None 让本实例跳过。redis 无 set 方法（测试桩）时视为无需锁、放行。
async def _acquire_startup_cleanup_lease(redis: Any) -> _StartupCleanupLease | None:
    set_method = getattr(redis, "set", None)
    if not callable(set_method):
        return _StartupCleanupLease(redis=redis, token=None)

    token = uuid.uuid4().hex
    try:
        acquired = await set_method(
            STARTUP_CLEANUP_LEASE_KEY,
            token,
            ex=_startup_cleanup_lease_ttl_seconds(),
            nx=True,
        )
    except Exception as exc:
        logger.warning("Failed to acquire startup cleanup lease: %s", exc)
        return None

    if not acquired:
        logger.info("Skipping startup cleanup; another instance holds the lease")
        return None
    return _StartupCleanupLease(redis=redis, token=token)


# 启动后台续租任务（若持有 token 且 redis 支持 eval）。清理耗时可能超过 TTL，
# 需周期续期以免中途丢锁。
def _start_startup_cleanup_lease_renewal(lease: _StartupCleanupLease) -> asyncio.Task | None:
    if not lease.token or not callable(getattr(lease.redis, "eval", None)):
        return None
    return asyncio.create_task(_renew_startup_cleanup_lease(lease))


# 续租循环：每隔 TTL/3 用 Lua 续期一次；一旦发现锁已不属于自己（续期失败）就退出，
# 避免在已丢锁的情况下继续「以为自己持锁」地清理。
async def _renew_startup_cleanup_lease(lease: _StartupCleanupLease) -> None:
    ttl_seconds = _startup_cleanup_lease_ttl_seconds()
    interval = max(10, ttl_seconds // 3)
    try:
        while True:
            await asyncio.sleep(interval)
            renewed = await lease.redis.eval(
                _RENEW_LEASE_LUA,
                1,
                STARTUP_CLEANUP_LEASE_KEY,
                lease.token,
                ttl_seconds,
            )
            if not renewed:
                logger.warning("Startup cleanup lease was lost before renewal")
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Failed to renew startup cleanup lease: %s", exc)


# 停止续租任务（取消并等待退出）。
async def _stop_startup_cleanup_lease_renewal(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# 释放启动清理租约（Lua 比对 token 后删除）。
async def _release_startup_cleanup_lease(lease: _StartupCleanupLease) -> None:
    if not lease.token:
        return
    eval_method = getattr(lease.redis, "eval", None)
    if not callable(eval_method):
        return
    try:
        await eval_method(_RELEASE_LEASE_LUA, 1, STARTUP_CLEANUP_LEASE_KEY, lease.token)
    except Exception as exc:
        logger.warning("Failed to release startup cleanup lease: %s", exc)


# 合并原始文档与规范化模型两处的 metadata（模型侧优先），得到统一的任务元数据视图。
def _task_metadata(session: dict[str, Any], session_model: Any) -> dict[str, Any]:
    raw_metadata = session.get("metadata") if isinstance(session, dict) else {}
    model_metadata = getattr(session_model, "metadata", None) or {}
    return {
        **(raw_metadata if isinstance(raw_metadata, dict) else {}),
        **(model_metadata if isinstance(model_metadata, dict) else {}),
    }


# 依据 metadata 判断该任务是否已被用户取消（error_code=cancelled 或状态 CANCELLED）。
def _is_user_cancelled_task(metadata: dict[str, Any]) -> bool:
    return (
        metadata.get("task_error_code") == "cancelled"
        or metadata.get("task_status") == TaskStatus.CANCELLED.value
    )


# 进一步核查是否已持久化过 user:cancel 事件。metadata 可能尚未及时落库，此处直接
# 查事件流兜底，确保用户已取消的任务不会在重启后被误恢复。
async def _has_persisted_cancel_event(session_id: str, run_id: str) -> bool:
    try:
        from src.infra.session.dual_writer import get_dual_writer

        events = await get_dual_writer().read_session_events(
            session_id,
            event_types=["user:cancel"],
            run_id=run_id,
            completed_only=False,
            max_events=1,
        )
        return bool(events)
    except Exception as exc:
        logger.warning(
            "Failed to inspect persisted cancel event during startup recovery: "
            "session=%s, run_id=%s, error=%s",
            session_id,
            run_id,
            exc,
        )
        return False


# 该 run 是否仍是会话记录的「当前 run」——只有当前 run 才需要对账，历史 run 忽略。
def _is_latest_run(
    metadata: dict[str, Any],
    run_id: str,
) -> bool:
    """Only reconcile the run that is still recorded as current for the session."""
    current_run_id = metadata.get("current_run_id")
    return current_run_id is not None and str(current_run_id) == str(run_id)


# 会话是否已指向一个更新的 run（此 run 被后来的对话轮次取代），若是则应跳过。
def _is_superseded_by_newer_run(
    metadata: dict[str, Any],
    run_id: str,
) -> bool:
    """Return true when the session already points to a newer conversation run."""
    current_run_id = metadata.get("current_run_id")
    return current_run_id is not None and str(current_run_id) != str(run_id)


# 是否为「显式记录了重启导致失败、且当前、且标记可恢复」的 run——只有这种 FAILED
# 才自动恢复，避免把普通业务失败也无脑重跑。
def _is_latest_explicit_system_restart_failure(
    metadata: dict[str, Any],
    run_id: str,
) -> bool:
    """Only auto-recover failed runs when shutdown was explicitly recorded."""
    return (
        _is_latest_run(metadata, run_id)
        and metadata.get("task_status") == TaskStatus.FAILED.value
        and metadata.get("task_recoverable") is True
        and metadata.get("task_error_code") == "server_restart"
    )


class TaskStartupCleanupService:
    """Handles startup reconciliation for stale and queued tasks."""

    # 依赖注入（由 manager 组装）：storage / heartbeat / executor 工厂 /
    # 加载会话记录 / 恢复中断 run 的回调；后两个可选回调允许外部替换「回放排队
    # 任务」「清理过期队列」的实现（缺省用本类自带实现）。
    def __init__(
        self,
        *,
        storage: Any,
        heartbeat: Any,
        ensure_executor: Callable[[], Any],
        load_session_record: Callable[[dict[str, Any]], Awaitable[Any | None]],
        resume_interrupted_run: Callable[[Any, str, str], Awaitable[dict[str, Any]]],
        replay_pending_queued_tasks: Callable[[], Awaitable[None]] | None = None,
        cleanup_stale_queues: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._storage = storage
        self._heartbeat = heartbeat
        self._ensure_executor = ensure_executor
        self._load_session_record = load_session_record
        self._resume_interrupted_run = resume_interrupted_run
        self._replay_pending_queued_tasks_cb = replay_pending_queued_tasks
        self._cleanup_stale_queues_cb = cleanup_stale_queues

    # 综合判断某 run 是否属于「用户已取消」：metadata 命中或已持久化 user:cancel 事件。
    async def _is_user_cancelled_run(
        self,
        session_id: str,
        metadata: dict[str, Any],
        run_id: str,
    ) -> bool:
        return _is_user_cancelled_task(metadata) or await _has_persisted_cancel_event(
            session_id,
            run_id,
        )

    # 启动对账主入口：先抢分布式 lease（抢不到直接返回，交给持锁实例），并启动续租；
    # 随后分三类扫描 MongoDB 会话并逐类处理：
    #   1) RUNNING —— 无心跳则视为僵尸，恢复；
    #   2) PENDING/QUEUED —— 在活跃集合但无心跳则恢复；
    #   3) FAILED 且显式可恢复（server_restart）—— 无心跳则恢复。
    # 最后回放仍在队列里的排队任务并清理过期队列。finally 里停续租、释放 lease。
    async def cleanup_stale_tasks(self) -> None:
        """
        Recover stale active tasks and explicitly recoverable failed tasks after restart.
        """
        from .concurrency import get_concurrency_limiter

        limiter = get_concurrency_limiter()
        redis = limiter.redis
        lease = await _acquire_startup_cleanup_lease(redis)
        if lease is None:
            return
        renewal_task = _start_startup_cleanup_lease_renewal(lease)

        try:
            # --- RUNNING tasks ---
            cursor = self._storage.collection.find(
                {"metadata.task_status": TaskStatus.RUNNING.value}
            )

            cleaned_count = 0

            async for running_sessions in _iter_cursor_batches(cursor):
                cleaned_count += await self._process_running_sessions(running_sessions)

            # --- PENDING / QUEUED tasks ---
            cursor = self._storage.collection.find(
                {
                    "metadata.task_status": {
                        "$in": [TaskStatus.PENDING.value, TaskStatus.QUEUED.value]
                    }
                }
            )
            async for pending_sessions in _iter_cursor_batches(cursor):
                cleaned_count += await self._process_pending_sessions(pending_sessions, redis)

            # --- FAILED recoverable tasks ---
            cursor = self._storage.collection.find(
                {
                    "metadata.task_status": TaskStatus.FAILED.value,
                    "metadata.task_recoverable": True,
                    "metadata.task_error_code": "server_restart",
                }
            )
            async for failed_recoverable_sessions in _iter_cursor_batches(cursor):
                cleaned_count += await self._process_failed_recoverable_sessions(
                    failed_recoverable_sessions
                )

            if cleaned_count > 0:
                logger.info("Cleaned up %s stale tasks without heartbeat", cleaned_count)

            await self.replay_pending_queued_tasks()
            await self.cleanup_stale_queues()
        except Exception as e:
            logger.error("Failed to cleanup stale tasks: %s", e)
        finally:
            await _stop_startup_cleanup_lease_renewal(renewal_task)
            await _release_startup_cleanup_lease(lease)

    # 处理一批 RUNNING 会话：并发加载模型、逐个筛掉「用户取消 / 已被更新 run 取代 /
    # 非当前 run」的候选，再并发查心跳；对无心跳者调用 resume_interrupted_run 恢复。
    # 返回本批清理数。
    async def _process_running_sessions(self, running_sessions: list[dict[str, Any]]) -> int:
        load_session_factories: list[Callable[[], Awaitable[Any]]] = []
        for session in running_sessions:

            async def _load_session(session: dict[str, Any] = session) -> Any:
                return await self._load_session_record(session)

            load_session_factories.append(_load_session)

        session_models = await _gather_limited(load_session_factories)

        candidates: list[tuple[Any, str, dict[str, Any], str]] = []
        for session, session_model in zip(running_sessions, session_models):
            if session_model is None:
                continue
            session_id = session_model.id
            metadata = _task_metadata(session, session_model)
            run_id = session.get("metadata", {}).get("current_run_id") or metadata.get(
                "current_run_id"
            )
            if not run_id:
                continue
            if await self._is_user_cancelled_run(session_id, metadata, str(run_id)):
                logger.info(
                    "Skipping user-cancelled RUNNING task during startup recovery: session=%s, run_id=%s",
                    session_id,
                    run_id,
                )
                continue
            if _is_superseded_by_newer_run(metadata, run_id):
                logger.info(
                    "Skipping superseded RUNNING task during startup recovery: session=%s, old_run=%s, current_run=%s",
                    session_id,
                    run_id,
                    metadata.get("current_run_id"),
                )
                continue
            if not _is_latest_run(metadata, run_id):
                logger.debug(
                    "Skipping non-current RUNNING task during startup recovery: session=%s, run_id=%s",
                    session_id,
                    run_id,
                )
                continue
            candidates.append((session_model, session_id, metadata, run_id))

        if not candidates:
            return 0

        cleaned_count = 0
        heartbeat_factories: list[Callable[[], Awaitable[bool]]] = []
        for _, _, _, run_id in candidates:

            async def _check_heartbeat(run_id: str = run_id) -> bool:
                return await self._heartbeat.check_exists(run_id)

            heartbeat_factories.append(_check_heartbeat)

        heartbeat_results = await _gather_limited(heartbeat_factories)
        for (
            session_model,
            session_id,
            metadata,
            run_id,
        ), heartbeat_exists in zip(candidates, heartbeat_results):
            if heartbeat_exists:
                logger.debug(
                    "Task still running on another instance: session=%s, run_id=%s",
                    session_id,
                    run_id,
                )
                continue

            logger.warning(
                "Cleaning up stale RUNNING task (no heartbeat): session=%s, run_id=%s",
                session_id,
                run_id,
            )
            recovery_result = await self._resume_interrupted_run(
                session_model,
                run_id,
                "server_restart",
            )
            if recovery_result.get("success"):
                logger.info(
                    "Recovered stale RUNNING task: session=%s, old_run=%s, new_run=%s",
                    session_id,
                    run_id,
                    recovery_result.get("run_id"),
                )
            else:
                logger.warning(
                    "Failed to auto-recover stale RUNNING task %s: %s",
                    run_id,
                    recovery_result.get("message"),
                )
            cleaned_count += 1
        return cleaned_count

    # 处理一批 PENDING/QUEUED 会话：在 running 逻辑基础上多一层——只处理仍在活跃
    # Sorted Set 里（zscore 非空）的 run；这些在活跃集合但无心跳的，判定为僵尸并恢复。
    async def _process_pending_sessions(
        self,
        pending_sessions: list[dict[str, Any]],
        redis: Any,
    ) -> int:
        pending_load_factories: list[Callable[[], Awaitable[Any]]] = []
        for session in pending_sessions:

            async def _load_pending_session(session: dict[str, Any] = session) -> Any:
                return await self._load_session_record(session)

            pending_load_factories.append(_load_pending_session)

        pending_models = await _gather_limited(pending_load_factories)

        pending_candidates: list[tuple[Any, str, dict[str, Any], str, str]] = []
        for session, session_model in zip(pending_sessions, pending_models):
            if session_model is None:
                continue
            session_id = session_model.id
            metadata = _task_metadata(session, session_model)
            run_id = session.get("metadata", {}).get("current_run_id") or metadata.get(
                "current_run_id"
            )
            user_id = session.get("user_id")
            if not run_id or not user_id:
                continue
            if await self._is_user_cancelled_run(session_id, metadata, str(run_id)):
                logger.info(
                    "Skipping user-cancelled PENDING task during startup recovery: session=%s, run_id=%s",
                    session_id,
                    run_id,
                )
                continue
            if _is_superseded_by_newer_run(metadata, run_id):
                logger.info(
                    "Skipping superseded PENDING task during startup recovery: session=%s, old_run=%s, current_run=%s",
                    session_id,
                    run_id,
                    metadata.get("current_run_id"),
                )
                continue
            if not _is_latest_run(metadata, run_id):
                logger.debug(
                    "Skipping non-current PENDING task during startup recovery: session=%s, run_id=%s",
                    session_id,
                    run_id,
                )
                continue
            pending_candidates.append((session_model, session_id, metadata, run_id, user_id))

        if not pending_candidates:
            return 0

        active_factories: list[Callable[[], Awaitable[Any]]] = []
        for _, _, _, run_id, user_id in pending_candidates:

            async def _active_score(run_id: str = run_id, user_id: str = user_id) -> Any:
                return await redis.zscore(f"chat:active:{user_id}", run_id)

            active_factories.append(_active_score)

        active_results = await _gather_limited(active_factories)
        active_candidates = [
            cand for cand, score in zip(pending_candidates, active_results) if score is not None
        ]
        if not active_candidates:
            return 0

        cleaned_count = 0
        pending_heartbeat_factories: list[Callable[[], Awaitable[bool]]] = []
        for _, _, _, run_id, _ in active_candidates:

            async def _check_pending_heartbeat(run_id: str = run_id) -> bool:
                return await self._heartbeat.check_exists(run_id)

            pending_heartbeat_factories.append(_check_pending_heartbeat)

        heartbeat_results = await _gather_limited(pending_heartbeat_factories)
        for (
            session_model,
            session_id,
            metadata,
            run_id,
            user_id,
        ), heartbeat_exists in zip(active_candidates, heartbeat_results):
            if heartbeat_exists:
                logger.debug(
                    "Pending task still in active set (running elsewhere): session=%s, run_id=%s",
                    session_id,
                    run_id,
                )
                continue

            logger.warning(
                "Cleaning up stale PENDING task (in active set, no heartbeat): session=%s, run_id=%s",
                session_id,
                run_id,
            )
            recovery_result = await self._resume_interrupted_run(
                session_model,
                run_id,
                "server_restart",
            )
            if recovery_result.get("success"):
                logger.info(
                    "Recovered stale PENDING task: session=%s, old_run=%s, new_run=%s",
                    session_id,
                    run_id,
                    recovery_result.get("run_id"),
                )
            else:
                logger.warning(
                    "Failed to auto-recover stale PENDING task %s: %s",
                    run_id,
                    recovery_result.get("message"),
                )
            cleaned_count += 1
        return cleaned_count

    # 处理一批「显式可恢复的 FAILED」会话：只认 _is_latest_explicit_system_restart_failure
    # 的 run，无心跳则恢复。避免把普通业务失败也当成需要重跑的对象。
    async def _process_failed_recoverable_sessions(
        self,
        failed_recoverable_sessions: list[dict[str, Any]],
    ) -> int:
        failed_load_factories: list[Callable[[], Awaitable[Any]]] = []
        for session in failed_recoverable_sessions:

            async def _load_failed_session(session: dict[str, Any] = session) -> Any:
                return await self._load_session_record(session)

            failed_load_factories.append(_load_failed_session)

        failed_models = await _gather_limited(failed_load_factories)

        failed_candidates: list[tuple[Any, str, dict[str, Any], str]] = []
        for session, session_model in zip(failed_recoverable_sessions, failed_models):
            if session_model is None:
                continue
            session_id = session_model.id
            run_id = session.get("metadata", {}).get("current_run_id")
            if not run_id:
                continue
            metadata = _task_metadata(session, session_model)
            if not _is_latest_explicit_system_restart_failure(metadata, run_id):
                logger.debug(
                    "Skipping unmarked FAILED task during startup recovery: session=%s, run_id=%s",
                    session_id,
                    run_id,
                )
                continue
            failed_candidates.append((session_model, session_id, metadata, run_id))

        if not failed_candidates:
            return 0

        cleaned_count = 0
        failed_heartbeat_factories: list[Callable[[], Awaitable[bool]]] = []
        for _, _, _, run_id in failed_candidates:

            async def _check_failed_heartbeat(run_id: str = run_id) -> bool:
                return await self._heartbeat.check_exists(run_id)

            failed_heartbeat_factories.append(_check_failed_heartbeat)

        failed_heartbeat_results = await _gather_limited(failed_heartbeat_factories)
        for (
            session_model,
            session_id,
            metadata,
            run_id,
        ), heartbeat_exists in zip(failed_candidates, failed_heartbeat_results):
            if heartbeat_exists:
                logger.debug(
                    "Recoverable failed task still has heartbeat: session=%s, run_id=%s",
                    session_id,
                    run_id,
                )
                continue

            logger.warning(
                "Recovering failed-but-recoverable task: session=%s, run_id=%s",
                session_id,
                run_id,
            )
            recovery_result = await self._resume_interrupted_run(
                session_model,
                run_id,
                "server_restart",
            )
            if recovery_result.get("success"):
                logger.info(
                    "Recovered failed task: session=%s, old_run=%s, new_run=%s",
                    session_id,
                    run_id,
                    recovery_result.get("run_id"),
                )
            else:
                logger.warning(
                    "Failed to auto-recover failed task %s: %s",
                    run_id,
                    recovery_result.get("message"),
                )
            cleaned_count += 1
        return cleaned_count

    # 清理所有用户队列中超时的排队条目（SCAN 遍历 chat:queue:* 并重写）。可被外部
    # 注入的回调替换。
    async def cleanup_stale_queues(self) -> None:
        """Drop queue entries that have exceeded the concurrency queue timeout."""
        if self._cleanup_stale_queues_cb is not None:
            await self._cleanup_stale_queues_cb()
            return

        try:
            from .concurrency import QUEUE_TIMEOUT, get_concurrency_limiter

            limiter = get_concurrency_limiter()
            redis = limiter.redis

            cursor = 0
            while True:
                cursor, keys = await redis.scan(cursor=cursor, match="chat:queue:*", count=100)
                for key in keys:
                    expired = await _rewrite_queue_without_expired(
                        redis,
                        key,
                        queue_timeout=QUEUE_TIMEOUT,
                    )
                    if expired:
                        logger.info("Cleaned %s expired queue entries from %s", expired, key)
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning("Failed to cleanup stale queues: %s", e)

    # 回放启动时仍处于 PENDING/QUEUED 的排队任务。对每个当前候选 run：
    #   - 若仍在 Redis 队列里：调 limiter.release 触发一次出队分发（把它推进执行）；
    #   - 若已不在队列、也不在活跃集合、又无心跳：说明排队期间服务器重启把它丢了，
    #     尝试恢复；恢复不成则标记为 EXPIRED（放弃）。
    # 可被外部注入的回调替换。
    async def replay_pending_queued_tasks(self) -> None:
        """Replay latest queued tasks that still have Redis queue entries."""
        if self._replay_pending_queued_tasks_cb is not None:
            await self._replay_pending_queued_tasks_cb()
            return

        try:
            from .concurrency import get_concurrency_limiter

            limiter = get_concurrency_limiter()
            redis = limiter.redis

            cursor = self._storage.collection.find(
                {
                    "metadata.task_status": {
                        "$in": [TaskStatus.PENDING.value, TaskStatus.QUEUED.value]
                    }
                }
            )

            replayed = 0
            abandoned = 0

            async for pending_sessions in _iter_cursor_batches(cursor):
                replay_candidates: list[
                    tuple[dict[str, Any], Any, str, dict[str, Any], str, str]
                ] = []
                wanted_run_ids_by_user: dict[str, set[str]] = {}
                replay_load_factories: list[Callable[[], Awaitable[Any]]] = []
                for session in pending_sessions:

                    async def _load_replay_session(session: dict[str, Any] = session) -> Any:
                        return await self._load_session_record(session)

                    replay_load_factories.append(_load_replay_session)

                pending_models = await _gather_limited(replay_load_factories)

                for session, session_model in zip(pending_sessions, pending_models):
                    if session_model is None:
                        continue
                    session_id = session_model.id
                    metadata = _task_metadata(session, session_model)
                    run_id = session.get("metadata", {}).get("current_run_id") or metadata.get(
                        "current_run_id"
                    )
                    user_id = session.get("user_id")
                    if not run_id or not user_id:
                        continue
                    if await self._is_user_cancelled_run(session_id, metadata, str(run_id)):
                        logger.info(
                            "Skipping user-cancelled queued task replay during startup recovery: session=%s, run_id=%s",
                            session_id,
                            run_id,
                        )
                        continue

                    if _is_superseded_by_newer_run(metadata, run_id):
                        logger.info(
                            "Skipping superseded queued task replay during startup recovery: session=%s, old_run=%s, current_run=%s",
                            session_id,
                            run_id,
                            metadata.get("current_run_id"),
                        )
                        continue

                    if not _is_latest_run(metadata, run_id):
                        logger.debug(
                            "Skipping non-current queued task replay during startup recovery: session=%s, run_id=%s",
                            session_id,
                            run_id,
                        )
                        continue

                    run_id = str(run_id)
                    replay_candidates.append(
                        (session, session_model, session_id, metadata, run_id, user_id)
                    )
                    wanted_run_ids_by_user.setdefault(user_id, set()).add(run_id)

                queued_run_ids_by_user: dict[str, set[str]] = {}
                for user_id, wanted_run_ids in wanted_run_ids_by_user.items():
                    queue_key = f"chat:queue:{user_id}"
                    queued_run_ids_by_user[user_id] = await _find_queued_run_ids(
                        redis,
                        queue_key,
                        wanted_run_ids,
                    )

                for (
                    session,
                    session_model,
                    session_id,
                    metadata,
                    run_id,
                    user_id,
                ) in replay_candidates:
                    if run_id in queued_run_ids_by_user.get(user_id, set()):
                        logger.info(
                            "Replaying queued task on startup: session=%s, run_id=%s",
                            session_id,
                            run_id,
                        )
                        try:
                            await limiter.release(user_id, run_id)
                            replayed += 1
                        except Exception as e:
                            logger.warning("Failed to replay queued task %s: %s", run_id, e)
                    else:
                        active_key = f"chat:active:{user_id}"
                        in_active = await redis.zscore(active_key, run_id) is not None
                        heartbeat_exists = await self._heartbeat.check_exists(run_id)

                        if in_active or heartbeat_exists:
                            logger.debug(
                                "Pending task still active or running elsewhere: session=%s, run_id=%s",
                                session_id,
                                run_id,
                            )
                        else:
                            logger.warning(
                                "Abandoned queued task (no queue entry, no active, no heartbeat): session=%s, run_id=%s",
                                session_id,
                                run_id,
                            )
                            recovery_result = await self._resume_interrupted_run(
                                session_model,
                                run_id,
                                "server_restart",
                            )
                            if recovery_result.get("success"):
                                logger.info(
                                    "Recovered abandoned queued task: session=%s, old_run=%s, new_run=%s",
                                    session_id,
                                    run_id,
                                    recovery_result.get("run_id"),
                                )
                            else:
                                executor = self._ensure_executor()
                                await executor._update_session_status(
                                    session_id,
                                    TaskStatus.EXPIRED,
                                    "Task abandoned (server restarted while queued)",
                                    run_id=run_id,
                                )
                                abandoned += 1

            if replayed > 0:
                logger.info("Replayed %s queued tasks on startup", replayed)
            if abandoned > 0:
                logger.warning("Marked %s abandoned queued tasks as EXPIRED", abandoned)
        except Exception as e:
            logger.error("Failed to replay pending queued tasks: %s", e)

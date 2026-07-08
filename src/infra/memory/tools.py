"""
Unified Memory Tools - LangChain Tool Integration

Provides a single set of memory tools that work with any MemoryBackend.
The underlying backend is transparent to the Agent — tool names and interfaces
are identical regardless of which memory provider is active.
"""

import asyncio
import json
import uuid
from typing import Annotated, Any, Optional

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool
from langsmith.run_helpers import tracing_context

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.memory.client.base import (
    MemoryBackend,
    create_memory_backend,
    get_user_id_from_runtime,
)
from src.infra.memory.compaction_agent import (
    get_memory_compaction_agent,
    stop_memory_compaction_agent,
)
from src.infra.scheduler import ScheduledJob, get_runtime_scheduler
from src.kernel.config import settings

logger = get_logger(__name__)


# JSON 序列化丢到线程池执行；三个工具函数（retain/recall/delete）统一通过它把结果字典
# 格式化为字符串返回（LangChain 工具约定返回值必须是字符串）
async def _json_dumps_result(data: dict[str, Any]) -> str:
    return await run_blocking_io(json.dumps, data, ensure_ascii=False)


# Module-level cached backend (initialized lazily)
# 进程级单例，懒创建；配置变更时可通过 schedule_backend_reset 热替换，无需重启进程
_backend: Optional[MemoryBackend] = None
# 保护 _backend 懒创建过程的锁，避免并发首次访问时创建出多个 backend 实例
_backend_lock: Optional[asyncio.Lock] = None
# 记录上面这把锁是在哪个事件循环里创建的：asyncio.Lock 内部绑定创建时所在的事件循环，
# 跨循环使用会出错，因此当前运行循环发生变化（例如某些热重载场景）时需要重新创建一把新锁
_backend_lock_loop: Optional[asyncio.AbstractEventLoop] = None
# 跟踪当前正在执行的后端重置任务（fire-and-forget），避免重复调度多个重置任务
_backend_reset_task: Optional[asyncio.Task] = None
# 持有所有 fire-and-forget 后台任务的强引用，防止被 GC 提前回收；应用关闭时统一取消/等待
_background_tasks: set[asyncio.Task] = set()
# 按用户维度跟踪"自动记忆抓取"后台任务，保证同一用户同一时间最多一个抓取任务在跑
_auto_capture_tasks_by_user: dict[str, asyncio.Task] = {}
# 按用户维度的本地（进程内）锁：在真正去抢分布式锁之前先做一次本地互斥，
# 减少同一进程内并发请求都去抢分布式锁的无意义 Redis 往返
_auto_capture_user_locks: dict[str, asyncio.Lock] = {}
_AUTO_CAPTURE_LOCKS_MAX = 500  # Prevent unbounded lock accumulation
# 自动抓取记忆时，单次输入文本允许的最大字符数，超出会被截断（见 _clip_auto_capture_input）
_AUTO_CAPTURE_INPUT_MAX_CHARS = 8000
# 全进程范围内，同时处于运行状态的自动抓取任务数量上限，
# 防止用户消息过于密集时无限制地并发创建后台任务
_AUTO_CAPTURE_MAX_TASKS = 8


# 延迟导入 distributed 模块中的分布式锁函数，避免模块加载期的循环依赖
# （distributed.py 处理 pub/sub 消息时会反向导入本模块的 _get_backend）
def _get_auto_capture_lock_fns():
    from src.infra.memory.distributed import acquire_auto_capture_lock, release_auto_capture_lock

    return acquire_auto_capture_lock, release_auto_capture_lock


# 当某用户的本地锁既没有被持有、也没有其他协程在等待时，把它从字典里移除，
# 避免为"只活跃过一次就再也不用"的用户长期占着一个 Lock 对象
def _cleanup_local_auto_capture_lock(user_id: str, lock: asyncio.Lock) -> None:
    waiters = getattr(lock, "_waiters", None)
    has_waiters = bool(waiters) if waiters is not None else False
    if not lock.locked() and not has_waiters:
        current = _auto_capture_user_locks.get(user_id)
        if current is lock:
            _auto_capture_user_locks.pop(user_id, None)


def _evict_idle_auto_capture_locks() -> None:
    """Evict idle locks when the dict grows too large."""
    # 简单的批量淘汰策略：超过上限时，把当前所有空闲（未被持有且无等待者）的锁挑出来，
    # 淘汰其中 1/4；不追求精确的 LRU，只是为了防止字典在活跃用户很多时无限增长
    if len(_auto_capture_user_locks) <= _AUTO_CAPTURE_LOCKS_MAX:
        return
    idle_users = [
        uid
        for uid, lock in _auto_capture_user_locks.items()
        if not lock.locked() and not getattr(lock, "_waiters", None)
    ]
    for uid in idle_users[: len(_auto_capture_user_locks) // 4]:
        _auto_capture_user_locks.pop(uid, None)


def _get_backend_lock() -> asyncio.Lock:
    """Get or create the backend lock for the current event loop.

    Recreates the lock if the event loop has changed (e.g. after uvicorn reload).
    """
    global _backend_lock, _backend_lock_loop
    current_loop = asyncio.get_running_loop()
    if _backend_lock is None or _backend_lock_loop is not current_loop:
        _backend_lock = asyncio.Lock()
        _backend_lock_loop = current_loop
    return _backend_lock


# 截断过长的用户输入后再送去做自动记忆抓取，避免把整段超长对话都塞给抽取逻辑
# （成本高且容易抓出无意义的碎片）；截断时附带提示信息，方便排查记忆内容为何显得不完整
def _clip_auto_capture_input(user_input: str) -> str:
    max_chars = max(
        int(
            getattr(
                settings,
                "NATIVE_MEMORY_AUTO_CAPTURE_INPUT_MAX_CHARS",
                _AUTO_CAPTURE_INPUT_MAX_CHARS,
            )
            or 0
        ),
        1,
    )
    if len(user_input) <= max_chars:
        return user_input
    return (
        user_input[:max_chars].rstrip()
        + f"\n\n[truncated from {len(user_input)} chars for auto memory capture]"
    )


# 从配置读取自动抓取并发任务上限，读取失败/非法时退回默认值，且结果至少为 1
def _get_auto_capture_max_tasks() -> int:
    return max(
        int(
            getattr(
                settings,
                "NATIVE_MEMORY_AUTO_CAPTURE_MAX_TASKS",
                _AUTO_CAPTURE_MAX_TASKS,
            )
            or 0
        ),
        1,
    )


async def _get_backend() -> Optional[MemoryBackend]:
    """Get or create the active memory backend (singleton)."""
    # 双重检查锁定：先无锁快速判断是否已存在，避免每次调用都要 await 锁；
    # 真正需要创建时才进锁，进锁后再次判断以防止并发场景下重复创建。
    # ENABLE_MEMORY=false 或创建失败时 backend 为 None，调用方需要自行处理
    # "记忆功能不可用"的情况（通常是直接跳过或提示用户）。
    global _backend
    if _backend is not None:
        return _backend

    async with _get_backend_lock():
        if _backend is None:
            _backend = await create_memory_backend()
            if _backend is None:
                logger.warning(
                    "[Memory] No backend available (ENABLE_MEMORY=%s)",
                    settings.ENABLE_MEMORY,
                )
            else:
                logger.info("[Memory] Backend initialized: %s", _backend.name)
        return _backend


# ============================================================================
# Unified Memory Tools
# ============================================================================


@tool
async def memory_retain(
    content: Annotated[str, "The memory content to store (facts, observations, experiences)"],
    title: Annotated[
        Optional[str],
        "Short title for this memory (max 25 chars, e.g. 'Go expert new to React', 'prefers raw SQL')",
    ] = None,
    summary: Annotated[
        Optional[str],
        "Brief summary of this memory (max 80 chars)",
    ] = None,
    context: Annotated[
        Optional[str],
        "Optional context or category for this memory (e.g., 'user_identity', 'project_constraint', 'feedback_rule', 'reference_link')",
    ] = None,
    tags: Annotated[
        Optional[list[str]],
        "Optional keyword tags for this memory (e.g., ['Go', 'React', 'newcomer']). Max 5 tags.",
    ] = None,
    existing_memory_id: Annotated[
        Optional[str],
        "Optional existing memory ID to update instead of relying on fuzzy deduplication.",
    ] = None,
    runtime: ToolRuntime = None,  # type: ignore[assignment]
) -> str:
    """
    Store a memory for cross-session persistence. STRICT: only genuinely useful,
    non-temporary information is accepted. Content that is too short, looks like a
    question, resembles code/commands, or duplicates an existing recent memory will
    be rejected. Prefer storing high-signal facts like user preferences, project
    context, feedback, or external references. Use explicit context labels such as
    `user_identity`, `project_constraint`, `project_status`, `feedback_rule`, or
    `reference_link` instead of vague buckets like `user_preferences`.
    """
    # 三个工具函数（retain/recall/delete）共享同一套模式：从 runtime 中取出当前用户身份
    # -> 确保记忆后端已初始化 -> 调用对应后端方法 -> 统一以 JSON 字符串形式返回
    # （LangChain 工具约定返回值必须是字符串）。任何异常都被捕获转换为
    # {success: False, error: ...} 而不是让异常冒泡，避免一次记忆操作失败中断整个 agent 运行。
    user_id = get_user_id_from_runtime(runtime)
    if not user_id:
        return await _json_dumps_result({"success": False, "error": "User not authenticated"})

    backend = await _get_backend()
    if not backend:
        return await _json_dumps_result({"success": False, "error": "Memory service not available"})

    try:
        result = await backend.retain(
            user_id,
            content,
            context,
            title=title,
            summary=summary,
            tags=tags,
            existing_memory_id=existing_memory_id,
        )
        return await _json_dumps_result(result)
    except Exception as e:
        logger.error(f"[Memory] Failed to retain memory: {e}")
        return await _json_dumps_result({"success": False, "error": str(e)})


@tool
async def memory_recall(
    query: Annotated[str, "The search query to find relevant memories"],
    max_results: Annotated[int, "Maximum number of memories to return (default: 5)"] = 5,
    memory_types: Annotated[
        Optional[list[str]],
        "Filter by memory types (backend-specific), or None for all types",
    ] = None,
    runtime: ToolRuntime = None,  # type: ignore[assignment]
) -> str:
    """
    Search and retrieve relevant memories from cross-session storage.

    Use this tool to recall previously stored information. The search is
    semantic and will find memories that are conceptually related to the query.
    """
    # 与 memory_retain 相同的"鉴权 + 后端检查 + 异常兜底"模式
    user_id = get_user_id_from_runtime(runtime)
    if not user_id:
        return await _json_dumps_result({"success": False, "error": "User not authenticated"})

    backend = await _get_backend()
    if not backend:
        return await _json_dumps_result({"success": False, "error": "Memory service not available"})

    try:
        result = await backend.recall(user_id, query, max_results, memory_types)
        return await _json_dumps_result(result)
    except Exception as e:
        logger.error(f"[Memory] Failed to recall memories: {e}")
        return await _json_dumps_result({"success": False, "error": str(e)})


@tool
async def memory_delete(
    memory_id: Annotated[str, "The ID of the memory to delete"],
    runtime: ToolRuntime = None,  # type: ignore[assignment]
) -> str:
    """
    Delete a specific memory by ID.

    Use this tool when a user wants to remove a specific memory.
    Get the memory ID from the memory_recall tool output.
    """
    # 与 memory_retain 相同的"鉴权 + 后端检查 + 异常兜底"模式
    user_id = get_user_id_from_runtime(runtime)
    if not user_id:
        return await _json_dumps_result({"success": False, "error": "User not authenticated"})

    backend = await _get_backend()
    if not backend:
        return await _json_dumps_result({"success": False, "error": "Memory service not available"})

    try:
        result = await backend.delete(user_id, memory_id)
        return await _json_dumps_result(result)
    except Exception as e:
        logger.error(f"[Memory] Failed to delete memory: {e}")
        return await _json_dumps_result({"success": False, "error": str(e)})


# ============================================================================
# Tool Factory Functions
# ============================================================================

# 以下几个工厂函数把模块级的 @tool 装饰实例暴露出去，供上层按需组装工具列表
# （例如某些 agent 可能只想启用 recall，不启用 retain/delete）


def get_memory_retain_tool() -> BaseTool:
    return memory_retain


def get_memory_recall_tool() -> BaseTool:
    return memory_recall


def get_memory_delete_tool() -> BaseTool:
    return memory_delete


def get_all_memory_tools() -> list[BaseTool]:
    """Get all unified memory tools (works with any backend)."""
    return [memory_retain, memory_recall, memory_delete]


def _background_task_error(task: asyncio.Task) -> None:
    """Handle exceptions from background tasks."""
    # 配合 asyncio.Task.add_done_callback 使用：任务的异常如果没有人读取
    # （调用 exception()/result()），会在任务对象被 GC 时以
    # "Task exception was never retrieved" 警告的形式泄漏出来；这里主动读取并记录日志，
    # 避免这类警告，但不会重新抛出（毕竟是后台 fire-and-forget 任务）。
    try:
        exc = task.exception()
        if exc:
            logger.warning(f"[Memory] Background task failed: {exc}")
    except asyncio.CancelledError:
        pass


def _auto_capture_task_done(user_id: str, task: asyncio.Task) -> None:
    # 自动抓取任务结束后的清理回调：只有它仍是当前记录的那个任务时才摘除
    # （防止跟一个已被新任务替换掉的旧回调互相打架），并从全局后台任务集合里移除，
    # 同时顺带记录可能存在的异常
    current = _auto_capture_tasks_by_user.get(user_id)
    if current is task:
        _auto_capture_tasks_by_user.pop(user_id, None)
    _background_tasks.discard(task)
    _background_task_error(task)


# 真正执行一次自动记忆抓取：分两层加锁——
#   1) 本地锁（_auto_capture_user_locks，按用户维度）：避免同一进程内针对同一用户的并发调用
#      都去抢分布式锁，减少无意义的 Redis 往返；
#   2) 分布式锁（acquire_auto_capture_lock，见 memory/distributed.py）：避免多实例部署下，
#      不同进程同时对同一用户做抓取，产生重复/冲突的记忆写入。
# 拿到两层锁之后才真正调用 backend.auto_retain_from_text 做实际的抽取与写入；
# 写入成功（stored>0）时顺带触发一次"写后压缩检查"，让长期积累的碎片记忆有机会被合并整理，
# 压缩失败不影响本次抓取结果本身。
async def _auto_retain_user_memory(user_id: str, user_input: str) -> None:
    if not user_id or not user_input.strip():
        return
    # 本地锁按需创建；创建前先触发一次空闲锁清理，控制字典整体大小
    lock = _auto_capture_user_locks.get(user_id)
    if lock is None:
        _evict_idle_auto_capture_locks()
        lock = asyncio.Lock()
        _auto_capture_user_locks[user_id] = lock
    try:
        async with lock:
            # 每次尝试都生成一个新的临时标记，只用于这一次分布式锁的持有者标识，
            # 与进程级 pubsub 的 instance_id 无关
            instance_id = uuid.uuid4().hex[:8]
            acquire_lock, release_lock = _get_auto_capture_lock_fns()
            lock_state = await acquire_lock(user_id, instance_id)
            if lock_state != "acquired":
                # 本地锁保证了同进程不会重复调用，但没拿到分布式锁说明别的实例正在处理
                # 这个用户，直接放弃本次抓取
                return
            try:
                backend = await _get_backend()
                if backend is None:
                    return
                # 并非所有 backend 都实现了自动抓取能力（这是可选特性），用 hasattr 判断兼容性
                if hasattr(backend, "auto_retain_from_text"):
                    result = await backend.auto_retain_from_text(user_id, user_input)
                    stored = 0
                    if isinstance(result, dict):
                        stored = int(result.get("stored") or 0)
                    logger.info(
                        "[Memory] Auto-retain completed for user %s: stored=%s candidates=%s",
                        user_id,
                        stored,
                        result.get("candidates") if isinstance(result, dict) else None,
                    )
                    if stored > 0:
                        # 只有真的写入了新记忆才需要检查是否要触发压缩，避免空写入也跑一次压缩判断
                        try:
                            compaction_result = (
                                await get_memory_compaction_agent().maybe_compact_after_write(
                                    backend, user_id
                                )
                            )
                            logger.info(
                                "[Memory] Auto-compaction check for user %s: %s",
                                user_id,
                                compaction_result,
                            )
                        except Exception as e:
                            logger.warning(
                                "[Memory] Background memory compaction check failed: %s", e
                            )
            finally:
                # 无论抓取是否成功都要释放分布式锁
                await release_lock(user_id, instance_id)
    finally:
        # 本地锁用完后检查是否可以清理掉（没人持有也没人等待）
        _cleanup_local_auto_capture_lock(user_id, lock)


async def _auto_retain_user_memory_detached(user_id: str, user_input: str) -> None:
    """Run background memory capture without inheriting the chat trace parent."""
    # 用 tracing_context(parent=False) 切断与当前对话 trace 的父子关系，
    # 让这次后台记忆抓取在可观测性系统里表现为一次独立的追踪，
    # 而不会被计入对话本身的耗时/子步骤
    with tracing_context(parent=False):
        await _auto_retain_user_memory(user_id, user_input)


def schedule_auto_memory_capture(user_id: str, user_input: str) -> None:
    """Best-effort background capture of durable user memories from latest input."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环（比如非异步调用上下文）就直接放弃——这正是"尽力而为"的体现
        return

    existing = _auto_capture_tasks_by_user.get(user_id)
    if existing is not None and not existing.done():
        # 同一用户已经有一个抓取任务在跑，不重复调度，避免同一批输入被处理多次
        logger.debug("[Memory] Auto-retain already running for user %s, skipping", user_id)
        return
    active_auto_capture_tasks = sum(
        1 for task in _auto_capture_tasks_by_user.values() if not task.done()
    )
    if active_auto_capture_tasks >= _get_auto_capture_max_tasks():
        # 全进程范围内还在运行的抓取任务数已达上限，跳过本次调度，
        # 防止消息高峰期无限制地为每个用户各开一个后台任务
        logger.warning(
            "[Memory] Auto-retain skipped for user %s: active task limit reached (%s)",
            user_id,
            active_auto_capture_tasks,
        )
        return

    # 先截断超长输入，再创建后台任务；用 detached 版本避免抓取任务的 trace 挂在当前对话下
    clipped_input = _clip_auto_capture_input(user_input)
    logger.info("[Memory] Scheduling auto-retain for user %s", user_id)
    task = loop.create_task(_auto_retain_user_memory_detached(user_id, clipped_input))
    _auto_capture_tasks_by_user[user_id] = task
    _background_tasks.add(task)
    task.add_done_callback(lambda done: _auto_capture_task_done(user_id, done))


async def run_scheduled_memory_compaction() -> dict:
    """Run the scheduled native memory compaction pass."""
    # 供调度器（见下面 start_memory_compaction_agent）周期性调用的入口；
    # backend 不可用时直接返回一个"已跳过"的统计结果，而不是抛异常打断调度循环
    backend = await _get_backend()
    if backend is None:
        return {"checked": 0, "triggered": 0, "skipped": 1, "reason": "backend_unavailable"}
    return await get_memory_compaction_agent().run_periodic_once(backend)


def start_memory_compaction_agent() -> None:
    """Register periodic memory compaction checks with the unified scheduler."""
    if not settings.ENABLE_MEMORY:
        logger.info("[Memory] Auto-compaction scheduler not registered: ENABLE_MEMORY=false")
        return
    agent = get_memory_compaction_agent()
    # 把记忆压缩检查注册为一个使用"可调用 interval/enabled"的调度任务：
    # interval 从 agent.get_periodic_interval_seconds 动态读取（支持运行时调整压缩频率），
    # enabled 同时检查全局开关 ENABLE_MEMORY 与 agent 自身"周期压缩是否开启"的配置，
    # 任一为假都会在触发时跳过执行，而不是取消注册（避免频繁注册/反注册任务）
    get_runtime_scheduler().register_job(
        ScheduledJob.from_interval(
            id="memory.compaction",
            name="Memory compaction",
            interval_seconds=agent.get_periodic_interval_seconds,
            enabled=lambda: bool(settings.ENABLE_MEMORY) and agent.is_periodic_enabled(),
            handler=run_scheduled_memory_compaction,
        )
    )
    logger.info(
        "[Memory] Auto-compaction scheduler registered: enabled=%s threshold=%s interval=%ss",
        agent.is_periodic_enabled(),
        getattr(agent, "threshold", None),
        agent.get_periodic_interval_seconds(),
    )


# ============================================================================
# Backend Lifecycle (hot-swap support)
# ============================================================================


async def _close_and_reset_backend() -> None:
    """Close the current backend (if any) and reset the singleton."""
    global _backend
    lock = _get_backend_lock()
    async with lock:
        # 加锁后先取出旧引用并立即清空单例，缩短持锁时间；
        # 真正的 backend.close()（可能涉及网络 IO）放到锁外执行，避免阻塞其他正在等待
        # _get_backend 的调用
        backend = _backend
        _backend = None
    if backend is not None:
        try:
            await backend.close()
        except Exception as e:
            logger.warning(f"[Memory] Error closing backend during reset: {e}")
    if settings.ENABLE_MEMORY:
        # 重置后如果记忆功能仍然启用，需要重新注册一次压缩调度任务
        # （register_job 对同 id 任务是覆盖式替换，重复调用是安全的）
        start_memory_compaction_agent()
    logger.info("[Memory] Backend reset (will be recreated on next use)")


def _backend_reset_done(task: asyncio.Task) -> None:
    # 后端重置任务结束后的清理回调：仅当它仍是当前记录的重置任务时才清空该引用
    # （避免与新调度的重置任务互相覆盖），并从后台任务集合中移除、记录可能的异常
    global _backend_reset_task
    if _backend_reset_task is task:
        _backend_reset_task = None
    _background_tasks.discard(task)
    _background_task_error(task)


def schedule_backend_reset() -> None:
    """Schedule a non-blocking backend reset (fire-and-forget).

    Call this when memory-related settings change so the next request
    picks up the new configuration without a server restart.
    """
    global _backend_reset_task

    existing = _backend_reset_task
    if existing is not None and not existing.done():
        # 已经有一个重置任务在排队/执行中就不重复调度：重置操作本身是幂等的，没必要叠加多次
        logger.debug("[Memory] Backend reset already scheduled")
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop — reset synchronously (close may be incomplete but safe)
        # 极端情况下（没有运行中的事件循环）无法创建后台任务，只能直接同步清空引用；
        # 代价是旧 backend 的 close() 不会被调用，但这属于边缘场景，安全性优先于完整性
        global _backend
        _backend = None
        _backend_reset_task = None
        logger.info("[Memory] Backend reset (no event loop)")
        return

    task = loop.create_task(_close_and_reset_backend())
    _backend_reset_task = task
    _background_tasks.add(task)
    task.add_done_callback(_backend_reset_done)


async def shutdown() -> None:
    """Cancel all pending background tasks and close the backend.

    Call during application shutdown to prevent orphaned tasks.
    """
    global _backend, _backend_lock, _backend_lock_loop, _backend_reset_task

    # Cancel all background tasks
    # 应用退出：先取消所有仍在运行的后台任务（自动抓取、后端重置等），再等待它们真正结束
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    _background_tasks.clear()
    # 顺带停止记忆压缩 agent 自身可能持有的后台状态
    await stop_memory_compaction_agent()

    # Close backend
    # 最后再关闭 backend 本身，并清空所有模块级状态，
    # 保证下次（比如测试场景）重新初始化时不会残留旧状态
    backend = _backend
    _backend = None
    _backend_lock = None
    _backend_lock_loop = None
    _backend_reset_task = None
    _auto_capture_tasks_by_user.clear()
    _auto_capture_user_locks.clear()
    if backend is not None:
        try:
            await backend.close()
        except Exception as e:
            logger.warning(f"[Memory] Error closing backend during shutdown: {e}")

"""Sandbox MCP Rebuild - Register MCP servers inside sandbox via mcporter.

Extracted from session_manager.py so that both session startup and
individual tool operations can share the same rebuild logic.
"""
# 中文说明：本模块是"沙箱 MCP 动态配置"中最核心也最容易出错的一块——
# 沙箱重建/会话启动时，需要把数据库中记录的用户 MCP 服务器配置，
# 重新同步（reconcile）进这个全新/复用的沙箱环境的 mcporter 配置里。
# 难点主要在三处：
#   1）差异协调：要先算出"当前 mcporter 里已注册的" vs "数据库中期望存在的"，
#      移除多余的（stale，比如用户改名/删除/停用过的），再补齐缺失的；
#   2）并发与去重：同一用户可能同时有多个请求触发 rebuild（例如并发工具调用
#      + 会话启动），需要用进程内 in-flight 任务表 + 短期结果缓存 + 跨进程
#      Redis 分布式锁三层机制，避免同一沙箱被并发重复 rebuild；
#   3）失败隔离：单个 MCP 服务器注册失败不应该阻塞其它服务器注册，也不应该
#      阻塞整个会话启动，因此各处大量使用"失败仅记录日志、不向上抛出"的策略。

import asyncio
import json
import shlex
import time
from typing import Any

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)

# mcporter command timeout per server (seconds)
_MCPORTER_TIMEOUT = 60
# 单个 (user_id, sandbox) 组合最近一次成功 rebuild 后，多久内跳过重复 rebuild（秒）
_REBUILD_CACHE_TTL_SECONDS = 60
# 「最近已 rebuild」本地缓存的最大条目数，防止长期运行下无限增长
_REBUILD_CACHE_MAX_ENTRIES = 1000
# 跨进程 Redis 分布式锁的存活时间（秒），需比单次 rebuild 实际耗时更长，
# 避免锁提前过期导致另一个进程重复执行 rebuild
_REBUILD_LOCK_TTL_SECONDS = 90
# 默认的 rebuild 并发度（同时注册/移除多少个 MCP 服务器）
_DEFAULT_REBUILD_CONCURRENCY = 4
# 进程内缓存：cache_key -> 最近一次成功 rebuild 完成的时间（time.monotonic）
_recent_rebuilds: dict[str, float] = {}
# 进程内缓存：cache_key -> 正在执行中的 rebuild 任务，用于同进程内的请求去重合并
_inflight_rebuilds: dict[str, asyncio.Task[None]] = {}
# 保护上面两个 dict 的锁；延迟到首次使用时才创建（见 _get_inflight_rebuilds_lock）
_inflight_rebuilds_lock: asyncio.Lock | None = None


def _get_rebuild_concurrency() -> int:
    # 从配置读取并发度，非正数时回退到默认值，且始终保证至少为 1
    return max(
        int(
            getattr(
                settings,
                "SANDBOX_MCP_REBUILD_CONCURRENCY",
                _DEFAULT_REBUILD_CONCURRENCY,
            )
            or 0
        ),
        1,
    )


def _get_backend_sandbox_id(backend: Any) -> str:
    # 中文：传入的 backend 可能是 CompositeBackend（用 .default 包了一层真正的沙箱后端），
    # 也可能直接就是沙箱后端本身；优先取其显式 id 属性，
    # 没有的话用 Python 对象 id() 兜底，保证同一个后端实例在本进程内始终对应同一个 key
    sandbox_backend = getattr(backend, "default", backend)
    sandbox_id = getattr(sandbox_backend, "id", None)
    if sandbox_id:
        return str(sandbox_id)
    return str(id(sandbox_backend))


def _rebuild_cache_key(backend: Any, user_id: str) -> str:
    # 缓存/锁的 key 由用户 id + 沙箱实例 id 组成：同一用户换了新沙箱也要重新 rebuild
    return f"{user_id}:{_get_backend_sandbox_id(backend)}"


def clear_sandbox_mcp_rebuild_cache() -> None:
    # 中文：清空进程内的"最近已 rebuild"与"正在进行中"两个缓存，主要用于测试场景重置状态
    _recent_rebuilds.clear()
    _inflight_rebuilds.clear()


def _get_inflight_rebuilds_lock() -> asyncio.Lock:
    # 中文：懒加载创建 asyncio.Lock，避免在模块导入阶段（可能还没有运行中的事件循环）
    # 就创建 Lock 对象
    global _inflight_rebuilds_lock
    if _inflight_rebuilds_lock is None:
        _inflight_rebuilds_lock = asyncio.Lock()
    return _inflight_rebuilds_lock


def _recently_rebuilt(cache_key: str, now: float | None = None) -> bool:
    # 中文：判断该 cache_key 是否在 TTL 时间内已经成功 rebuild 过，
    # 若已过期则顺手把这条记录清掉
    current = time.monotonic() if now is None else now
    last_rebuilt = _recent_rebuilds.get(cache_key)
    if last_rebuilt is None:
        return False
    if current - last_rebuilt < _REBUILD_CACHE_TTL_SECONDS:
        return True
    _recent_rebuilds.pop(cache_key, None)
    return False


def _prune_recent_rebuild_cache(now: float | None = None) -> None:
    # 中文：先清理所有已过 TTL 的条目
    current = time.monotonic() if now is None else now
    expired_keys = [
        key
        for key, last_rebuilt in _recent_rebuilds.items()
        if current - last_rebuilt >= _REBUILD_CACHE_TTL_SECONDS
    ]
    for key in expired_keys:
        _recent_rebuilds.pop(key, None)

    # 清理过期条目后仍超过条目数上限时，按时间从旧到新淘汰多余的部分
    overflow = len(_recent_rebuilds) - _REBUILD_CACHE_MAX_ENTRIES
    if overflow <= 0:
        return

    def _last_rebuild_at(cache_key: str) -> float:
        return _recent_rebuilds[cache_key]

    oldest_keys = sorted(_recent_rebuilds, key=_last_rebuild_at)[:overflow]
    for key in oldest_keys:
        _recent_rebuilds.pop(key, None)


async def _acquire_distributed_rebuild_lock(cache_key: str) -> bool:
    # 中文：用 Redis 的 SET NX EX 语义实现跨进程/跨实例的分布式锁——
    # 只有第一个成功 SET 成功（返回真值）的进程才允许真正执行 rebuild，
    # 其它并发进程会因为 key 已存在而 SET 失败，从而跳过本次 rebuild。
    # 如果 Redis 不可用（比如未部署/网络异常），则退化为"总是允许 rebuild"，
    # 因为多进程重复 rebuild 只是浪费一些资源，并不会破坏正确性，
    # 属于宁可多做也不要因为基础设施异常而完全跳过的场景（fail-open）。
    try:
        from src.infra.storage.redis import get_redis_client

        redis_client = get_redis_client()
        return bool(
            await redis_client.set(
                f"sandbox:mcp:rebuild:{cache_key}",
                str(time.time()),
                ex=_REBUILD_LOCK_TTL_SECONDS,
                nx=True,
            )
        )
    except Exception as e:
        logger.debug(f"[Sandbox MCP Rebuild] Redis rebuild lock unavailable: {e}")
        return True


async def _get_mcporter_server_names(backend: Any) -> set[str]:
    """Return the set of server names currently registered in mcporter."""
    # 中文：查询沙箱内 mcporter 当前实际已注册的服务器名集合，
    # 用于和数据库中"期望存在"的集合做差异比较；任何异常都视为空集合，
    # 这样至多导致多注册几次，不会因为查询失败而中断整个 rebuild 流程
    try:
        result = await backend.aexecute("mcporter list --json", timeout=15)
        if result.exit_code != 0:
            return set()
        data = await run_blocking_io(json.loads, result.output)
        servers = data.get("servers", [])
        return {s.get("name", "") for s in servers if isinstance(s, dict) and s.get("name")}
    except Exception:
        return set()


async def _run_limited(
    items: list[Any],
    worker_func,
    *,
    concurrency: int,
) -> None:
    # 中文：一个简单的"有限并发工作池"实现——用共享的 next_index 指针 + 锁
    # 让多个 worker 协程轮流领取下一个待处理项，从而把 items 的处理并发度
    # 限制在 concurrency 以内（例如避免同时对沙箱发起过多 mcporter 命令）
    if not items:
        return

    next_index = 0
    lock = asyncio.Lock()

    async def _worker() -> None:
        nonlocal next_index
        while True:
            async with lock:
                if next_index >= len(items):
                    return
                item = items[next_index]
                next_index += 1
            await worker_func(item)

    worker_count = min(max(concurrency, 1), len(items))
    await asyncio.gather(*(_worker() for _ in range(worker_count)))


async def rebuild_sandbox_mcp(backend: Any, user_id: str) -> None:
    """Register all user's sandbox MCP servers inside the sandbox via mcporter.

    This is called at session startup to ensure mcporter config is up to date
    with the latest env var values. Failures are logged but not propagated
    (individual server failures don't block the session).

    Args:
        backend: The sandbox backend to run mcporter commands on.
        user_id: User ID whose MCP servers to register.
    """
    from src.infra.envvar.storage import EnvVarStorage
    from src.infra.mcp.storage import MCPStorage
    from src.infra.tool.sandbox_mcp_utils import build_env_flags

    mcp_storage = MCPStorage()
    env_storage = EnvVarStorage()

    logger.info(f"[Sandbox MCP Rebuild] Starting rebuild for user {user_id}")

    # Check mcporter availability
    # 沙箱镶像若未安装 mcporter，则本次 rebuild 直接跳过（非错误，仅是能力缺失）
    version_result = await backend.aexecute("mcporter --version", timeout=10)
    if version_result.exit_code != 0:
        logger.info(
            f"[Sandbox MCP Rebuild] mcporter not available (exit={version_result.exit_code}, output={version_result.output}), skipping"
        )
        return
    logger.info(f"[Sandbox MCP Rebuild] mcporter version: {version_result.output.strip()}")

    # Get sandbox-transport MCP servers (with role-based filtering)
    # 中文：只取 transport=SANDBOX 类型的服务器配置，并按用户角色/配额过滤
    # （管理员可能能看到更多，普通用户受角色策略限制）
    from src.infra.mcp.quota import resolve_user_mcp_access

    user_roles, is_admin = await resolve_user_mcp_access(user_id)
    sandbox_servers = await mcp_storage.get_sandbox_servers(
        user_id,
        user_roles=user_roles,
        is_admin=is_admin,
    )
    logger.info(f"[Sandbox MCP Rebuild] Found {len(sandbox_servers)} sandbox servers")

    # Compute the set of server names that *should* be registered
    # 中文：desired_names 是"数据库视角下这个用户应该拥有的服务器名集合"，
    # 没有 command 的配置视为无效，不计入期望集合
    desired_names: set[str] = set()
    for server_config in sandbox_servers:
        server_name = server_config.get("name", "")
        if not server_config.get("command", ""):
            continue
        desired_names.add(server_name)

    # Remove stale servers from mcporter (disabled, deleted, or renamed)
    # 中文：current_names 是"沙箱里当前实际已注册的服务器名集合"；
    # current - desired 得到的就是应该被移除的多余/过期配置
    # （比如用户在别处禁用、删除或重命名了某个服务器）
    current_names = await _get_mcporter_server_names(backend)
    stale_names = current_names - desired_names
    if stale_names:
        logger.info(f"[Sandbox MCP Rebuild] Stale servers to remove: {stale_names}")

    async def _remove_stale_server(name: str) -> None:
        result = await backend.aexecute(
            f"mcporter config remove {shlex.quote(name)}", timeout=_MCPORTER_TIMEOUT
        )
        if result.exit_code != 0:
            logger.warning(f"[Sandbox MCP Rebuild] Failed to remove '{name}': {result.output}")
        else:
            logger.info(f"[Sandbox MCP Rebuild] Removed stale MCP server '{name}' from sandbox")

    rebuild_concurrency = _get_rebuild_concurrency()
    await _run_limited(
        sorted(stale_names),
        _remove_stale_server,
        concurrency=rebuild_concurrency,
    )

    if not sandbox_servers:
        logger.info(f"[Sandbox MCP Rebuild] No sandbox MCP servers for user {user_id}")
        return

    # Get user's env vars for injection
    env_vars = await env_storage.get_decrypted_vars(user_id)

    # Register each server with mcporter
    async def _register_server(server_config: dict[str, Any]) -> None:
        server_name = server_config.get("name", "")
        command = server_config.get("command", "")
        env_keys = server_config.get("env_keys", [])

        if not command:
            return

        env_flags = await build_env_flags(user_id, env_keys)

        # Remove first, then add (same pattern as sandbox_mcp_tool update)
        # to ensure config is up-to-date even if server was previously registered.
        # Failure is OK — server may not have been registered yet.
        try:
            await backend.aexecute(
                f"mcporter config remove {shlex.quote(server_name)}", timeout=_MCPORTER_TIMEOUT
            )
        except Exception:
            pass

        cmd = f"mcporter config add {shlex.quote(server_name)} --stdio {shlex.quote(command)}{env_flags}"
        result = await backend.aexecute(cmd, timeout=_MCPORTER_TIMEOUT)
        if result.exit_code != 0:
            logger.info(
                f"[Sandbox MCP Rebuild] Failed to register '{server_name}': exit={result.exit_code}, output={result.output}"
            )
        else:
            logger.info(f"[Sandbox MCP Rebuild] Registered MCP server '{server_name}' in sandbox")

    # 中文：即使数据库里有若干服务器，也用有限并发逐个"先删后加"重新注册一遍，
    # 保证 env_keys 对应的真实变量值总是最新的（用户可能在别处改过环境变量）
    await _run_limited(
        [server for server in sandbox_servers if server.get("command", "")],
        _register_server,
        concurrency=rebuild_concurrency,
    )

    # Preheat npx caches in background
    # 中文：注册完成后预热 npx 包缓存，减少 agent 第一次实际调用该工具时的等待时间
    await _preheat_mcp_cache(backend, sandbox_servers, env_vars)


async def ensure_sandbox_mcp(
    backend: Any,
    user_id: str,
    *,
    force_rebuild: bool = False,
) -> None:
    """Rebuild sandbox MCP config, sync env vars, and invalidate prompt cache.

    Convenience wrapper called at every session startup path (cache hit,
    resume, new sandbox) to ensure mcporter config reflects the latest
    env var values and user environment variables are up-to-date.

    Args:
        backend: The sandbox backend.
        user_id: User ID.
    """
    from src.infra.tool.sandbox_mcp_prompt import invalidate_sandbox_mcp_prompt_cache

    cache_key = _rebuild_cache_key(backend, user_id)
    # 中文：三层去重/限流机制，从外到内依次生效——
    #   第一层（本地短期缓存）：force_rebuild 为假且最近 TTL 内已经 rebuild 过，直接跳过；
    #   第二层（进程内 in-flight 任务表）：同进程内若已有同 cache_key 的 rebuild 正在跑，
    #     直接复用同一个 task 等待其结果，而不是再起一个重复任务；
    #   第三层（Redis 分布式锁）：多进程/多实例情况下，只有抢到锁的那个进程真正执行 rebuild。
    if force_rebuild or not _recently_rebuilt(cache_key):
        async with _get_inflight_rebuilds_lock():
            task = _inflight_rebuilds.get(cache_key)
            if task is None or task.done():
                should_rebuild = await _acquire_distributed_rebuild_lock(cache_key)
                if should_rebuild:
                    task = asyncio.create_task(rebuild_sandbox_mcp(backend, user_id))
                    _inflight_rebuilds[cache_key] = task
                else:
                    # 抢锁失败：说明别的实例正在/刚刚做过 rebuild，本次不再重复执行
                    task = None

        if task is not None:
            try:
                # 等待（本进程新建的，或者刚好复用到的）rebuild 任务完成
                await task
                now = time.monotonic()
                _recent_rebuilds[cache_key] = now
                _prune_recent_rebuild_cache(now)
            finally:
                # 任务结束后从 in-flight 表中摘除，但要确认表里存的仍是"我们等待的这个任务"，
                # 避免把后来新提交的同 key 任务误删
                async with _get_inflight_rebuilds_lock():
                    if _inflight_rebuilds.get(cache_key) is task:
                        _inflight_rebuilds.pop(cache_key, None)
        else:
            logger.debug(
                f"[Sandbox MCP Rebuild] Skipping rebuild for user {user_id}: another instance recently acquired lock"
            )
    # 无论是否执行了 rebuild，都要重新同步一次用户环境变量到 backend，
    # 并让系统提示词的沙箱工具缓存失效，确保后续对话看到的是最新状态
    await _sync_user_env_vars(backend, user_id)
    invalidate_sandbox_mcp_prompt_cache(user_id)


async def _sync_user_env_vars(backend: Any, user_id: str) -> None:
    """Sync user environment variables into the sandbox backend.

    Loads env vars from storage and sets them on the sandbox backend so
    every subsequent execute() call passes them via the SDK's envs/env_vars
    parameter (no files written to disk).

    Args:
        backend: The CompositeBackend wrapping the sandbox backend.
        user_id: User ID.
    """
    from src.infra.envvar.storage import EnvVarStorage

    try:
        env_storage = EnvVarStorage()
        env_vars = await env_storage.get_decrypted_vars(user_id)
    except Exception as e:
        logger.warning(f"[Sandbox Env Sync] Failed to load env vars for user {user_id}: {e}")
        return

    # Set env vars on the underlying sandbox backend
    # 中文：直接把解密后的环境变量字典挂到 backend.env_vars 属性上（若存在该属性），
    # 后续每次 execute() 调用时由 SDK 通过 envs/env_vars 参数传入，
    # 不写入沙箱磁盘文件，避免明文环境变量落盘造成的信息泄露风险
    sandbox_backend = getattr(backend, "default", backend)
    if hasattr(sandbox_backend, "env_vars"):
        sandbox_backend.env_vars = env_vars or {}

    count = len(env_vars) if env_vars else 0
    logger.info(f"[Sandbox Env Sync] Synced {count} env vars for user {user_id}")


async def _preheat_mcp_cache(
    backend: Any,
    servers: list[dict[str, Any]],
    env_vars: dict[str, str],
) -> None:
    """Preheat npx/npm caches for sandbox MCP commands.

    For commands starting with 'npx', runs a dry install so the package is
    already cached when the agent first calls a tool.
    """

    async def _preheat_server(server_config: dict[str, Any]) -> None:
        command = server_config.get("command", "")
        env_keys = server_config.get("env_keys", [])

        # 只预热 npx 命令：npx 首次运行的包下载耗时较长，是 agent 第一次调用工具时
        # 最容易感知到延迟的地方；其它类型命令（如本地脚本）不需要预热
        if not command or not command.startswith("npx"):
            return

        # Build env string for the preheat command
        env_str = ""
        for key in env_keys:
            val = env_vars.get(key, "")
            env_str += f" {shlex.quote(key)}={shlex.quote(val)}"

        # Extract package name from npx command (e.g., "npx -y @scope/pkg" -> "@scope/pkg")
        # 中文：简单地按空格切分命令行，跳过 npx 本身、形如 -y/--yes 的开关，
        # 以及带值的选项（形如 "-x value"，遇到以 "-" 开头的 token 时假设下一个 token 是其值）
        parts = command.split()
        pkg = ""
        skip_next = False
        for part in parts:
            if skip_next:
                skip_next = False
                continue
            if part in ("-y", "--yes"):
                continue
            if part.startswith("-"):
                skip_next = True
                continue
            if part and not part.startswith("npx"):
                pkg = part
                break

        if not pkg:
            return

        # 中文：用 npm install --prefer-offline 做一次"预热安装"，
        # 目的只是把包放进 npm/npx 缓存，真正调用时可以命中缓存加速；
        # 输出只保留最后一行，避免安装日志刷屏
        preheat_cmd = f"env{env_str} npm install --prefer-offline {shlex.quote(pkg)} 2>&1 | tail -1"
        try:
            result = await backend.aexecute(preheat_cmd, timeout=60)
            logger.debug(f"[Sandbox MCP Rebuild] Preheat '{pkg}': {result.output.strip()}")
        except Exception as e:
            # 预热失败是非致命的：最多退化为"agent 第一次调用时才现场安装"，不影响功能正确性
            logger.debug(f"[Sandbox MCP Rebuild] Preheat '{pkg}' failed (non-fatal): {e}")

    await _run_limited(
        servers,
        _preheat_server,
        concurrency=_get_rebuild_concurrency(),
    )

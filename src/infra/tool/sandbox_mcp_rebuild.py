"""Sandbox MCP Rebuild - Register MCP servers inside sandbox via mcporter.

Extracted from session_manager.py so that both session startup and
individual tool operations can share the same rebuild logic.
"""

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
_REBUILD_CACHE_TTL_SECONDS = 60
_REBUILD_CACHE_MAX_ENTRIES = 1000
_REBUILD_LOCK_TTL_SECONDS = 90
_DEFAULT_REBUILD_CONCURRENCY = 4
_recent_rebuilds: dict[str, float] = {}
_inflight_rebuilds: dict[str, asyncio.Task[None]] = {}
_inflight_rebuilds_lock: asyncio.Lock | None = None


def _get_rebuild_concurrency() -> int:
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
    sandbox_backend = getattr(backend, "default", backend)
    sandbox_id = getattr(sandbox_backend, "id", None)
    if sandbox_id:
        return str(sandbox_id)
    return str(id(sandbox_backend))


def _rebuild_cache_key(backend: Any, user_id: str) -> str:
    return f"{user_id}:{_get_backend_sandbox_id(backend)}"


def clear_sandbox_mcp_rebuild_cache() -> None:
    _recent_rebuilds.clear()
    _inflight_rebuilds.clear()


def _get_inflight_rebuilds_lock() -> asyncio.Lock:
    global _inflight_rebuilds_lock
    if _inflight_rebuilds_lock is None:
        _inflight_rebuilds_lock = asyncio.Lock()
    return _inflight_rebuilds_lock


def _recently_rebuilt(cache_key: str, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    last_rebuilt = _recent_rebuilds.get(cache_key)
    if last_rebuilt is None:
        return False
    if current - last_rebuilt < _REBUILD_CACHE_TTL_SECONDS:
        return True
    _recent_rebuilds.pop(cache_key, None)
    return False


def _prune_recent_rebuild_cache(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    expired_keys = [
        key
        for key, last_rebuilt in _recent_rebuilds.items()
        if current - last_rebuilt >= _REBUILD_CACHE_TTL_SECONDS
    ]
    for key in expired_keys:
        _recent_rebuilds.pop(key, None)

    overflow = len(_recent_rebuilds) - _REBUILD_CACHE_MAX_ENTRIES
    if overflow <= 0:
        return

    oldest_keys = sorted(_recent_rebuilds, key=_recent_rebuilds.get)[:overflow]
    for key in oldest_keys:
        _recent_rebuilds.pop(key, None)


async def _acquire_distributed_rebuild_lock(cache_key: str) -> bool:
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
    version_result = await backend.aexecute("mcporter --version", timeout=10)
    if version_result.exit_code != 0:
        logger.info(
            f"[Sandbox MCP Rebuild] mcporter not available (exit={version_result.exit_code}, output={version_result.output}), skipping"
        )
        return
    logger.info(f"[Sandbox MCP Rebuild] mcporter version: {version_result.output.strip()}")

    # Get sandbox-transport MCP servers (with role-based filtering)
    from src.infra.mcp.quota import resolve_user_mcp_access

    user_roles, is_admin = await resolve_user_mcp_access(user_id)
    sandbox_servers = await mcp_storage.get_sandbox_servers(
        user_id,
        user_roles=user_roles,
        is_admin=is_admin,
    )
    logger.info(f"[Sandbox MCP Rebuild] Found {len(sandbox_servers)} sandbox servers")

    # Compute the set of server names that *should* be registered
    desired_names: set[str] = set()
    for server_config in sandbox_servers:
        server_name = server_config.get("name", "")
        if not server_config.get("command", ""):
            continue
        desired_names.add(server_name)

    # Remove stale servers from mcporter (disabled, deleted, or renamed)
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

    await _run_limited(
        [server for server in sandbox_servers if server.get("command", "")],
        _register_server,
        concurrency=rebuild_concurrency,
    )

    # Preheat npx caches in background
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
    if force_rebuild or not _recently_rebuilt(cache_key):
        async with _get_inflight_rebuilds_lock():
            task = _inflight_rebuilds.get(cache_key)
            if task is None or task.done():
                should_rebuild = await _acquire_distributed_rebuild_lock(cache_key)
                if should_rebuild:
                    task = asyncio.create_task(rebuild_sandbox_mcp(backend, user_id))
                    _inflight_rebuilds[cache_key] = task
                else:
                    task = None

        if task is not None:
            try:
                await task
                now = time.monotonic()
                _recent_rebuilds[cache_key] = now
                _prune_recent_rebuild_cache(now)
            finally:
                async with _get_inflight_rebuilds_lock():
                    if _inflight_rebuilds.get(cache_key) is task:
                        _inflight_rebuilds.pop(cache_key, None)
        else:
            logger.debug(
                f"[Sandbox MCP Rebuild] Skipping rebuild for user {user_id}: another instance recently acquired lock"
            )
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

        if not command or not command.startswith("npx"):
            return

        # Build env string for the preheat command
        env_str = ""
        for key in env_keys:
            val = env_vars.get(key, "")
            env_str += f" {shlex.quote(key)}={shlex.quote(val)}"

        # Extract package name from npx command (e.g., "npx -y @scope/pkg" -> "@scope/pkg")
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

        preheat_cmd = f"env{env_str} npm install --prefer-offline {shlex.quote(pkg)} 2>&1 | tail -1"
        try:
            result = await backend.aexecute(preheat_cmd, timeout=60)
            logger.debug(f"[Sandbox MCP Rebuild] Preheat '{pkg}': {result.output.strip()}")
        except Exception as e:
            logger.debug(f"[Sandbox MCP Rebuild] Preheat '{pkg}' failed (non-fatal): {e}")

    await _run_limited(
        servers,
        _preheat_server,
        concurrency=_get_rebuild_concurrency(),
    )

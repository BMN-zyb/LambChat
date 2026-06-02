"""Synchronization helpers for environment variable changes."""

from __future__ import annotations

from typing import Any

from src.infra.tool.cache_pubsub import publish_tool_cache_invalidation
from src.infra.tool.env_var_prompt import invalidate_env_var_prompt_cache
from src.infra.tool.sandbox_mcp_rebuild import ensure_sandbox_mcp


def get_session_sandbox_manager():
    from src.infra.sandbox.session_manager import get_session_sandbox_manager as _get_manager

    return _get_manager()


async def sync_envvar_change(user_id: str, *, backend: Any | None = None) -> None:
    """Invalidate local caches, broadcast to peers, and refresh sandbox state when possible."""
    invalidate_env_var_prompt_cache(user_id)
    await publish_tool_cache_invalidation("env_var_prompt", user_id=user_id)

    if backend is None:
        try:
            backend = get_session_sandbox_manager().get_cached_backend(user_id)
        except Exception:
            backend = None

    if backend is not None:
        await ensure_sandbox_mcp(backend, user_id, force_rebuild=True)

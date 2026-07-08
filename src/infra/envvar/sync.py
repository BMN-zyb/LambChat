"""Synchronization helpers for environment variable changes."""

from __future__ import annotations

from typing import Any

from src.infra.tool.cache_pubsub import publish_tool_cache_invalidation
from src.infra.tool.env_var_prompt import invalidate_env_var_prompt_cache
from src.infra.tool.sandbox_mcp_rebuild import ensure_sandbox_mcp


def get_session_sandbox_manager():
    # 延迟导入以避免模块加载期的循环依赖：sandbox.session_manager 可能间接
    # 依赖到本模块所在的调用链，故只在函数实际被调用时才导入
    from src.infra.sandbox.session_manager import get_session_sandbox_manager as _get_manager

    return _get_manager()


async def sync_envvar_change(user_id: str, *, backend: Any | None = None) -> None:
    """Invalidate local caches, broadcast to peers, and refresh sandbox state when possible."""
    # 环境变量新增/修改/删除后必须做的三件联动副作用，缺一都可能导致状态不一致：
    # 1) 失效本进程内的 env_var_prompt 本地缓存，避免继续使用旧值拼接提示词
    invalidate_env_var_prompt_cache(user_id)
    # 2) 通过 pub/sub 广播给其他实例，让所有进程的本地缓存都失效
    #    （单机内已经调用过 invalidate_env_var_prompt_cache，这里是为了多实例部署场景）
    await publish_tool_cache_invalidation("env_var_prompt", user_id=user_id)

    if backend is None:
        try:
            # 若调用方没有显式传入 backend，尝试从会话级沙箱管理器里取出
            # 该用户当前已缓存的沙箱后端；取不到（例如用户还没有活跃沙箱）就忽略
            backend = get_session_sandbox_manager().get_cached_backend(user_id)
        except Exception:
            backend = None

    if backend is not None:
        # 3) 若用户当前有存活的沙箱，强制重建其 MCP 配置，
        #    确保新的环境变量能立即注入到沙箱内运行的 MCP 服务进程中，
        #    否则沙箱会继续使用旧的环境变量直到下次重启
        await ensure_sandbox_mcp(backend, user_id, force_rebuild=True)

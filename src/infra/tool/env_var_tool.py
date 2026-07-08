"""LLM-callable environment variable tools.

The tools allow the agent to manage the current user's encrypted environment
variables without ever reading plaintext values back into model context.
"""
# 中文说明：本模块提供环境变量的增删查工具，供 LLM 在对话中帮用户管理
# 那些要注入到沙箱 MCP 服务器（sandbox_mcp_add 的 env_keys 参数）的密钥/凭据。
# 安全设计要点：所有返回给 LLM 的变量值都会被打码为 "***"（见 _masked_var），
# 明文值只在写入/读取加密存储（EnvVarStorage）内部流转，永不进入模型上下文，
# 防止密钥通过对话历史或日志泄露。

import json
import re
import sys
from typing import TYPE_CHECKING, Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolArg

from src.infra.async_utils import run_blocking_io
from src.infra.envvar.storage import EnvVarStorage
from src.infra.tool.backend_utils import get_backend_from_runtime, get_user_id_from_runtime
from src.infra.tool.cache_pubsub import publish_tool_cache_invalidation
from src.infra.tool.env_var_prompt import invalidate_env_var_prompt_cache
from src.infra.tool.sandbox_mcp_rebuild import ensure_sandbox_mcp

if TYPE_CHECKING:
    from langchain.tools import ToolRuntime
else:
    try:
        from langchain.tools import ToolRuntime  # type: ignore[assignment]
    except ImportError:  # pragma: no cover
        # 兼容旧版本 langchain：找不到 ToolRuntime 时动态构造占位模块
        _mod = type(sys)("langchain.tools")  # type: ignore[assignment]
        _mod.ToolRuntime = Any  # type: ignore[assignment]
        sys.modules.setdefault("langchain.tools", _mod)
        from langchain.tools import ToolRuntime  # type: ignore[assignment]

from langchain.tools import tool  # noqa: E402

# 环境变量 key 的合法格式：字母/下划线开头，后续可跟字母、数字、下划线
# （与 shell 环境变量命名规范一致，确保能安全用作 mcporter --env KEY=VALUE 的 KEY）
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def _json_dumps_result(data: dict[str, Any]) -> str:
    # 统一以 JSON 字符串形式返回工具结果给 LLM
    return await run_blocking_io(json.dumps, data, ensure_ascii=False, default=str)


def _get_user_id(runtime: ToolRuntime) -> str | None:
    # 从 runtime 中解析当前用户 id；取不到时返回 None 而不是空字符串，便于调用方判空
    user_id = get_user_id_from_runtime(runtime)
    return user_id if user_id else None


async def _sync_envvar_change(user_id: str, backend: Any | None) -> None:
    # 中文：环境变量增/删/清空后需要做的收尾同步——
    #   1）失效该用户的环境变量提示词缓存，并广播通知其它进程一并失效；
    #   2）若当前有沙箱 backend 在用，强制触发一次 sandbox MCP rebuild，
    #      让沙箱内已注册的 MCP 服务器立刻拿到最新的环境变量值
    #      （否则要等到下次会话/沙箱重建才会生效）
    invalidate_env_var_prompt_cache(user_id)
    await publish_tool_cache_invalidation("env_var_prompt", user_id=user_id)
    if backend is not None:
        await ensure_sandbox_mcp(backend, user_id, force_rebuild=True)


def _validate_key(key: str) -> str | None:
    # 返回 None 表示合法；否则返回给 LLM 的错误提示文本
    if _ENV_KEY_PATTERN.match(key):
        return None
    return "Invalid key format. Must match: ^[A-Za-z_][A-Za-z0-9_]*$"


def _masked_var(variable: Any) -> dict[str, Any]:
    # 中文：把真实 value 替换为固定的 "***" 占位符再返回，
    # 确保任何工具调用结果都不会把明文密钥值暴露给模型上下文
    return {
        "key": variable.key,
        "value": "***",
        "created_at": variable.created_at,
        "updated_at": variable.updated_at,
    }


@tool
async def env_var_list(
    runtime: Annotated[ToolRuntime, InjectedToolArg],
) -> str:
    """List the current user's saved environment variable keys.
    Values are always masked and plaintext secrets are never returned."""
    user_id = _get_user_id(runtime)
    if not user_id:
        return await _json_dumps_result({"error": "No user context available"})

    try:
        variables = await EnvVarStorage().list_vars(user_id)
    except Exception as e:
        return await _json_dumps_result({"error": f"Failed to list variables: {e}"})
    masked = [_masked_var(variable) for variable in variables]
    return await _json_dumps_result({"variables": masked, "count": len(masked)})


@tool
async def env_var_set(
    key: Annotated[str, "Environment variable key. Must match ^[A-Za-z_][A-Za-z0-9_]*$."],
    value: Annotated[str, "Environment variable value to store encrypted."],
    runtime: Annotated[ToolRuntime, InjectedToolArg],
) -> str:
    """Create or update one encrypted environment variable for the current user.
    Use this when configuring sandbox MCP env_keys. The saved value is never
    returned; responses contain only a masked value."""
    user_id = _get_user_id(runtime)
    if not user_id:
        return await _json_dumps_result({"error": "No user context available"})

    validation_error = _validate_key(key)
    if validation_error:
        return await _json_dumps_result({"error": validation_error})

    try:
        # EnvVarStorage.set_var 内部负责加密后落库；变量值在这里只是"过手"，从未落地为日志
        variable = await EnvVarStorage().set_var(user_id, key, value)
        backend = get_backend_from_runtime(runtime)
        # 写入成功后立即同步：刷新提示词缓存 + （如有沙箱）强制 rebuild 使新值生效
        await _sync_envvar_change(user_id, backend)
    except Exception as e:
        return await _json_dumps_result({"error": f"Failed to save variable: {e}"})
    return await _json_dumps_result(
        {
            "success": True,
            "message": f"Environment variable '{key}' saved",
            "variable": _masked_var(variable),
        }
    )


@tool
async def env_var_delete(
    key: Annotated[str, "Environment variable key. Must match ^[A-Za-z_][A-Za-z0-9_]*$."],
    runtime: Annotated[ToolRuntime, InjectedToolArg],
) -> str:
    """Delete one environment variable for the current user by key."""
    user_id = _get_user_id(runtime)
    if not user_id:
        return await _json_dumps_result({"error": "No user context available"})

    validation_error = _validate_key(key)
    if validation_error:
        return await _json_dumps_result({"error": validation_error})

    try:
        deleted = await EnvVarStorage().delete_var(user_id, key)
    except Exception as e:
        return await _json_dumps_result({"error": f"Failed to delete variable: {e}"})
    if not deleted:
        return await _json_dumps_result({"error": f"Environment variable '{key}' not found"})
    backend = get_backend_from_runtime(runtime)
    await _sync_envvar_change(user_id, backend)
    return await _json_dumps_result(
        {"success": True, "message": f"Environment variable '{key}' deleted"}
    )


@tool
async def env_var_delete_all(
    runtime: Annotated[ToolRuntime, InjectedToolArg],
) -> str:
    """Delete all environment variables for the current user. Use only when the
    user explicitly asks to clear all environment variables."""
    user_id = _get_user_id(runtime)
    if not user_id:
        return await _json_dumps_result({"error": "No user context available"})

    try:
        count = await EnvVarStorage().delete_all_vars(user_id)
        backend = get_backend_from_runtime(runtime)
        await _sync_envvar_change(user_id, backend)
    except Exception as e:
        return await _json_dumps_result({"error": f"Failed to delete all variables: {e}"})
    return await _json_dumps_result(
        {
            "success": True,
            "message": f"Deleted {count} environment variable(s)",
            "deleted_count": count,
        }
    )


def get_env_var_tools() -> list[BaseTool]:
    """Return safe environment variable CRUD tools for the current user."""
    # 中文：故意不把 env_var_delete_all 放进默认工具集，避免 LLM 在常规场景下
    # 误触发"清空全部环境变量"这种破坏性较大的操作（该工具仍可被显式引用/注册）
    return [env_var_list, env_var_set, env_var_delete]

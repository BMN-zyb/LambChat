"""Sandbox MCP Tools - Manage MCP servers inside the sandbox via mcporter CLI.

Exposes three independent tools so the LLM can manage MCP servers:
  - sandbox_mcp_add:      Register a new MCP server (persists to MongoDB)
  - sandbox_mcp_update:   Update an existing MCP server's command/env (persists to MongoDB)
  - sandbox_mcp_remove:   Unregister an MCP server (persists to MongoDB)

Note: sandbox_mcp_list and sandbox_mcp_call were removed. The LLM discovers
tools via the system prompt and calls/discovers them directly via bash + mcporter.
"""
# 中文说明：本模块实现"沙箱内 MCP 服务器管理"工具集，供 LLM 在对话中动态
# 增/改/删 MCP server 配置。核心难点在于"双写一致性"：既要在沙箱内通过
# mcporter CLI 实际生效，也要把配置持久化到 MongoDB（MCPStorage），
# 以便沙箱被重建/重启后能够自动恢复用户此前配置的 MCP 服务器。

import json
import shlex
import sys
from typing import TYPE_CHECKING, Annotated, Any, Optional

from langchain_core.tools import BaseTool, InjectedToolArg

from src.infra.tool.sandbox_mcp_utils import build_env_flags

# 中文说明：ToolRuntime 类型在 langchain_core >= 1.2.20 版本后从 langchain.tools 导出，
# 不同版本导入路径不同，下面做兼容处理。
# ToolRuntime moved to langchain.tools in langchain_core >= 1.2.20.
# Must be a real runtime import (not TYPE_CHECKING) because InjectedToolArg
# needs to inspect the actual type annotation at runtime.
if TYPE_CHECKING:
    from langchain.tools import ToolRuntime
else:
    try:
        from langchain.tools import ToolRuntime  # type: ignore[assignment]
    except ImportError:  # pragma: no cover
        # 兼容旧版本 langchain：若 langchain.tools 下没有 ToolRuntime，
        # 则动态构造一个假模块，并把 ToolRuntime 设为 Any 类型占位，
        # 避免因版本差异导致导入直接失败。
        _mod = type(sys)("langchain.tools")  # type: ignore[assignment]
        _mod.ToolRuntime = Any  # type: ignore[assignment]
        sys.modules.setdefault("langchain.tools", _mod)
        from langchain.tools import ToolRuntime  # type: ignore[assignment]

from langchain.tools import tool  # noqa: E402

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.tool.backend_utils import (
    get_backend_from_runtime,
    get_user_id_from_runtime,
)
from src.infra.tool.cache_pubsub import publish_tool_cache_invalidation
from src.infra.tool.sandbox_mcp_prompt import invalidate_sandbox_mcp_prompt_cache

logger = get_logger(__name__)

# mcporter command timeout (seconds)
# mcporter 命令超时时间（秒）
_MCPORTER_TIMEOUT = 60
# mcporter 出错时回传给 LLM 的输出最大字符数，超出会被截断，避免污染上下文
_MCPORTER_ERROR_OUTPUT_MAX_CHARS = 8000


# 将 mcporter 命令的输出转换为字符串，并在超长时进行截断，
# 防止把沙箱命令的海量输出（如异常堆栈、日志）原样塞进 LLM 上下文
def _truncate_tool_output(output: Any) -> str:
    text = str(output)
    if len(text) <= _MCPORTER_ERROR_OUTPUT_MAX_CHARS:
        return text
    # 超出上限时只保留前 _MCPORTER_ERROR_OUTPUT_MAX_CHARS 个字符，
    # 并在末尾追加省略提示，告知调用方省略了多少字符
    omitted = len(text) - _MCPORTER_ERROR_OUTPUT_MAX_CHARS
    return text[:_MCPORTER_ERROR_OUTPUT_MAX_CHARS] + (
        f"... truncated, {omitted} more character(s) omitted"
    )


# 统一以 JSON 字符串形式返回工具结果给 LLM；
# json.dumps 本身是同步阻塞调用，这里通过 run_blocking_io 放到线程池执行，
# 避免在异步事件循环中直接执行同步序列化造成阻塞
async def _json_dumps_result(data: dict[str, Any]) -> str:
    return await run_blocking_io(json.dumps, data, ensure_ascii=False)


# ── MongoDB persistence helpers ───────────────────────────────


async def _persist_server_to_mongodb(
    user_id: str,
    server_name: str,
    command: str,
    env_keys: list[str],
) -> bool:
    """Create or update a sandbox MCP server in MongoDB."""
    # MCPStorage 封装了 MCP 服务器配置在 MongoDB 中的存取逻辑，
    # 这里延迟导入以避免模块加载时的循环依赖
    from src.infra.mcp.storage import MCPStorage
    from src.kernel.schemas.mcp import MCPServerCreate, MCPTransport

    storage = MCPStorage()
    # 先查询该用户名下是否已存在同名 server，据此决定走更新还是新建分支
    existing = await storage.get_user_server(server_name, user_id)
    if existing:
        # Update existing
        from src.kernel.schemas.mcp import MCPServerUpdate

        update = MCPServerUpdate(command=command, env_keys=env_keys if env_keys else None)
        result = await storage.update_user_server(server_name, update, user_id)
        if result:
            logger.info(f"[sandbox_mcp] Updated MongoDB server '{server_name}' for user {user_id}")
            return True
        return False
    else:
        # Create new
        create = MCPServerCreate(
            name=server_name,
            transport=MCPTransport.SANDBOX,
            command=command,
            env_keys=env_keys if env_keys else None,
        )
        server = await storage.create_user_server(create, user_id)
        if server:
            logger.info(f"[sandbox_mcp] Created MongoDB server '{server_name}' for user {user_id}")
            return True
        return False


async def _delete_server_from_mongodb(user_id: str, server_name: str) -> bool:
    """Delete a sandbox MCP server from MongoDB."""
    from src.infra.mcp.storage import MCPStorage

    storage = MCPStorage()
    # 按用户 + 服务器名删除数据库记录，返回值表示记录是否原本存在并被真正删除
    deleted = await storage.delete_user_server(server_name, user_id)
    if deleted:
        logger.info(f"[sandbox_mcp] Deleted MongoDB server '{server_name}' for user {user_id}")
    return deleted


# ── Tool implementations ───────────────────────────────────────


# 中文：使用 @tool 装饰器将下面的函数注册为 LangChain 工具，可被 LLM 直接调用；
# runtime 参数通过 InjectedToolArg 注入，不会出现在暴露给 LLM 的工具签名中，
# 用于在工具内部拿到当前请求对应的沙箱 backend 与用户身份。
@tool
async def sandbox_mcp_add(
    server_name: Annotated[str, "MCP server name to register"],
    command: Annotated[str, "stdio command, e.g. 'npx @anthropic/mcp-server-fetch'"],
    env_keys: Annotated[
        Optional[str],
        "Comma-separated list of environment variable KEY names to inject "
        "(must be pre-defined in user's environment variables settings)",
    ] = None,
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Register a new MCP server in the sandbox and persist it to the database.
    Provide server_name and the stdio command (e.g. 'npx @anthropic/mcp-server-fetch').
    Optionally pass env_keys as comma-separated KEY names to inject
    (these must be pre-defined in user's environment variable settings).
    The server will be automatically restored when the sandbox is rebuilt."""
    # 从 runtime 中取出当前请求绑定的沙箱执行 backend；
    # 若为空说明用户当前没有可用沙箱（例如尚未启动沙箱会话），无法继续
    backend = get_backend_from_runtime(runtime)
    if backend is None:
        return await _json_dumps_result({"error": "No sandbox backend available"})

    user_id = get_user_id_from_runtime(runtime) or "unknown"
    # 将逗号分隔的环境变量 KEY 列表解析为字符串列表，并过滤空白项
    env_key_list = [k.strip() for k in env_keys.split(",") if k.strip()] if env_keys else []

    try:
        # Register in sandbox
        # 根据 env_key_list 从用户环境变量配置中取值，拼接成
        # `--env KEY=VALUE` 形式的命令行参数（详见 sandbox_mcp_utils.build_env_flags）
        env_flags = await build_env_flags(user_id, env_key_list)
        cmd = f"mcporter config add {shlex.quote(server_name)} --stdio {shlex.quote(command)}{env_flags}"
        result = await backend.aexecute(cmd, timeout=_MCPORTER_TIMEOUT)
        if result.exit_code != 0:
            return await _json_dumps_result(
                {"error": f"mcporter failed: {_truncate_tool_output(result.output)}"}
            )

        # Persist to MongoDB
        # 沙箱内注册成功后，把配置写入数据库，便于沙箱重建/重启后自动恢复
        ok = await _persist_server_to_mongodb(user_id, server_name, command, env_key_list)
        if not ok:
            return await _json_dumps_result(
                {"error": "Server registered in sandbox but failed to persist to database"}
            )

        # 配置发生变化后需要让系统提示词中缓存的 MCP 服务器列表失效，
        # 并通过发布/订阅通知其它进程同样清空缓存
        invalidate_sandbox_mcp_prompt_cache(user_id)
        await publish_tool_cache_invalidation("sandbox_mcp_prompt", user_id=user_id)
    except Exception as e:
        # 兜底捕获所有异常，转换为工具错误结果返回，避免异常向上抛出打断整个 agent 执行
        return await _json_dumps_result({"error": f"Failed to add server: {e}"})
    return await _json_dumps_result(
        {
            "success": True,
            "message": f"Server '{server_name}' added to sandbox and saved",
            "server_name": server_name,
            "command": command,
            "env_keys": env_key_list,
        }
    )


# 中文：更新已注册的 MCP 服务器。由于 mcporter 没有直接的"更新"命令，
# 实现策略是先 remove 旧配置再 add 新配置；若新增失败会尝试用旧配置回滚。
@tool
async def sandbox_mcp_update(
    server_name: Annotated[str, "Name of the MCP server to update"],
    command: Annotated[Optional[str], "New stdio command (leave unchanged if omitted)"] = None,
    env_keys: Annotated[
        Optional[str],
        "Comma-separated list of environment variable KEY names to inject "
        "(leave unchanged if omitted)",
    ] = None,
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Update an existing sandbox MCP server's command or environment variables.
    Provide server_name and optionally the new command and/or env_keys.
    Changes are persisted to the database and applied to the sandbox."""
    # 复用相同逻辑获取沙箱 backend，为空说明当前没有可用沙箱
    backend = get_backend_from_runtime(runtime)
    if backend is None:
        return await _json_dumps_result({"error": "No sandbox backend available"})

    user_id = get_user_id_from_runtime(runtime) or "unknown"
    # 注意默认值是 None 而不是 []：None 表示"未传该参数、保持原值不变"，
    # 空列表则表示"显式清空环境变量"，二者语义不同，下面据此区分处理
    env_key_list = [k.strip() for k in env_keys.split(",") if k.strip()] if env_keys else None

    try:
        # We need to know the current command to rebuild mcporter config.
        # Read from MongoDB first.
        # 中文：mcporter 的 add 命令是全量覆盖式的，因此必须先从数据库读出
        # 当前完整配置，再与本次传入的新值合并，否则未传的字段会被清空丢失
        from src.infra.mcp.storage import MCPStorage

        storage = MCPStorage()
        existing = await storage.get_user_server(server_name, user_id)
        if not existing:
            return await _json_dumps_result(
                {"error": f"Server '{server_name}' not found in database"}
            )

        # 未显式传入的参数沿用数据库中的旧值，实现"部分更新"语义
        resolved_command = command or existing.command or ""
        resolved_env_keys = env_key_list if env_key_list is not None else (existing.env_keys or [])

        # Remove old config from mcporter, add new one
        # 中文：mcporter 没有真正的"更新"操作，只能先移除旧配置再添加新配置
        await backend.aexecute(
            f"mcporter config remove {shlex.quote(server_name)}", timeout=_MCPORTER_TIMEOUT
        )
        # remove may fail if server wasn't in mcporter yet, that's ok

        env_flags = await build_env_flags(user_id, resolved_env_keys)
        add_cmd = f"mcporter config add {shlex.quote(server_name)} --stdio {shlex.quote(resolved_command)}{env_flags}"
        result = await backend.aexecute(add_cmd, timeout=_MCPORTER_TIMEOUT)
        if result.exit_code != 0:
            # Try to restore the old one if possible
            # 中文：新配置添加失败时，尝试用旧的 command/env 重新添加回去，
            # 避免出现"旧配置已删除、新配置又没生效"的中间不一致状态
            old_env = await build_env_flags(user_id, existing.env_keys or [])
            restore_cmd = f"mcporter config add {shlex.quote(server_name)} --stdio {shlex.quote(existing.command or '')}{old_env}"
            await backend.aexecute(restore_cmd, timeout=_MCPORTER_TIMEOUT)
            return await _json_dumps_result(
                {"error": f"mcporter update failed: {_truncate_tool_output(result.output)}"}
            )

        # Persist to MongoDB
        # 中文：沙箱侧更新成功后，把合并后的最终配置写回数据库
        from src.kernel.schemas.mcp import MCPServerUpdate

        update = MCPServerUpdate(
            command=resolved_command,
            env_keys=resolved_env_keys,
        )
        updated = await storage.update_user_server(server_name, update, user_id)
        if not updated:
            return await _json_dumps_result(
                {"error": "mcporter updated but failed to persist to database"}
            )

        # 更新完成后同样需要失效提示词缓存并广播通知其它进程
        invalidate_sandbox_mcp_prompt_cache(user_id)
        await publish_tool_cache_invalidation("sandbox_mcp_prompt", user_id=user_id)
    except Exception as e:
        return await _json_dumps_result({"error": f"Failed to update server: {e}"})
    return await _json_dumps_result(
        {
            "success": True,
            "message": f"Server '{server_name}' updated in sandbox and saved",
            "server_name": server_name,
            "command": resolved_command,
            "env_keys": resolved_env_keys,
        }
    )


# 中文：从沙箱与数据库中移除指定 MCP 服务器；即便沙箱侧移除失败
# （例如原本就没在 mcporter 中注册过），只要数据库删除成功也视为整体成功，
# 因为核心目标"以后不再自动恢复该服务器"已经达成。
@tool
async def sandbox_mcp_remove(
    server_name: Annotated[str, "MCP server name to remove"],
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Remove an MCP server from the sandbox and delete it from the database.
    The server will no longer be restored when the sandbox is rebuilt."""
    backend = get_backend_from_runtime(runtime)
    if backend is None:
        return await _json_dumps_result({"error": "No sandbox backend available"})

    user_id = get_user_id_from_runtime(runtime) or "unknown"

    try:
        # Unregister from mcporter
        cmd = f"mcporter config remove {shlex.quote(server_name)}"
        result = await backend.aexecute(cmd, timeout=_MCPORTER_TIMEOUT)

        # Persist removal to MongoDB (even if mcporter remove failed, e.g. server wasn't registered)
        deleted = await _delete_server_from_mongodb(user_id, server_name)

        # 中文：即使 mcporter 侧移除失败，只要数据库记录确实被删除，
        # 也视为达成了"以后不再恢复该服务器"的目的，返回成功
        if result.exit_code != 0 and deleted:
            invalidate_sandbox_mcp_prompt_cache(user_id)
            await publish_tool_cache_invalidation("sandbox_mcp_prompt", user_id=user_id)
            return await _json_dumps_result(
                {
                    "success": True,
                    "message": f"Server '{server_name}' removed from database (was not in sandbox)",
                }
            )

        # 中文：mcporter 移除失败且数据库中也没有对应记录，说明服务器本就不存在，
        # 直接把 mcporter 的报错信息回传给 LLM
        if result.exit_code != 0:
            return await _json_dumps_result(
                {"error": f"mcporter failed: {_truncate_tool_output(result.output)}"}
            )

        invalidate_sandbox_mcp_prompt_cache(user_id)
        await publish_tool_cache_invalidation("sandbox_mcp_prompt", user_id=user_id)
    except Exception as e:
        return await _json_dumps_result({"error": f"Failed to remove server: {e}"})
    return await _json_dumps_result(
        {
            "success": True,
            "message": f"Server '{server_name}' removed from sandbox and database",
            "server_name": server_name,
        }
    )


# ── Public API ─────────────────────────────────────────────────


def get_sandbox_mcp_tools() -> list[BaseTool]:
    """Get all sandbox MCP management tools.

    Returns three independent LangChain tools so the LLM can
    manage MCP servers:
      - sandbox_mcp_add:      register a new server (persists to MongoDB)
      - sandbox_mcp_update:   update server command/env_keys (persists to MongoDB)
      - sandbox_mcp_remove:   unregister a server (persists to MongoDB)
    """
    # 中文：供 agent 构建工具集时调用，一次性返回三个独立工具供 LLM 按需选择
    return [sandbox_mcp_add, sandbox_mcp_update, sandbox_mcp_remove]


# Backwards compatibility alias
def get_sandbox_mcp_tool() -> BaseTool:
    """Get a single sandbox MCP management tool (deprecated, use get_sandbox_mcp_tools)."""
    # 中文：历史遗留接口，仅返回列表中的第一个工具（sandbox_mcp_add），保留以兼容旧调用方
    return get_sandbox_mcp_tools()[0]

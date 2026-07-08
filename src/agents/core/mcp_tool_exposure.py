"""Helpers for deciding whether MCP tools are inline or deferred."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 一个 MCP server 往往暴露几十上百个工具，如果全部原样注册进 Agent 的工具
# 列表，会让每次模型调用都携带巨量的工具 schema，白白消耗 prompt 预算。
# 因此默认策略是"延迟加载"：MCP 工具默认归为 deferred（不直接出现在工具
# 列表里，需要 Agent 主动调用 search_tools 搜索发现后才动态注入，具体
# 执行逻辑见 src/infra/agent/middleware/tool_interception.py 的
# ToolSearchMiddleware）。只有被管理员/配置显式标记为 inline_exposure=True
# 的工具（通常是高频、核心的少数工具）才会跳过这层延迟机制，直接出现在
# 工具列表里。本文件就是做这道"inline 还是 deferred"的分流判断。
# ============================================================================

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from src.kernel.schemas.mcp import MCPToolPolicy

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


def _server_for_tool(tool: "BaseTool") -> str:
    # 优先用工具对象自带的 server 属性；没有的话，MCP 工具名约定是
    # "server:tool_name" 这种带命名空间的形式，从名字里切出冒号前的部分
    server = getattr(tool, "server", "")
    if isinstance(server, str) and server:
        return server
    name = getattr(tool, "name", "")
    return name.split(":", 1)[0] if ":" in name else ""


def _raw_name_for_tool(tool: "BaseTool", server_name: str) -> str:
    # 从"server:tool_name"形式的完整名字里剥掉 "server:" 前缀，还原出
    # 该工具在 policy 配置里对应的原始名字；前缀不匹配时原样返回，不做强制假设
    name = getattr(tool, "name", "")
    if server_name and name.startswith(f"{server_name}:"):
        return name[len(server_name) + 1 :]
    return name


def split_mcp_tools_for_exposure(
    tools: list["BaseTool"],
    policies_by_server: Mapping[str, Mapping[str, MCPToolPolicy]],
) -> tuple[list["BaseTool"], list["BaseTool"]]:
    """Split MCP tools into directly exposed tools and deferred tools."""
    inline_tools: list["BaseTool"] = []
    deferred_tools: list["BaseTool"] = []

    for tool in tools:
        server_name = _server_for_tool(tool)
        raw_name = _raw_name_for_tool(tool, server_name)
        # 按 server -> tool 名两级查找该工具的策略配置
        policy = policies_by_server.get(server_name, {}).get(raw_name)
        # 只有查到策略、且策略显式要求 inline_exposure 时才归入直接暴露一档；
        # 没查到策略（未配置）或策略未开启 inline_exposure，都默认归入延迟加载
        if policy and policy.inline_exposure:
            inline_tools.append(tool)
        else:
            deferred_tools.append(tool)

    return inline_tools, deferred_tools

"""
search_tools 工具 — LangChain BaseTool，供 LLM 搜索和加载延迟的 MCP 工具。

LLM 调用此工具时：
1. 使用关键词搜索引擎匹配延迟工具
2. 将匹配工具提升为"已发现"状态
3. 返回完整 schema 供 LLM 后续调用
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.tool.tool_search import ToolSearchResult, search_tools_with_keywords

if TYPE_CHECKING:
    from src.infra.tool.deferred_manager import DeferredToolManager

logger = get_logger(__name__)

# 返回的参数 schema 压缩阈值：数组最多保留 200 项、字符串最多 2000 字符，
# 超出则截断并注明省略数量，避免庞大 schema 撑爆 LLM 上下文
TOOL_SEARCH_SCHEMA_MAX_ARRAY_ITEMS = 200
TOOL_SEARCH_SCHEMA_MAX_STRING_CHARS = 2000


class ToolSearchInput(BaseModel):
    """search_tools 的输入 schema"""

    query: str = Field(
        ...,
        description=(
            "Query to find deferred tools by name or capability. "
            "Use exact tool names as shown in the deferred MCP list, for example "
            '"select:github:create_issue". '
            'Use keywords like "database query" for best-match search. '
            'Prefix a term with + to require it in the tool name (e.g., "+slack send").'
        ),
    )


class ToolSearchTool(BaseTool):
    """搜索并加载延迟的 MCP 工具。

    当 LLM 需要一个不在当前工具列表中的工具时，调用此工具来搜索和加载。
    搜索成功后，匹配的工具会立即可用于后续调用。
    """

    name: str = "search_tools"
    # description 直接面向 LLM，详细说明 search_tools 的适用范围与查询语法
    description: str = (
        "Fetches full schema definitions for deferred tools so they can be called. "
        'Deferred tools appear by name in the "Available MCP Tools (Deferred)" section below. '
        "This only applies to deferred MCP tools exposed through the main tool registry; "
        "it does NOT search sandbox tools managed by `mcporter`. "
        "Sandbox tools are NOT MCP tools — use the `execute` tool with `mcporter` commands to invoke them. "
        "Until fetched, only the name is known — there is no parameter schema, so the tool cannot be invoked. "
        "This tool takes a query, matches it against the deferred tool list, and returns "
        "the matched tools' complete parameter schemas. Once a tool's schema is returned, "
        "it is callable exactly like any other tool in your tool list. "
        "Use exact tool names as shown in the deferred MCP list (format: `server:tool`).\n\n"
        "Query forms:\n"
        '- "select:github:create_issue" — fetch this exact tool by name\n'
        '- "database query" — keyword search, best matches returned\n'
        '- "+slack send" — require "slack" in the name, rank by remaining terms'
    )
    args_schema: type[BaseModel] = ToolSearchInput

    # 注入的依赖（非 Pydantic 字段）
    # _manager：延迟工具管理器，负责实际的发现/提升；_search_limit：单次最多返回数
    _manager: Optional["DeferredToolManager"] = None
    _search_limit: int = 25

    class Config:
        # 允许非 Pydantic 类型（DeferredToolManager）作为私有属性存在
        arbitrary_types_allowed = True

    def __init__(
        self,
        manager: "DeferredToolManager",
        search_limit: int = 25,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._manager = manager
        self._search_limit = search_limit

    def _run(self, query: str) -> str:
        # 本工具仅支持异步执行；同步入口直接报错，避免被误用
        raise NotImplementedError("Use async _arun")

    async def _arun(
        self,
        query: str,
        config: Optional[RunnableConfig] = None,
        run_manager: Optional[Any] = None,
    ) -> str:
        # 未注入 manager 时视为配置错误，返回可读错误串（不抛异常）
        if not self._manager:
            return "Error: search_tools is not configured properly."

        # 分别取已发现/未发现工具：两者都参与搜索，但未发现工具优先
        discovered = self._manager.get_discovered_tools()
        undiscovered = self._manager.get_undiscovered_tools()
        all_tools = discovered + undiscovered
        if not all_tools:
            return "No deferred tools are available for search."

        # 搜索与格式化是 CPU 密集的纯计算，放到线程池执行避免阻塞事件循环
        results, parts = await run_blocking_io(
            _search_and_format_tool_results,
            query,
            discovered,
            undiscovered,
            self._search_limit,
        )

        if not results:
            return (
                f"No tools found matching '{query}'. "
                f"Try different keywords or check the available tool list."
            )

        # 提升匹配的工具
        # 把命中的工具从延迟状态提升为"已发现"，使其在后续对话中可直接调用
        matched_names = [r.name for r in results]
        newly_discovered = self._manager.discover_tools(matched_names)
        newly_discovered_names = {tool.name for tool in newly_discovered}
        # 已发现集合与本次结果之差即为"此前已可用"的数量
        already_available_count = len(results) - len(newly_discovered)

        # 组装状态说明：区分"新加载"与"此前已可用"，便于 LLM 理解结果
        status = ""
        if newly_discovered and already_available_count:
            status = (
                f" ({len(newly_discovered)} newly loaded, "
                f"{already_available_count} already available)"
            )
        elif newly_discovered:
            status = f" ({len(newly_discovered)} tools loaded)"
        elif already_available_count:
            status = f" ({already_available_count} already available)"

        header = (
            f"Found {len(results)} tool(s){status}. These tools are now available for use. "
            "If the tool you need appears below, call it directly next.\n\n"
        )
        # 回填每个结果的状态占位符（新加载/已可用）——占位符在格式化阶段预留
        formatted_parts = [
            part.replace(
                "__TOOL_STATUS__",
                "newly loaded" if result.name in newly_discovered_names else "already available",
            )
            for result, part in zip(results, parts)
        ]
        return header + "\n\n---\n\n".join(formatted_parts)


def _search_and_format_tool_results(
    query: str,
    discovered: list[BaseTool],
    undiscovered: list[BaseTool],
    search_limit: int,
) -> tuple[list[ToolSearchResult], list[str]]:
    # 优先返回未加载工具，避免较小的 result limit 被已可用工具占满。
    # 先在未发现工具中搜索并占用名额
    undiscovered_results = search_tools_with_keywords(
        query=query,
        tools=undiscovered,
        max_results=search_limit,
    )
    # 剩余名额再用于已发现工具，保证新工具的曝光优先级
    remaining_slots = max(search_limit - len(undiscovered_results), 0)
    discovered_results = (
        search_tools_with_keywords(
            query=query,
            tools=discovered,
            max_results=remaining_slots,
        )
        if remaining_slots > 0
        else []
    )
    results = undiscovered_results + discovered_results
    return results, [_format_tool_result(result) for result in results]


def _format_tool_result(result: ToolSearchResult) -> str:
    # 把单个搜索结果格式化为带完整参数 schema 的 Markdown 片段
    tool = result.tool
    schema: dict[str, Any] = {}
    args_schema = getattr(tool, "args_schema", None)
    if args_schema is not None:
        # 生成 JSON Schema 失败时降级为空 schema，不影响其余结果
        try:
            schema = args_schema.model_json_schema()
        except Exception:
            pass

    # 对 properties / required 做体积压缩，防止超大 schema 撑爆上下文
    props = _compact_schema_value(schema.get("properties", {}))
    required = _compact_schema_value(schema.get("required", []))

    schema_str = json.dumps(
        {"properties": props, "required": required},
        ensure_ascii=False,
        indent=2,
    )

    # __TOOL_STATUS__ 为占位符，由 _arun 根据是否新发现回填
    return (
        f"## {result.name} (score: {result.score:.1f})\n"
        "Status: __TOOL_STATUS__\n"
        f"Description: {result.description[:300]}\n"
        f"Parameters:\n```json\n{schema_str}\n```"
    )


def _compact_schema_value(value: Any) -> Any:
    # 递归压缩 schema：对 dict 逐键递归；对超长数组/字符串截断并注明省略量
    if isinstance(value, dict):
        return {key: _compact_schema_value(child) for key, child in value.items()}
    if isinstance(value, list):
        # 数组未超限则整体递归压缩
        if len(value) <= TOOL_SEARCH_SCHEMA_MAX_ARRAY_ITEMS:
            return [_compact_schema_value(child) for child in value]
        # 超限：仅保留前 N 项并追加一条省略说明
        omitted = len(value) - TOOL_SEARCH_SCHEMA_MAX_ARRAY_ITEMS
        compacted = [
            _compact_schema_value(child) for child in value[:TOOL_SEARCH_SCHEMA_MAX_ARRAY_ITEMS]
        ]
        compacted.append(f"... schema truncated, {omitted} more item(s) omitted")
        return compacted
    # 超长字符串：截断并注明省略字符数
    if isinstance(value, str) and len(value) > TOOL_SEARCH_SCHEMA_MAX_STRING_CHARS:
        omitted = len(value) - TOOL_SEARCH_SCHEMA_MAX_STRING_CHARS
        return (
            value[:TOOL_SEARCH_SCHEMA_MAX_STRING_CHARS]
            + f"... schema truncated, {omitted} more character(s) omitted"
        )
    return value

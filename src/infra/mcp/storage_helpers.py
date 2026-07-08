"""Shared helpers and limits for MCP storage."""

from typing import Any

# Sensitive fields that should be masked in responses
# 返回给前端时需要脱敏（打码）的敏感字段：请求头中的鉴权/密钥类字段
# 采用 "headers.字段名" 的点路径写法，供响应组装时按路径遮蔽
SENSITIVE_FIELDS = [
    "headers.Authorization",
    "headers.X-Api-Key",
    "headers.Api-Key",
]

# Patterns for sensitive env variables
# 环境变量名匹配以下后缀模式时视为敏感（如 XXX_API_KEY / XXX_TOKEN），需脱敏
SENSITIVE_ENV_PATTERNS = ["_API_KEY", "_SECRET", "_PASSWORD", "_TOKEN"]
# 各类列表查询的上限，防止单次读取过多文档拖垮内存/响应
MCP_SERVER_LIST_LIMIT = 500
MCP_PREFERENCE_LIST_LIMIT = 1000
MCP_TOOL_POLICY_LIST_LIMIT = 1000
MCP_DISCOVER_TOOL_LIMIT = 100
MCP_DISCOVER_TOOL_PARAMETER_LIMIT = 100
# 单个服务器“禁用工具”列表的数量上限
MCP_DISABLED_TOOLS_LIMIT = 100


# 规范化“禁用工具名”列表：去重、去空、限制数量上限
# include：可选，强制保证某个工具名一定出现在结果中（用于本次要新增禁用的工具）
# 返回一个干净的字符串列表，最长不超过 MCP_DISABLED_TOOLS_LIMIT
def _normalize_disabled_tools(values: Any, *, include: str | None = None) -> list[str]:
    # 非列表/元组/集合一律视为空
    if not isinstance(values, (list, tuple, set)):
        return []
    normalized: list[str] = []
    # seen 用于去重；include_seen 记录 include 是否在原始数据中出现过
    seen: set[str] = set()
    include_seen = False
    for value in values:
        # 跳过非字符串、空串以及重复项
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        if include is not None and value == include:
            include_seen = True
        # 达到上限后不再追加，但仍继续循环以正确标记 include_seen
        if len(normalized) < MCP_DISABLED_TOOLS_LIMIT:
            normalized.append(value)
    # 若 include 存在于原始数据但因上限被挤出，则替换末位以保证其保留
    if include is not None and include_seen and include not in normalized:
        if len(normalized) >= MCP_DISABLED_TOOLS_LIMIT:
            normalized[-1] = include
        else:
            normalized.append(include)
    return normalized


# 对某工具的禁用状态做增量更新：disabled=True 表示加入禁用列表，False 表示移出
# 先规范化现有列表，再根据目标状态增删，超过上限时抛错拒绝
def _apply_disabled_tool_update(values: Any, tool_name: str, disabled: bool) -> list[str]:
    disabled_tools = _normalize_disabled_tools(
        values,
        include=tool_name if disabled else None,
    )
    if disabled:
        # 加入禁用：不存在才追加，超上限抛错
        if tool_name not in disabled_tools:
            if len(disabled_tools) >= MCP_DISABLED_TOOLS_LIMIT:
                raise ValueError(
                    f"Too many disabled tools: maximum {MCP_DISABLED_TOOLS_LIMIT} allowed."
                )
            disabled_tools.append(tool_name)
    elif tool_name in disabled_tools:
        # 解除禁用：存在才移除
        disabled_tools.remove(tool_name)
    return disabled_tools

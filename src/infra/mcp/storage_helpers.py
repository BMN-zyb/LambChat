"""Shared helpers and limits for MCP storage."""

from typing import Any

# Sensitive fields that should be masked in responses
SENSITIVE_FIELDS = [
    "headers.Authorization",
    "headers.X-Api-Key",
    "headers.Api-Key",
]

# Patterns for sensitive env variables
SENSITIVE_ENV_PATTERNS = ["_API_KEY", "_SECRET", "_PASSWORD", "_TOKEN"]
MCP_SERVER_LIST_LIMIT = 500
MCP_PREFERENCE_LIST_LIMIT = 1000
MCP_TOOL_POLICY_LIST_LIMIT = 1000
MCP_DISCOVER_TOOL_LIMIT = 100
MCP_DISCOVER_TOOL_PARAMETER_LIMIT = 100
MCP_DISABLED_TOOLS_LIMIT = 100


def _normalize_disabled_tools(values: Any, *, include: str | None = None) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    include_seen = False
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        if include is not None and value == include:
            include_seen = True
        if len(normalized) < MCP_DISABLED_TOOLS_LIMIT:
            normalized.append(value)
    if include is not None and include_seen and include not in normalized:
        if len(normalized) >= MCP_DISABLED_TOOLS_LIMIT:
            normalized[-1] = include
        else:
            normalized.append(include)
    return normalized


def _apply_disabled_tool_update(values: Any, tool_name: str, disabled: bool) -> list[str]:
    disabled_tools = _normalize_disabled_tools(
        values,
        include=tool_name if disabled else None,
    )
    if disabled:
        if tool_name not in disabled_tools:
            if len(disabled_tools) >= MCP_DISABLED_TOOLS_LIMIT:
                raise ValueError(
                    f"Too many disabled tools: maximum {MCP_DISABLED_TOOLS_LIMIT} allowed."
                )
            disabled_tools.append(tool_name)
    elif tool_name in disabled_tools:
        disabled_tools.remove(tool_name)
    return disabled_tools

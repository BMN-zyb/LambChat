"""Shared types and constants for LangChain agent stream events."""

from typing import Any, TypeAlias

from langchain_core.runnables.schema import CustomStreamEvent, StandardStreamEvent

# StreamEvent 统一别名：astream_events 既可能产出标准事件，也可能产出自定义事件
StreamEvent: TypeAlias = StandardStreamEvent | CustomStreamEvent

# 内置 task 工具名（deepagents 用它派发子 agent），事件路由据此识别子 agent 调用
TOOL_TASK = "task"

# 工具输出中若包含以下任一关键字（小写匹配），则判定该工具执行失败
TOOL_ERROR_INDICATORS = frozenset(
    (
        "error:",
        "validationerror",
        "[mcp tool error]",
        "failed",
        "command failed",
        "exception",
        "traceback",
    )
)


def get_value(obj: Any, key: str, default: Any = 0) -> Any:
    """Read a value from either a dict-like object or an attribute object."""
    # 兼容两种数据形态：dict 用 .get 取键，普通对象用 getattr 取属性
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

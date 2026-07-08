"""Tracing decorators."""

import os
from typing import Any, Callable, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def traced(name: str = "", run_type: str = "chain") -> Callable[[F], F]:
    """
    Decorator for tracing functions with LangSmith.

    Usage:
        @traced(name="my_function", run_type="tool")
        async def my_function(arg: str) -> str:
            return result

    Args:
        name: Name for the trace (default: function name)
        run_type: Type of run (chain, tool, llm, etc.)
    """

    def decorator(func: F) -> F:
        # If tracing is disabled, return original function
        # 追踪未开启时零开销:原样返回被装饰函数,完全不引入 langsmith 依赖或包装。
        if os.getenv("LANGSMITH_TRACING", "false").lower() != "true":
            return func

        # Import here to avoid circular imports
        # 局部导入,避免模块级循环依赖,也让未开启追踪时无需导入 langsmith。
        from langsmith import traceable

        # Apply langsmith traceable decorator with keyword-only run_type
        # 套用 langsmith 的 traceable:name 缺省时用函数名作为 trace 名称。
        decorated = traceable(name=name or func.__name__, run_type=run_type)  # type: ignore[call-overload]
        return cast(F, decorated(func))

    return decorator

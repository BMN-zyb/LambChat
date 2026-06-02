"""
Trace Filter - 日志过滤器

自动将追踪上下文注入到日志记录中。
"""

from __future__ import annotations

import logging

from src.infra.logging.context import TraceContext


def _build_trace_context(parts: dict[str, str]) -> str:
    rendered = [f"{key}={value}" for key, value in parts.items() if value and value != "-"]
    return " ".join(rendered) + " " if rendered else ""


class TraceFilter(logging.Filter):
    """
    追踪日志过滤器

    自动从 TraceContext 获取追踪信息并注入到 LogRecord 中。

    注入的属性:
        - record.request_id: 请求 ID
        - record.trace_id: 追踪 ID
        - record.span_id: 跨度 ID
        - record.parent_span_id: 父跨度 ID
        - record.session_id: 会话 ID
        - record.run_id: 运行 ID
        - record.user_id: 用户 ID
        - record.trace_info: 格式化的追踪信息字符串

    Usage:
        handler = logging.StreamHandler()
        handler.addFilter(TraceFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """
        注入追踪上下文到日志记录

        Args:
            record: 日志记录对象

        Returns:
            总是返回 True（允许所有记录通过）
        """
        info = TraceContext.get()
        request_context = TraceContext.get_request_context()

        # 注入追踪属性
        record.request_id = info.request_id or request_context.request_id or "-"
        record.trace_id = info.trace_id or request_context.trace_id or "-"
        record.span_id = info.span_id or "-"
        record.parent_span_id = info.parent_span_id or "-"
        record.session_id = request_context.session_id or "-"
        record.run_id = request_context.run_id or "-"
        record.user_id = request_context.user_id or "-"
        record.trace_info = info.format()
        record.trace_context = _build_trace_context(
            {
                "request_id": record.request_id,
                "trace_id": record.trace_id,
                "span_id": record.span_id,
                "user_id": record.user_id,
                "session_id": record.session_id,
                "run_id": record.run_id,
            }
        )

        return True

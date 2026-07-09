"""
Trace Filter - 日志过滤器

自动将追踪上下文注入到日志记录中。
"""

# ---------------------------------------------------------------------------
# 模块说明：追踪信息日志过滤器
#
# 本模块提供 TraceFilter（logging.Filter 子类），是「trace 自动注入日志」的
# 消费端，与 context.py 配套：context.py 负责往 contextvars 写追踪信息，
# 本过滤器在每条日志产生时把它们读出来、挂到 LogRecord 上，之后日志格式串
# 就能用 %(trace_id)s / %(trace_context)s 等占位符直接输出。
#
# 关键点（也是难点）：
#   - Filter 挂到 handler 上后，会对流经该 handler 的每一条日志各调用一次；
#   - filter() 名为「过滤」，这里其实借用它做「副作用式属性注入」，并始终返回
#     True（不真正丢弃任何日志）；
#   - 所有注入字段都用 or "-" 兜底，保证格式串引用这些属性时永远有值，
#     不会因为某次日志缺少 trace 上下文而抛 KeyError。
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
from typing import Protocol, cast

from src.infra.logging.context import TraceContext


class _TraceLogRecord(Protocol):
    # 结构化协议:仅用于类型标注,声明经本过滤器注入后 LogRecord 上会具备的追踪相关属性。
    request_id: str
    trace_id: str
    span_id: str
    parent_span_id: str
    user_id: str
    session_id: str
    run_id: str
    trace_info: str
    trace_context: str


def _build_trace_context(parts: dict[str, str]) -> str:
    # 把非空(且不为占位符 "-")的键值渲染成 "k=v k=v " 形式;全空则返回空串。
    # 末尾保留一个空格,便于在日志格式里直接拼接而无需额外分隔。
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
        trace_record = cast(_TraceLogRecord, record)

        # 注入追踪属性
        # 每个字段都用 or 兜底到 "-",保证日志格式串引用这些属性时永远有值,不会因缺失而抛错;
        # request_id/trace_id 优先取追踪上下文,缺失时回退到业务请求上下文。
        trace_record.request_id = info.request_id or request_context.request_id or "-"
        trace_record.trace_id = info.trace_id or request_context.trace_id or "-"
        trace_record.span_id = info.span_id or "-"
        trace_record.parent_span_id = info.parent_span_id or "-"
        trace_record.session_id = request_context.session_id or "-"
        trace_record.run_id = request_context.run_id or "-"
        trace_record.user_id = request_context.user_id or "-"
        trace_record.trace_info = info.format()
        trace_record.trace_context = _build_trace_context(
            {
                "request_id": trace_record.request_id,
                "trace_id": trace_record.trace_id,
                "span_id": trace_record.span_id,
                "user_id": trace_record.user_id,
                "session_id": trace_record.session_id,
                "run_id": trace_record.run_id,
            }
        )

        return True

"""
分布式追踪日志系统

提供自动注入追踪上下文的日志功能。

Usage:
    from src.infra.logging import TraceContext, setup_logging

    # 在请求入口设置追踪上下文
    TraceContext.set(trace_id="abc123", span_id="def456")

    # 正常使用 logger，trace_id 自动注入
    import logging
    logger = logging.getLogger(__name__)
    logger.info("processing request")  # 自动带上 trace_id

    # 清除追踪上下文
    TraceContext.clear()
"""

# 从各子模块聚合导出日志系统的公共接口:
# config(get_logger/日志级别解析/初始化)、context(追踪上下文)、filter(把上下文注入日志记录)。
from src.infra.logging.config import get_logger, parse_log_levels, setup_logging
from src.infra.logging.context import TraceContext, TraceInfo
from src.infra.logging.filter import TraceFilter

# __all__ 显式声明本包对外暴露的符号,约束 `from src.infra.logging import *` 的范围。
__all__ = [
    "TraceContext",
    "TraceInfo",
    "TraceFilter",
    "setup_logging",
    "parse_log_levels",
    "get_logger",
]

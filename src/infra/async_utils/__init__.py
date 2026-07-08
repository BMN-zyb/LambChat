"""Utilities for keeping async runtime paths non-blocking."""

# 对外导出阻塞 IO 卸载工具:run_blocking_io(把同步 IO 挪出事件循环)与关闭线程池的收尾函数。
from .blocking import run_blocking_io, shutdown_blocking_io_executor

__all__ = ["run_blocking_io", "shutdown_blocking_io_executor"]

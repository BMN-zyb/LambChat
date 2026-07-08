# src/infra/task/exceptions.py
"""
Background Task Manager - Exceptions
"""


# TaskInterruptedError 用于「用户主动取消」这条中断路径：agent 在执行过程中
# 调用 check_interrupt 检测到中断信号时抛出，executor 捕获后把任务落为
# CANCELLED 终态（而非 FAILED，也不会触发自动恢复）。它与 asyncio 的
# CancelledError 区分开：前者是业务层优雅中断，后者是 asyncio 层强制取消。
class TaskInterruptedError(Exception):
    """任务被中断异常"""

    pass

# src/infra/task/__init__.py
"""Background Task Manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.infra.task.constants import (
    CANCEL_CHANNEL,
    HEARTBEAT_PREFIX,
    HEARTBEAT_TIMEOUT,
    INTERRUPT_PREFIX,
)
from src.infra.task.exceptions import TaskInterruptedError

if TYPE_CHECKING:
    from src.infra.task.cancellation import TaskCancellation
    from src.infra.task.executor import TaskExecutor
    from src.infra.task.heartbeat import TaskHeartbeat
    from src.infra.task.manager import BackgroundTaskManager
    from src.infra.task.pubsub import TaskPubSub
    from src.infra.task.status import TaskStatus

# 对外导出的符号清单。为保持向后兼容，历史上 `from src.infra.task import X`
# 的用法都要能继续工作，因此这里把散落在各子模块里的核心类重新导出。
__all__ = [
    # Main exports (backward compatibility)
    "BackgroundTaskManager",
    "TaskStatus",
    "get_task_manager",
    # Additional exports for advanced usage
    "TaskInterruptedError",
    "TaskCancellation",
    "TaskExecutor",
    "TaskHeartbeat",
    "TaskPubSub",
    "CANCEL_CHANNEL",
    "HEARTBEAT_PREFIX",
    "INTERRUPT_PREFIX",
    "HEARTBEAT_TIMEOUT",
]


# 模块级 __getattr__（PEP 562）：把 BackgroundTaskManager / TaskExecutor 等重型
# 类做成「按需惰性导入」。这样 `import src.infra.task` 本身很轻，只有真正访问
# 到某个属性时才会 import 对应子模块，既避免了循环 import，又降低启动开销。
def __getattr__(name: str):
    if name == "BackgroundTaskManager" or name == "get_task_manager":
        from src.infra.task.manager import BackgroundTaskManager, get_task_manager

        return BackgroundTaskManager if name == "BackgroundTaskManager" else get_task_manager
    if name == "TaskStatus":
        from src.infra.task.status import TaskStatus

        return TaskStatus
    if name == "TaskCancellation":
        from src.infra.task.cancellation import TaskCancellation

        return TaskCancellation
    if name == "TaskExecutor":
        from src.infra.task.executor import TaskExecutor

        return TaskExecutor
    if name == "TaskHeartbeat":
        from src.infra.task.heartbeat import TaskHeartbeat

        return TaskHeartbeat
    if name == "TaskPubSub":
        from src.infra.task.pubsub import TaskPubSub

        return TaskPubSub
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

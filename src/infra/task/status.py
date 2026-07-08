# src/infra/task/status.py
"""
Background Task Manager - Task Status Enum
"""

from enum import Enum


# 任务状态枚举，是整个任务生命周期状态机（见 state_machine.py）的取值集合。
# 继承 str 便于直接序列化到 MongoDB session.metadata 的 task_status 字段，
# 以及在 SSE/前端之间传递。典型流转：
#   QUEUED/PENDING -> STARTING -> RUNNING -> COMPLETED
# 异常/中断分支：-> CANCELLING/CANCELLED、-> FAILED；崩溃后可 -> RECOVERING。
class TaskStatus(str, Enum):
    """任务状态"""

    # 已进入并发等待队列（arq 分发或超出用户并发上限时的排队态）
    QUEUED = "queued"
    # 已提交、等待启动（本地分发路径提交后的初始态）
    PENDING = "pending"
    # 正在启动：创建 Presenter、写入 user 消息、准备 trace 等
    STARTING = "starting"
    # agent 正在实际执行、持续产出事件
    RUNNING = "running"
    # 收到取消请求、正在优雅收尾（尚未落终态）
    CANCELLING = "cancelling"
    # 已取消（终态，用户主动取消，不可自动恢复）
    CANCELLED = "cancelled"
    # 正常完成（终态）
    COMPLETED = "completed"
    # 执行失败（终态；若标记为 recoverable 可被启动恢复流程接管）
    FAILED = "failed"
    # 恢复中：崩溃/重启后正在为旧 run 拉起新一轮
    RECOVERING = "recovering"
    # 已过期（终态，如排队期间服务器重启导致任务被放弃）
    EXPIRED = "expired"

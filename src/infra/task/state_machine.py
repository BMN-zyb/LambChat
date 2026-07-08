"""任务生命周期状态机。

集中定义「哪些状态可以流转到哪些状态」以及「哪些是终态」，并负责构造要写入
session.metadata 的状态元数据。把这套规则收拢到一处，可避免各调用点各自
拼装 metadata 或做出非法的状态跳变（例如把已 COMPLETED 的任务改回 RUNNING）。
"""

from __future__ import annotations

from typing import Any

from .status import TaskStatus


# 试图进行一次不被允许的生命周期跳变时抛出（继承 ValueError 便于上层捕获）。
class InvalidTaskTransitionError(ValueError):
    """Raised when a task attempts to move through an invalid lifecycle edge."""


class TaskStateMachine:
    """Validate task lifecycle transitions and build persistent status metadata."""

    # 终态集合：进入这些状态后任务不应再被普通流程改写（FAILED/EXPIRED 仍允许
    # 进入 RECOVERING 以支持崩溃恢复，见下方转移表）。
    _terminal_statuses = {
        TaskStatus.CANCELLED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.EXPIRED,
    }

    # 合法转移表：key 为「当前状态」（None 表示尚无状态的全新任务），value 为
    # 「允许迁入的目标状态集合」。设计要点：
    #   - None 只能进入排队/待启动/启动中，保证任务从头开始；
    #   - RUNNING 可进入 RECOVERING，用于运行中实例失联后被其他实例接管；
    #   - CANCELLED/COMPLETED 是干净终态（空集，不允许再流转）；
    #   - FAILED/EXPIRED 允许回到 RECOVERING，这是自动恢复的入口。
    _allowed_transitions: dict[TaskStatus | None, set[TaskStatus]] = {
        None: {TaskStatus.QUEUED, TaskStatus.PENDING, TaskStatus.STARTING},
        TaskStatus.QUEUED: {
            TaskStatus.PENDING,
            TaskStatus.STARTING,
            TaskStatus.RUNNING,
            TaskStatus.CANCELLING,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
            TaskStatus.FAILED,
            TaskStatus.RECOVERING,
        },
        TaskStatus.PENDING: {
            TaskStatus.QUEUED,
            TaskStatus.STARTING,
            TaskStatus.RUNNING,
            TaskStatus.CANCELLING,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
            TaskStatus.RECOVERING,
        },
        TaskStatus.STARTING: {
            TaskStatus.RUNNING,
            TaskStatus.CANCELLING,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        },
        TaskStatus.RUNNING: {
            TaskStatus.CANCELLING,
            TaskStatus.CANCELLED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.RECOVERING,
        },
        TaskStatus.CANCELLING: {
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        },
        TaskStatus.RECOVERING: {
            TaskStatus.QUEUED,
            TaskStatus.PENDING,
            TaskStatus.STARTING,
            TaskStatus.RUNNING,
            TaskStatus.FAILED,
        },
        TaskStatus.CANCELLED: set(),
        TaskStatus.COMPLETED: set(),
        TaskStatus.FAILED: {TaskStatus.RECOVERING},
        TaskStatus.EXPIRED: {TaskStatus.RECOVERING},
    }

    # 校验一次状态跳变是否合法：非法则抛 InvalidTaskTransitionError。
    # 注意允许 current == target 的「原地不变」（幂等写入），不视为非法。
    def validate_transition(
        self,
        current: TaskStatus | str | None,
        target: TaskStatus | str,
    ) -> None:
        current_status = self._coerce_status(current)
        target_status = self._coerce_status(target)
        allowed = self._allowed_transitions.get(current_status, set())
        if target_status not in allowed and current_status != target_status:
            raise InvalidTaskTransitionError(
                f"Invalid task transition: {current_status!s} -> {target_status!s}"
            )

    # 判断某状态是否为终态。
    def is_terminal(self, status: TaskStatus | str) -> bool:
        return self._coerce_status(status) in self._terminal_statuses

    # 构造要写入 session.metadata 的任务状态元数据字典。
    # 除了 task_status/task_error/task_error_code，还会：
    #   - 带上 current_run_id 用于多轮隔离与「陈旧写入」判定；
    #   - 推导 task_recoverable（可恢复标记）：显式传入优先，否则对一批
    #     「非自动恢复」的状态默认置 False；
    #   - 为 CANCELLED/EXPIRED 补默认 error_code，方便前端/恢复逻辑区分原因。
    def build_metadata(
        self,
        status: TaskStatus | str,
        *,
        run_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
        recoverable: bool | None = None,
    ) -> dict[str, Any]:
        coerced = self._coerce_status(status)
        assert coerced is not None  # status param is never None here
        metadata: dict[str, Any] = {
            "task_status": coerced.value,
            "task_error": error,
            "task_error_code": error_code,
        }
        if run_id:
            metadata["current_run_id"] = run_id

        if recoverable is not None:
            metadata["task_recoverable"] = recoverable
        elif status in {
            TaskStatus.QUEUED,
            TaskStatus.PENDING,
            TaskStatus.STARTING,
            TaskStatus.RUNNING,
            TaskStatus.CANCELLING,
            TaskStatus.CANCELLED,
            TaskStatus.COMPLETED,
            TaskStatus.EXPIRED,
        }:
            metadata["task_recoverable"] = False

        if status == TaskStatus.CANCELLED and error_code is None:
            metadata["task_error_code"] = "cancelled"
        if status == TaskStatus.EXPIRED and error_code is None:
            metadata["task_error_code"] = "expired"

        return metadata

    # 把 None / 字符串 / 枚举统一归一为 TaskStatus（None 透传），供内部比较使用。
    @staticmethod
    def _coerce_status(status: TaskStatus | str | None) -> TaskStatus | None:
        if status is None:
            return None
        if isinstance(status, TaskStatus):
            return status
        return TaskStatus(status)

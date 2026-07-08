"""scheduled_task_delete and scheduled_task_run tool implementations."""

import sys
from typing import TYPE_CHECKING, Annotated, Any

from langchain_core.tools import InjectedToolArg

from src.infra.scheduler.service import ScheduledTaskService
from src.infra.tool.backend_utils import get_user_id_from_runtime
from src.kernel.types import Permission

# ToolRuntime 兼容处理：不同 langchain 版本对该类型的导出路径不一致，
# 若正式包里没有该符号，就动态构造一个占位模块，避免 import 失败导致整个工具不可用
if TYPE_CHECKING:
    from langchain.tools import ToolRuntime
else:
    try:
        from langchain.tools import ToolRuntime  # type: ignore[assignment]
    except ImportError:  # pragma: no cover
        _mod = type(sys)("langchain.tools")  # type: ignore[assignment]
        _mod.ToolRuntime = Any  # type: ignore[assignment]
        sys.modules.setdefault("langchain.tools", _mod)
        from langchain.tools import ToolRuntime  # type: ignore[assignment]

from langchain.tools import tool  # noqa: E402

from src.infra.tool.scheduled_task.helpers import _json, _permission_error


@tool
async def scheduled_task_delete(
    task_id: Annotated[str, "ID of the task to delete"],
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Delete a scheduled task. This is a hard delete — the task document is physically
    removed from the database and will no longer appear in listings or fire."""
    # runtime 由 LangChain 框架自动注入（InjectedToolArg），从中解析出当前用户身份
    user_id = get_user_id_from_runtime(runtime)
    if not user_id:
        return _json({"error": "No user context available"})
    # 删除操作使用独立的 SCHEDULED_TASK_DELETE 权限点，与读/写权限区分
    error = await _permission_error(user_id, Permission.SCHEDULED_TASK_DELETE.value)
    if error:
        return _json(error)

    service = ScheduledTaskService()
    task = await service.get_task(task_id)
    if task is None:
        return _json({"error": f"Task '{task_id}' not found"})
    # 越权保护：即使 task_id 存在，也只允许任务所有者删除；
    # 非所有者一律返回"未找到"而不是"无权限"，避免暴露任务是否存在
    if task.owner_id != user_id:
        return _json({"error": f"Task '{task_id}' not found"})

    try:
        deleted = await service.delete_task(task_id)
    except Exception as e:
        return _json({"error": f"Failed to delete task: {e}"})

    if not deleted:
        return _json({"error": f"Task '{task_id}' delete failed"})
    return _json(
        {
            "success": True,
            "action": "deleted",
            "task_id": task_id,
            "message": f"Task '{task.name}' deleted.",
        }
    )


@tool
async def scheduled_task_run(
    task_id: Annotated[str, "ID of the task to trigger manually"],
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Manually trigger a scheduled task to run once immediately, regardless of its schedule.
    Useful for testing or ad-hoc execution."""
    user_id = get_user_id_from_runtime(runtime)
    if not user_id:
        return _json({"error": "No user context available"})
    # 手动触发一次运行属于"写"操作（会产生一次真实执行），而非删除权限
    error = await _permission_error(user_id, Permission.SCHEDULED_TASK_WRITE.value)
    if error:
        return _json(error)

    service = ScheduledTaskService()
    task = await service.get_task(task_id)
    if task is None:
        return _json({"error": f"Task '{task_id}' not found"})
    if task.owner_id != user_id:
        return _json({"error": f"Task '{task_id}' not found"})

    try:
        result = await service.run_task_now(task_id)
    except Exception as e:
        return _json({"error": f"Failed to run task: {e}"})

    return _json(
        {
            "success": True,
            "action": "triggered",
            "task_id": task_id,
            "name": task.name,
            "result": result,
            "message": f"Task '{task.name}' triggered manually.",
        }
    )

"""scheduled_task_list and scheduled_task_get tool implementations."""

import sys
from typing import TYPE_CHECKING, Annotated, Any, Optional

from langchain_core.tools import InjectedToolArg

from src.infra.scheduler.service import ScheduledTaskService
from src.infra.tool.backend_utils import get_user_id_from_runtime
from src.kernel.schemas.scheduled_task import ScheduledTaskStatus
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
async def scheduled_task_list(
    task_id: Annotated[
        str | None,
        "Optional scheduled task ID. When provided, returns detailed information for that task.",
    ] = None,
    status: Annotated[
        str | None,
        "Filter by status: 'active', 'paused', or omit to list all",
    ] = None,
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """List scheduled tasks owned by the current user. Provide task_id to fetch
    detailed information for a single task; otherwise optionally filter by
    status ('active' or 'paused')."""
    # runtime 由 LangChain 框架自动注入（InjectedToolArg），从中解析出当前用户身份
    user_id = get_user_id_from_runtime(runtime)
    if not user_id:
        return _json({"error": "No user context available"})
    error = await _permission_error(user_id, Permission.SCHEDULED_TASK_READ.value)
    if error:
        return _json(error)

    service = ScheduledTaskService()
    if task_id:
        # 传入 task_id 时退化为"查询单个任务详情"的行为
        try:
            task = await service.get_task(task_id)
        except Exception as e:
            return _json({"error": f"Failed to get task: {e}"})

        # 不存在或不属于当前用户，都统一返回"未找到"，不区分越权和真正不存在两种情况
        if task is None or task.owner_id != user_id:
            return _json({"error": f"Task '{task_id}' not found"})

        resp = ScheduledTaskService.to_response(task)
        return _json(
            {
                "success": True,
                "task": resp.model_dump(mode="json"),
            }
        )

    status_enum: Optional[ScheduledTaskStatus] = None
    if status:
        try:
            status_enum = ScheduledTaskStatus(status)
        except ValueError:
            return _json(
                {"error": f"Invalid status '{status}'. Use 'active', 'paused', or 'deleted'."}
            )

    try:
        # ScheduledTaskService.list_tasks 内部已按 owner_id 过滤，
        # 保证用户只能看到自己创建的定时任务
        tasks = await service.list_tasks(owner_id=user_id, status=status_enum)
    except Exception as e:
        return _json({"error": f"Failed to list tasks: {e}"})

    items = [ScheduledTaskService.to_response(t).model_dump(mode="json") for t in tasks]
    return _json(
        {
            "success": True,
            "tasks": items,
            "total": len(items),
        }
    )


@tool
async def scheduled_task_get(
    task_id: Annotated[str, "ID of the scheduled task"],
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Get detailed information about a specific scheduled task, including its
    last run status, total runs, and trigger configuration."""
    user_id = get_user_id_from_runtime(runtime)
    if not user_id:
        return _json({"error": "No user context available"})
    error = await _permission_error(user_id, Permission.SCHEDULED_TASK_READ.value)
    if error:
        return _json(error)

    service = ScheduledTaskService()
    try:
        task = await service.get_task(task_id)
    except Exception as e:
        return _json({"error": f"Failed to get task: {e}"})

    if task is None:
        return _json({"error": f"Task '{task_id}' not found"})

    # 越权保护：任务存在但不属于当前用户时，同样返回"未找到"而不是"无权限"，
    # 避免向调用方泄露"该 task_id 确实存在"这一信息
    if task.owner_id != user_id:
        return _json({"error": f"Task '{task_id}' not found"})

    resp = ScheduledTaskService.to_response(task)
    return _json(
        {
            "success": True,
            "task": resp.model_dump(mode="json"),
        }
    )

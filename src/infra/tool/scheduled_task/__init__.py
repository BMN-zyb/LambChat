"""LLM-callable scheduled task tools.

CRUD tools for creating and managing scheduled tasks.
Each operation is a separate @tool function.

Split from the original monolithic scheduled_task_tool.py.
"""

from langchain_core.tools import BaseTool

# 按 CRUD 职责拆分成独立模块：
# create（创建，含人工审批确认）/ read（查询列表与详情）/
# update（更新字段及暂停/恢复/立即执行的生命周期操作）/ delete（删除与手动触发一次）。
# approval.py 与 helpers.py 是被上述模块共享的内部辅助逻辑，不对外注册为工具。
from src.infra.tool.scheduled_task.create import scheduled_task_create
from src.infra.tool.scheduled_task.delete import scheduled_task_delete, scheduled_task_run
from src.infra.tool.scheduled_task.read import scheduled_task_get, scheduled_task_list
from src.infra.tool.scheduled_task.update import (
    scheduled_task_pause,
    scheduled_task_resume,
    scheduled_task_update,
)

__all__ = [
    "scheduled_task_create",
    "scheduled_task_list",
    "scheduled_task_get",
    "scheduled_task_update",
    "scheduled_task_pause",
    "scheduled_task_resume",
    "scheduled_task_delete",
    "scheduled_task_run",
    "get_scheduled_task_tools",
]


def get_scheduled_task_tools() -> list[BaseTool]:
    """Return scheduled task CRUD tools for the current user."""
    # 只暴露最常用的四个工具给 agent 绑定使用；scheduled_task_get/pause/resume/run
    # 仍可通过 __all__ 直接导入使用（例如管理后台或其他内部调用），
    # 但不出现在默认绑定给 LLM 的工具集里，避免工具列表过长增加模型选择负担
    return [
        scheduled_task_create,
        scheduled_task_list,
        scheduled_task_update,
        scheduled_task_delete,
    ]

"""Compatibility exports for scheduled task tools.

The implementations live in ``src.infra.tool.scheduled_task``. This module keeps
the historical import path and test monkeypatch points working.
"""
# 中文说明：定时任务工具的真正实现已经拆分到 src.infra.tool.scheduled_task
# 这个子包（approval/create/delete/helpers/read/update 等子模块）里，
# 本文件只是一个"兼容层"——早期所有代码都写在这一个模块中，为了不破坏
# 历史上 `from src.infra.tool.scheduled_task_tool import xxx` 的导入路径，
# 以及测试用例里对这些名字的 monkeypatch，这里把子模块的公开符号重新导出一遍。
# 难点在于 monkeypatch 兼容：测试通常会 `monkeypatch.setattr(scheduled_task_tool,
# "ScheduledTaskService", FakeService)`，但真正调用 ScheduledTaskService 的代码在
# _create_mod/_read_mod 等子模块内部，它们各自用 `from ... import ScheduledTaskService`
# 拿到的是"当时那个对象的独立引用"，仅修改本模块的属性并不会影响子模块里已经
# 绑定好的那个名字。为此下面用 _PATCH_TARGETS + 自定义 __setattr__ 把"设置本模块属性"
# 这个动作，同步广播到所有实际使用该名字的子模块上，让 monkeypatch 才能真正生效。

import sys
import types
from typing import Any

# 中文：下面分三类导入——
#   1）服务/管理器类（ScheduledTaskService、PersonaPresetManager、TeamManager 等），
#      它们同时会被登记进 _PATCH_TARGETS，允许测试整体替换实现；
#   2）以 `_xxx_mod` 命名导入的子模块本身（approval/create/delete/helpers/read/update），
#      用于在 _PATCH_TARGETS 中作为"广播目标"引用；
#   3）子模块中具体的工具函数/私有辅助函数，直接重新导出，保持旧的导入路径可用。
from src.api.routes.human import create_approval, wait_for_response
from src.infra.persona_preset.manager import PersonaPresetManager
from src.infra.scheduler.service import ScheduledTaskService
from src.infra.team.manager import TeamManager
from src.infra.tool.scheduled_task import approval as _approval_mod
from src.infra.tool.scheduled_task import create as _create_mod
from src.infra.tool.scheduled_task import delete as _delete_mod
from src.infra.tool.scheduled_task import get_scheduled_task_tools
from src.infra.tool.scheduled_task import helpers as _helpers_mod
from src.infra.tool.scheduled_task import read as _read_mod
from src.infra.tool.scheduled_task import update as _update_mod
from src.infra.tool.scheduled_task.approval import (
    _confirm_scheduled_task_creation,
    _format_approval_message,
    _resolve_persona_preset_id_from_query,
    _resolve_team_id_from_query,
    _send_scheduled_task_approval_event,
)
from src.infra.tool.scheduled_task.create import _parse_run_at_iso, scheduled_task_create
from src.infra.tool.scheduled_task.delete import scheduled_task_delete, scheduled_task_run
from src.infra.tool.scheduled_task.helpers import (
    _build_task_preview,
    _coerce_channel_delivery,
    _format_trigger_preview,
    _get_current_session_defaults,
    _json,
    _permission_error,
    _resolve_user,
    _strip_resolved_agent_options,
)
from src.infra.tool.scheduled_task.read import scheduled_task_get, scheduled_task_list
from src.infra.tool.scheduled_task.update import (
    scheduled_task_pause,
    scheduled_task_resume,
    scheduled_task_update,
)
from src.infra.utils.datetime import utc_now

# 中文：__all__ 列出本兼容层对外重新导出的全部符号，供 `from ... import *`
# 或静态分析工具识别；内容涵盖各子模块的工具函数、辅助函数与内部私有函数
__all__ = [
    "ScheduledTaskService",
    "PersonaPresetManager",
    "TeamManager",
    "create_approval",
    "wait_for_response",
    "utc_now",
    "_json",
    "_strip_resolved_agent_options",
    "_resolve_user",
    "_get_current_session_defaults",
    "_coerce_channel_delivery",
    "_permission_error",
    "_format_trigger_preview",
    "_build_task_preview",
    "_format_approval_message",
    "_resolve_persona_preset_id_from_query",
    "_resolve_team_id_from_query",
    "_send_scheduled_task_approval_event",
    "_confirm_scheduled_task_creation",
    "_parse_run_at_iso",
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

_PATCH_TARGETS = {
    "ScheduledTaskService": (_create_mod, _read_mod, _update_mod, _delete_mod),
    "utc_now": (_create_mod,),
    "_get_current_session_defaults": (_create_mod, _helpers_mod),
    "_permission_error": (_create_mod, _read_mod, _update_mod, _delete_mod, _helpers_mod),
    "_resolve_user": (_helpers_mod,),
    "PersonaPresetManager": (_approval_mod,),
    "TeamManager": (_approval_mod,),
    "create_approval": (_approval_mod,),
    "wait_for_response": (_approval_mod,),
    "_send_scheduled_task_approval_event": (_approval_mod,),
    "_confirm_scheduled_task_creation": (_create_mod, _approval_mod),
}
# 中文：上面这份映射表是"谁在用哪个名字"的清单——key 是可能被测试
# monkeypatch 的名字，value 是所有从子模块里 import 了这个名字、
# 因而持有独立引用的子模块列表。下面的自定义 __setattr__ 会据此表
# 把对本模块该属性的赋值，同步写回到每一个列出的子模块里。


class _ScheduledTaskToolCompatModule(types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        # 先正常设置本模块自身的属性（保持 `scheduled_task_tool.X` 可读的行为不变）
        super().__setattr__(name, value)
        # 再把同一个值广播给 _PATCH_TARGETS 中登记的所有子模块，
        # 确保子模块内部实际执行时用到的也是被 monkeypatch 后的新值
        for module in _PATCH_TARGETS.get(name, ()):
            setattr(module, name, value)


# 中文：Python 允许在运行时替换一个已加载模块对象的 __class__，
# 这里把当前模块（sys.modules[__name__]，即本文件对应的 module 对象）的类型
# 由默认的 types.ModuleType 换成上面自定义的子类，从而让后续任何
# `scheduled_task_tool.某属性 = 新值` 形式的赋值都会触发自定义的 __setattr__ 广播逻辑。
sys.modules[__name__].__class__ = _ScheduledTaskToolCompatModule

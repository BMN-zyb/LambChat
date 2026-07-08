"""LLM-callable persona preset tools.

Internal tools for creating and updating persona presets, following the same
pattern as env_var_tool.py. Permission checks happen at invocation time.
"""
# 中文说明：人设预设（persona preset）本质是"系统提示词 + 元信息"的可复用配置，
# 既可以被用户直接选用，也可以作为 team_tool.py 组建团队时的成员角色来源。
# 本模块提供三个工具：
#   - save_persona_preset：统一的创建/更新入口（推荐，未来新功能优先加在这里）；
#   - create_persona_preset / update_persona_preset：早期拆分的创建、更新工具，
#     仍保留以兼容可能直接引用它们的旧调用方，但默认工具集
#     （get_persona_preset_tools）只对外暴露 save_persona_preset。
# 权限校验在每个工具函数内部即时进行（而不是在装饰器/路由层统一拦截），
# 因为这里没有 HTTP 请求上下文，只能在工具调用时现查用户角色与权限。

import json
import sys
from typing import TYPE_CHECKING, Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolArg

from src.infra.async_utils import run_blocking_io
from src.infra.persona_preset.manager import PersonaPresetManager
from src.infra.role.storage import RoleStorage
from src.infra.tool.backend_utils import get_user_id_from_runtime
from src.infra.user.storage import UserStorage
from src.kernel.exceptions import AuthorizationError, NotFoundError
from src.kernel.schemas.persona_preset import (
    PersonaPresetCreate,
    PersonaPresetScope,
    PersonaPresetStatus,
    PersonaPresetUpdate,
    PersonaPresetVisibility,
    PersonaStarterPrompt,
)
from src.kernel.schemas.user import TokenPayload

if TYPE_CHECKING:
    from langchain.tools import ToolRuntime
else:
    try:
        from langchain.tools import ToolRuntime  # type: ignore[assignment]
    except ImportError:  # pragma: no cover
        # 兼容旧版本 langchain：找不到 ToolRuntime 时动态构造占位模块
        _mod = type(sys)("langchain.tools")  # type: ignore[assignment]
        _mod.ToolRuntime = Any  # type: ignore[assignment]
        sys.modules.setdefault("langchain.tools", _mod)
        from langchain.tools import ToolRuntime  # type: ignore[assignment]

from langchain.tools import tool  # noqa: E402


async def _json_dumps_result(data: dict[str, Any]) -> str:
    # 统一以 JSON 字符串形式返回工具结果给 LLM
    return await run_blocking_io(json.dumps, data, ensure_ascii=False, default=str)


def _get_user_id(runtime: ToolRuntime) -> str | None:
    # 从 runtime 中解析当前用户 id
    return get_user_id_from_runtime(runtime)


async def _resolve_user(user_id: str) -> TokenPayload | None:
    """Resolve the latest roles and permissions for a user ID."""
    # 中文：工具调用时只有 user_id，没有原始请求鉴权生成的 TokenPayload，
    # 这里重新查库拼出一份等价对象（角色 + 汇总权限），供后续权限判断使用
    user = await UserStorage().get_by_id(user_id)
    if not user:
        return None

    role_storage = RoleStorage()
    roles = await role_storage.get_by_names(user.roles or [])

    permissions: set[str] = set()
    for role in roles:
        for permission in role.permissions:
            permissions.add(permission if isinstance(permission, str) else permission.value)

    return TokenPayload(
        sub=user.id,
        username=user.username,
        roles=[r.name for r in roles],
        permissions=sorted(permissions),
    )


def _is_admin(user: TokenPayload) -> bool:
    return "persona_preset:admin" in set(user.permissions or [])


# 中文：统一的人设创建/更新入口——不传 preset_id/current_name 时创建新人设，
# 传了其中之一则视为更新；team_tool.create_agent_team 找不到合适人设时
# 会引导 LLM 先调用本工具创建，再把返回的 preset.id 用作团队成员的 persona_preset_id
@tool
async def save_persona_preset(
    name: Annotated[
        str | None,
        "Persona name. Required when creating; optional new name when updating.",
    ] = None,
    system_prompt: Annotated[
        str | None,
        "Full system prompt. Required when creating; optional full replacement when updating.",
    ] = None,
    preset_id: Annotated[str | None, "Optional persona id to update when known."] = None,
    current_name: Annotated[
        str | None, "Existing persona name to update when id is unknown."
    ] = None,
    description: Annotated[str | None, "Short one-line description"] = None,
    avatar: Annotated[
        str | None,
        "emoji or avatar image URL for this persona.",
    ] = None,
    tags: Annotated[list[str] | None, "Tags for categorization"] = None,
    starter_prompts: Annotated[
        list[PersonaStarterPrompt] | None,
        "Starter prompt suggestions for this persona.",
    ] = None,
    skill_names: Annotated[list[str] | None, "Skill/tool names to enable"] = None,
    scope: Annotated[
        str | None,
        "Updated scope: 'user' or 'global'. Global requires admin permission.",
    ] = None,
    visibility: Annotated[
        str | None,
        "Visibility: 'private' (only you) or 'public' (all users)",
    ] = None,
    status: Annotated[
        str | None,
        "Status: 'draft' (work in progress) or 'published' (ready to use)",
    ] = None,
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Create or update a persona preset for the current user.

    Omit preset_id/current_name to create; pass preset_id or current_name to update.
    When a team role needs a new persona, create it here and pass preset.id into
    create_agent_team.
    """
    user_id = _get_user_id(runtime)
    if not user_id:
        return await _json_dumps_result({"error": "No user context available"})

    user = await _resolve_user(user_id)
    if not user or "persona_preset:write" not in set(user.permissions):
        return await _json_dumps_result(
            {"error": "Permission denied: persona_preset:write required"}
        )

    manager = PersonaPresetManager()
    # 中文：只要传了 preset_id，或者 current_name 非空，就判定为"更新"分支；
    # 两者都没传则走"创建"分支
    is_update = bool(preset_id or (current_name and current_name.strip()))

    if not is_update:
        # ------------------------- 创建分支 -------------------------
        if not name or not system_prompt:
            return await _json_dumps_result(
                {"error": "name and system_prompt are required when creating a persona preset"}
            )
        try:
            # 校验枚举值合法性；非法值直接拒绝，而不是让 Pydantic 抛出难懂的异常
            vis = PersonaPresetVisibility(visibility or "private")
            st = PersonaPresetStatus(status or "draft")
        except ValueError:
            return await _json_dumps_result({"error": "Invalid visibility or status value"})
        try:
            preset = await manager.create_preset(
                PersonaPresetCreate(
                    name=name,
                    description=description or "",
                    avatar=avatar,
                    tags=tags or [],
                    system_prompt=system_prompt,
                    starter_prompts=starter_prompts or [],
                    skill_names=skill_names or [],
                    visibility=vis,
                    status=st,
                ),
                user_id=user_id,
                is_admin=_is_admin(user),
            )
        except Exception as e:
            return await _json_dumps_result({"error": f"Failed to create preset: {e}"})
        return await _json_dumps_result(
            {
                "success": True,
                "entity_type": "persona_preset",
                "action": "created",
                "preset": preset.model_dump(mode="json"),
                "message": f"Persona preset '{preset.name}' created.",
            }
        )

    # ------------------------- 更新分支 -------------------------
    # 提前校验各枚举字段，避免走到后面 fields 组装/调用时才因非法值报错
    if scope is not None:
        try:
            PersonaPresetScope(scope)
        except ValueError:
            return await _json_dumps_result(
                {"error": "Invalid scope value. Must be 'user' or 'global'"}
            )
    if visibility is not None:
        try:
            PersonaPresetVisibility(visibility)
        except ValueError:
            return await _json_dumps_result({"error": "Invalid visibility value"})
    if status is not None:
        try:
            PersonaPresetStatus(status)
        except ValueError:
            return await _json_dumps_result({"error": "Invalid status value"})

    # 优先使用明确的 preset_id；未提供时按 current_name 在该用户名下的人设中精确匹配查找
    resolved_preset_id = preset_id
    if not resolved_preset_id:
        presets = await manager.list_presets(
            user_id=user_id,
            is_admin=_is_admin(user),
            scope="user",
            q=str(current_name).strip(),
            limit=20,
        )
        # q 是模糊搜索，这里再做一次精确名称匹配以避免误更新到同名近似的其它人设
        exact_matches = [p for p in presets if p.name == str(current_name).strip()]
        if len(exact_matches) == 1:
            resolved_preset_id = exact_matches[0].id
        elif len(exact_matches) > 1:
            # 存在多个同名人设，无法确定唯一目标，要求 LLM 改用 preset_id 精确指定
            return await _json_dumps_result(
                {"error": f"Multiple persona presets named '{current_name}' were found"}
            )
        else:
            return await _json_dumps_result({"error": f"Persona preset '{current_name}' not found"})

    # 只把 LLM 显式传入（非 None）的字段放进待更新字典，实现"部分更新"语义，
    # 未传的字段保持数据库中原值不变
    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if avatar is not None:
        fields["avatar"] = avatar
    if tags is not None:
        fields["tags"] = tags
    if system_prompt is not None:
        fields["system_prompt"] = system_prompt
    if starter_prompts is not None:
        fields["starter_prompts"] = starter_prompts
    if skill_names is not None:
        fields["skill_names"] = skill_names
    if scope is not None:
        fields["scope"] = PersonaPresetScope(scope)
        # 中文：升级为 global（官方/全局）人设时，若调用方未显式指定
        # visibility/status，则自动补齐为"公开+已发布"，符合 global 人设的默认预期
        if scope == PersonaPresetScope.GLOBAL.value:
            if visibility is None:
                fields["visibility"] = PersonaPresetVisibility.PUBLIC
            if status is None:
                fields["status"] = PersonaPresetStatus.PUBLISHED
    if visibility is not None and "visibility" not in fields:
        fields["visibility"] = PersonaPresetVisibility(visibility)
    if status is not None:
        fields["status"] = PersonaPresetStatus(status)
    if not fields:
        return await _json_dumps_result({"error": "At least one field to update is required"})

    try:
        preset = await manager.update_preset(
            str(resolved_preset_id),
            PersonaPresetUpdate(**fields),
            user_id=user_id,
            is_admin=_is_admin(user),
        )
    except (NotFoundError, AuthorizationError) as e:
        # 中文：找不到人设 / 无权限修改（如尝试改别人的私有人设）都归为业务错误，
        # 直接把异常信息回传给 LLM，而不是当作未知异常处理
        return await _json_dumps_result({"error": str(e)})
    except Exception as e:
        return await _json_dumps_result({"error": f"Failed to update preset: {e}"})

    return await _json_dumps_result(
        {
            "success": True,
            "entity_type": "persona_preset",
            "action": "updated",
            "preset": preset.model_dump(mode="json"),
            "message": f"Persona preset '{preset.name}' updated.",
        }
    )


# 中文：早期版本的"仅创建"工具，逻辑等价于 save_persona_preset 的创建分支；
# 默认工具集不再暴露它（见 get_persona_preset_tools），保留仅为兼容直接引用的旧调用方
@tool
async def create_persona_preset(
    name: Annotated[str, "Persona preset name, e.g. 'Code Reviewer', 'Translator'"],
    system_prompt: Annotated[
        str,
        "System prompt that defines the persona's behavior, personality, and rules. "
        "Write clear instructions covering: 1) Role identity (who the persona is), "
        "2) Behavioral guidelines (how it should act), 3) Output format preferences, "
        "4) Constraints (what it must not do). "
        "Example: 'You are a senior code reviewer. Focus on correctness, security, and readability. "
        "Always suggest fixes alongside issues. Never approve code with SQL injection risks.'",
    ],
    description: Annotated[str, "Short one-line description of what this persona does"] = "",
    avatar: Annotated[
        str | None,
        "Always provide an emoji or avatar image URL for this persona when creating a "
        "role. Use a single emoji such as '🧭' or an image URL such as "
        "'https://example.com/avatar.png'.",
    ] = None,
    tags: Annotated[list[str], "Optional tags for categorization, e.g. ['coding', 'review']"] = [],
    starter_prompts: Annotated[
        list[PersonaStarterPrompt],
        "Prompt suggestions shown after selecting this persona. "
        "Each entry is an object with 'text' (a plain string or a multi-language dict like {'zh': '中文', 'en': 'English'}) "
        "and an optional 'icon' (a single emoji, e.g. '🐍', '🧭').",
    ] = [],
    skill_names: Annotated[list[str], "Optional skill/tool names to enable for this persona"] = [],
    visibility: Annotated[
        str,
        "Visibility: 'private' (only you) or 'public' (all users)",
    ] = "private",
    status: Annotated[
        str,
        "Status: 'draft' (work in progress) or 'published' (ready to use)",
    ] = "draft",
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Create a new persona preset (AI character/role) for the current user.
    Prefer save_persona_preset for new tool exposure."""
    user_id = _get_user_id(runtime)
    if not user_id:
        return await _json_dumps_result({"error": "No user context available"})

    user = await _resolve_user(user_id)
    if not user or "persona_preset:write" not in set(user.permissions):
        return await _json_dumps_result(
            {"error": "Permission denied: persona_preset:write required"}
        )

    try:
        vis = PersonaPresetVisibility(visibility)
        st = PersonaPresetStatus(status)
    except ValueError:
        return await _json_dumps_result({"error": "Invalid visibility or status value"})

    manager = PersonaPresetManager()
    try:
        preset = await manager.create_preset(
            PersonaPresetCreate(
                name=name,
                description=description,
                avatar=avatar,
                tags=tags,
                system_prompt=system_prompt,
                starter_prompts=starter_prompts,
                skill_names=skill_names,
                visibility=vis,
                status=st,
            ),
            user_id=user_id,
            is_admin=_is_admin(user),
        )
    except Exception as e:
        return await _json_dumps_result({"error": f"Failed to create preset: {e}"})
    return await _json_dumps_result(
        {
            "success": True,
            "entity_type": "persona_preset",
            "action": "created",
            "preset": preset.model_dump(mode="json"),
            "message": f"Persona preset '{preset.name}' created.",
        }
    )


# 中文：早期版本的"仅更新"工具，字段解析/校验逻辑与 save_persona_preset 的更新分支
# 几乎一致（按名称模糊搜索再精确匹配、部分字段更新等），默认工具集不再暴露它
@tool
async def update_persona_preset(
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
    preset_id: Annotated[str | None, "Exact preset id to update when known"] = None,
    current_name: Annotated[str | None, "Existing persona name when preset id is unknown"] = None,
    name: Annotated[str | None, "New persona name"] = None,
    description: Annotated[str | None, "New one-line description"] = None,
    avatar: Annotated[str | None, "New avatar URL"] = None,
    tags: Annotated[list[str] | None, "Updated tags for categorization"] = None,
    system_prompt: Annotated[
        str | None,
        "Updated system prompt. Should clearly define: role identity, behavioral rules, "
        "output format, and constraints. Be specific and detailed for best results.",
    ] = None,
    starter_prompts: Annotated[
        list[PersonaStarterPrompt] | None,
        "Updated list of starter prompt suggestions. "
        "Each entry is an object with 'text' (a plain string or a multi-language dict like {'zh': '中文', 'en': 'English'}) "
        "and an optional 'icon' (a single emoji, e.g. '🐍', '🧭').",
    ] = None,
    skill_names: Annotated[list[str] | None, "Updated skill/tool names"] = None,
    scope: Annotated[
        str | None, "Updated scope: 'user' or 'global'. Global (official) requires admin permission"
    ] = None,
    visibility: Annotated[str | None, "Updated visibility: 'private' or 'public'"] = None,
    status: Annotated[str | None, "Updated status: 'draft' or 'published'"] = None,
) -> str:
    """Update an existing persona preset. Provide preset_id or current_name to identify
    the target, then pass only the fields you want to change.
    When updating system_prompt, rewrite the full prompt (partial edits are not supported)."""
    user_id = _get_user_id(runtime)
    if not user_id:
        return await _json_dumps_result({"error": "No user context available"})

    user = await _resolve_user(user_id)
    if not user or "persona_preset:write" not in set(user.permissions):
        return await _json_dumps_result(
            {"error": "Permission denied: persona_preset:write required"}
        )

    if scope is not None:
        try:
            PersonaPresetScope(scope)
        except ValueError:
            return await _json_dumps_result(
                {"error": "Invalid scope value. Must be 'user' or 'global'"}
            )
    if visibility is not None:
        try:
            PersonaPresetVisibility(visibility)
        except ValueError:
            return await _json_dumps_result({"error": "Invalid visibility value"})
    if status is not None:
        try:
            PersonaPresetStatus(status)
        except ValueError:
            return await _json_dumps_result({"error": "Invalid status value"})

    manager = PersonaPresetManager()

    # 优先使用明确的 preset_id；未提供时按 current_name 精确匹配查找（逻辑与
    # save_persona_preset 的更新分支一致）
    resolved_preset_id = preset_id
    if not resolved_preset_id:
        if not current_name or not current_name.strip():
            return await _json_dumps_result(
                {"error": "Either preset_id or current_name is required"}
            )
        presets = await manager.list_presets(
            user_id=user_id,
            is_admin=_is_admin(user),
            scope="user",
            q=current_name.strip(),
            limit=20,
        )
        # q 是模糊搜索，这里再做一次精确名称匹配，避免误更新到同名近似的其它人设
        exact_matches = [p for p in presets if p.name == current_name.strip()]
        if len(exact_matches) == 1:
            resolved_preset_id = exact_matches[0].id
        elif len(exact_matches) > 1:
            return await _json_dumps_result(
                {"error": f"Multiple persona presets named '{current_name}' were found"}
            )
        else:
            return await _json_dumps_result({"error": f"Persona preset '{current_name}' not found"})

    # 同样只收集显式传入（非 None）的字段，实现部分更新语义
    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if avatar is not None:
        fields["avatar"] = avatar
    if tags is not None:
        fields["tags"] = tags
    if system_prompt is not None:
        fields["system_prompt"] = system_prompt
    if starter_prompts is not None:
        fields["starter_prompts"] = starter_prompts
    if skill_names is not None:
        fields["skill_names"] = skill_names
    if scope is not None:
        fields["scope"] = PersonaPresetScope(scope)
        # 升级为 global 人设时若未显式指定 visibility/status，自动补齐默认值
        if scope == PersonaPresetScope.GLOBAL.value:
            if visibility is None:
                fields["visibility"] = PersonaPresetVisibility.PUBLIC
            if status is None:
                fields["status"] = PersonaPresetStatus.PUBLISHED
    if visibility is not None and "visibility" not in fields:
        fields["visibility"] = PersonaPresetVisibility(visibility)
    if status is not None:
        fields["status"] = PersonaPresetStatus(status)
    if not fields:
        return await _json_dumps_result({"error": "At least one field to update is required"})

    update_data = PersonaPresetUpdate(**fields)

    try:
        preset = await manager.update_preset(
            resolved_preset_id,
            update_data,
            user_id=user_id,
            is_admin=_is_admin(user),
        )
    except (NotFoundError, AuthorizationError) as e:
        return await _json_dumps_result({"error": str(e)})
    except Exception as e:
        return await _json_dumps_result({"error": f"Failed to update preset: {e}"})

    return await _json_dumps_result(
        {
            "success": True,
            "entity_type": "persona_preset",
            "action": "updated",
            "preset": preset.model_dump(mode="json"),
            "message": f"Persona preset '{preset.name}' updated.",
        }
    )


def get_persona_preset_tools() -> list[BaseTool]:
    """Return persona preset CRUD tools for the current user."""
    # 中文：只暴露统一入口 save_persona_preset；create/update_persona_preset
    # 仍定义在模块中但不注册进默认工具集，避免同一能力有两套入口让 LLM 困惑
    return [save_persona_preset]

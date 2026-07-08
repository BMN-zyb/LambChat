"""Persona preset routes."""

# 人格预设路由模块（挂载于 /api/persona-presets）
# 职责：人格预设的增删改查、批量创建、复制、启用为运行时快照，以及收藏/置顶偏好维护
# 可见性区分：普通用户可见"全局预设 + 自己的私有预设"，拥有 persona_preset:admin 的管理员可管理全局预设
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from src.api.deps import require_permissions
from src.infra.persona_preset.manager import PersonaPresetManager
from src.kernel.exceptions import AuthorizationError, NotFoundError
from src.kernel.schemas.persona_preset import (
    PersonaPreset,
    PersonaPresetCreate,
    PersonaPresetListResponse,
    PersonaPresetPreferenceUpdate,
    PersonaPresetSnapshot,
    PersonaPresetUpdate,
)
from src.kernel.schemas.user import TokenPayload

router = APIRouter()


# 判断当前用户是否为人格预设管理员（拥有 persona_preset:admin 权限，可操作全局预设）
def _is_admin(user: TokenPayload) -> bool:
    return "persona_preset:admin" in set(user.permissions or [])


# 工厂函数：创建人格预设管理器（业务逻辑与存储都封装在 PersonaPresetManager）
def _manager() -> PersonaPresetManager:
    return PersonaPresetManager()


# GET /api/persona-presets/ —— 列出当前用户可见的人格预设，需要 persona_preset:read 权限
# 支持按 scope（作用域）/status/tag/关键词 q/favorite/pinned 过滤，skip/limit 分页
# 是否管理员决定可见范围；返回预设列表 + total 总数（total 用相同过滤条件单独统计）
@router.get("/", response_model=PersonaPresetListResponse)
async def list_persona_presets(
    scope: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    favorite: bool | None = None,
    pinned: bool | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    user: TokenPayload = Depends(require_permissions("persona_preset:read")),
):
    """List visible persona presets."""
    presets = await _manager().list_presets(
        user_id=user.sub,
        is_admin=_is_admin(user),
        scope=scope,
        status=status,
        tag=tag,
        q=q,
        favorite=favorite,
        pinned=pinned,
        skip=skip,
        limit=limit,
    )
    total = await _manager().count_presets(
        user_id=user.sub,
        is_admin=_is_admin(user),
        scope=scope,
        status=status,
        tag=tag,
        q=q,
        favorite=favorite,
        pinned=pinned,
        skip=skip,
        limit=limit,
    )
    return PersonaPresetListResponse(
        presets=presets,
        total=total,
        skip=skip,
        limit=limit,
    )


# POST /api/persona-presets/ —— 创建人格预设，需要 persona_preset:write 权限
# 普通用户创建私有预设；管理员可创建全局预设。越权（如普通用户建全局）时 manager 抛 AuthorizationError -> 403
@router.post("/", response_model=PersonaPreset)
async def create_persona_preset(
    preset_data: PersonaPresetCreate,
    user: TokenPayload = Depends(require_permissions("persona_preset:write")),
):
    """Create a user preset or, for admins, a global preset."""
    try:
        return await _manager().create_preset(
            preset_data,
            user_id=user.sub,
            is_admin=_is_admin(user),
        )
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))


# POST /api/persona-presets/batch —— 批量创建人格预设（请求体一次最多 500 条），需要 persona_preset:write 权限
# 请求体为 PersonaPresetCreate 列表；返回创建后的预设列表
@router.post("/batch", response_model=list[PersonaPreset])
async def batch_create_persona_presets(
    items: Annotated[list[PersonaPresetCreate], Body(max_length=500)],
    user: TokenPayload = Depends(require_permissions("persona_preset:write")),
):
    """Batch create persona presets."""
    return await _manager().batch_create_presets(
        items,
        user_id=user.sub,
        is_admin=_is_admin(user),
    )


# GET /api/persona-presets/{preset_id} —— 获取一个可见的人格预设，需要 persona_preset:read 权限
# 不可见或不存在时抛 404（persona_preset_not_found）
@router.get("/{preset_id}", response_model=PersonaPreset)
async def get_persona_preset(
    preset_id: str,
    user: TokenPayload = Depends(require_permissions("persona_preset:read")),
):
    """Get a visible persona preset."""
    try:
        return await _manager().get_preset(
            preset_id,
            user_id=user.sub,
            is_admin=_is_admin(user),
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="persona_preset_not_found")


# PUT /api/persona-presets/{preset_id} —— 更新可编辑的人格预设，需要 persona_preset:write 权限
# 不存在抛 404；无编辑权限（如改他人/全局预设）抛 403
@router.put("/{preset_id}", response_model=PersonaPreset)
async def update_persona_preset(
    preset_id: str,
    preset_data: PersonaPresetUpdate,
    user: TokenPayload = Depends(require_permissions("persona_preset:write")),
):
    """Update an editable persona preset."""
    try:
        return await _manager().update_preset(
            preset_id,
            preset_data,
            user_id=user.sub,
            is_admin=_is_admin(user),
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="persona_preset_not_found")
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))


# DELETE /api/persona-presets/{preset_id} —— 删除可编辑的人格预设，需要 persona_preset:write 权限
# 成功返回 {"status": "deleted"}；不存在抛 404，无权限抛 403
@router.delete("/{preset_id}")
async def delete_persona_preset(
    preset_id: str,
    user: TokenPayload = Depends(require_permissions("persona_preset:write")),
):
    """Delete an editable persona preset."""
    try:
        await _manager().delete_preset(
            preset_id,
            user_id=user.sub,
            is_admin=_is_admin(user),
        )
        return {"status": "deleted"}
    except NotFoundError:
        raise HTTPException(status_code=404, detail="persona_preset_not_found")
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))


# POST /api/persona-presets/{preset_id}/copy —— 把一个可见预设复制为当前用户的私有预设，需要 persona_preset:write 权限
# 常用于以他人/全局预设为模板再自行修改；源不可见/不存在抛 404
@router.post("/{preset_id}/copy", response_model=PersonaPreset)
async def copy_persona_preset(
    preset_id: str,
    user: TokenPayload = Depends(require_permissions("persona_preset:write")),
):
    """Copy a visible preset into the current user's private presets."""
    try:
        return await _manager().copy_preset(
            preset_id,
            user_id=user.sub,
            is_admin=_is_admin(user),
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="persona_preset_not_found")


# POST /api/persona-presets/{preset_id}/use —— 将预设解析为运行时快照（PersonaPresetSnapshot），需要 persona_preset:read 权限
# 快照是应用到会话时真正生效的人格内容；不可见/不存在抛 404
@router.post("/{preset_id}/use", response_model=PersonaPresetSnapshot)
async def use_persona_preset(
    preset_id: str,
    user: TokenPayload = Depends(require_permissions("persona_preset:read")),
):
    """Resolve a persona preset into a runtime snapshot."""
    try:
        return await _manager().use_preset(
            preset_id,
            user_id=user.sub,
            is_admin=_is_admin(user),
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="persona_preset_not_found")


# PATCH /api/persona-presets/{preset_id}/preference —— 更新当前用户对某可见预设的收藏/置顶状态，需要 persona_preset:read 权限
# 仅记录个人偏好（is_favorite/is_pinned），不修改预设本身内容；不可见/不存在抛 404
@router.patch("/{preset_id}/preference", response_model=PersonaPreset)
async def update_persona_preset_preference(
    preset_id: str,
    preference: PersonaPresetPreferenceUpdate,
    user: TokenPayload = Depends(require_permissions("persona_preset:read")),
):
    """Update the current user's favorite/pinned state for a visible preset."""
    try:
        return await _manager().update_preference(
            preset_id,
            user_id=user.sub,
            is_admin=_is_admin(user),
            is_favorite=preference.is_favorite,
            is_pinned=preference.is_pinned,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="persona_preset_not_found")

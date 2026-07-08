"""
角色路由
"""

# 角色路由模块（挂载于 /api/roles），RBAC 权限体系的核心
# 职责：角色的增删改查；每个角色关联一组权限点(permission)，用户通过所属角色获得权限
# 权限约束：列表仅需登录；创建/查看/修改/删除均需 role:manage 权限
# 安全保护：系统内置角色(is_system) 不允许用户修改自己所属角色的权限，防止自我提权/锁死
from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import (
    get_current_user_required,
    require_permissions,
)
from src.infra.role.manager import RoleManager
from src.kernel.exceptions import ValidationError
from src.kernel.schemas.role import Role, RoleCreate, RoleListResponse, RoleUpdate
from src.kernel.schemas.user import TokenPayload

router = APIRouter()


# GET /api/roles/ —— 分页列出角色，仅需登录（供前端选择角色用）
# 查询参数：q 关键词过滤，skip/limit 分页；返回角色列表 + total
@router.get("/", response_model=RoleListResponse)
async def list_roles(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    q: str | None = None,
    _: TokenPayload = Depends(get_current_user_required),
):
    """列出角色（只需登录）"""
    manager = RoleManager()
    roles = await manager.list_roles(skip, limit, q)
    total = await manager.count_roles(q)
    return RoleListResponse(roles=roles, total=total, skip=skip, limit=limit)


# POST /api/roles/ —— 创建角色，需要 role:manage 权限
# 请求体 RoleCreate（角色名 + 权限点集合）
@router.post("/", response_model=Role)
async def create_role(
    role_data: RoleCreate,
    _: None = Depends(require_permissions("role:manage")),
):
    """创建角色"""
    manager = RoleManager()
    return await manager.create_role(role_data)


# GET /api/roles/{role_id} —— 获取单个角色详情，需要 role:manage 权限；不存在抛 404
@router.get("/{role_id}", response_model=Role)
async def get_role(
    role_id: str,
    _: None = Depends(require_permissions("role:manage")),
):
    """获取角色"""
    manager = RoleManager()
    role = await manager.get_role(role_id)
    if not role:
        raise HTTPException(status_code=404, detail="角色不存在")
    return role


# PUT /api/roles/{role_id} —— 更新角色（含权限点），需要 role:manage 权限
# 关键保护：若目标是系统角色且当前用户正属于该角色，则禁止修改（避免改动自己的权限，400）
# 角色不存在抛 404；权限点校验失败(ValidationError) 抛 400
@router.put("/{role_id}", response_model=Role)
async def update_role(
    role_id: str,
    role_data: RoleUpdate,
    current_user: TokenPayload = Depends(get_current_user_required),
    _: None = Depends(require_permissions("role:manage")),
):
    """更新角色"""
    manager = RoleManager()

    # 获取目标角色
    target_role = await manager.get_role(role_id)
    if not target_role:
        raise HTTPException(status_code=404, detail="角色不存在")

    # 如果是系统角色，检查当前用户是否拥有该角色
    if target_role.is_system:
        from src.infra.user.manager import UserManager

        user_manager = UserManager()
        user = await user_manager.get_user(current_user.sub)
        # 当前用户确实属于该系统角色时，拒绝修改（防止误删自己权限或越权提权）
        if user and user.roles and target_role.name in user.roles:
            raise HTTPException(
                status_code=400,
                detail="不能修改自己所属角色的权限",
            )

    try:
        role = await manager.update_role(role_id, role_data)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not role:
        raise HTTPException(status_code=404, detail="角色不存在")
    return role


# DELETE /api/roles/{role_id} —— 删除角色，需要 role:manage 权限
# 先取角色（用于缓存失效与 404 判断）；被占用/系统角色等约束不满足时抛 400（ValidationError）
@router.delete("/{role_id}")
async def delete_role(
    role_id: str,
    _: None = Depends(require_permissions("role:manage")),
):
    """删除角色"""
    manager = RoleManager()
    # 先获取角色名用于缓存失效
    target_role = await manager.get_role(role_id)
    if not target_role:
        raise HTTPException(status_code=404, detail="角色不存在")
    try:
        await manager.delete_role(role_id)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "deleted"}

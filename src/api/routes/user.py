"""
用户路由
"""

# 用户管理路由模块（挂载于 /api/users），面向管理员的用户账号管理
# 职责：用户的分页查询、创建、查看、更新、删除
# 权限点：user:read（查/列）、user:write（建/改）、user:delete（删）
# 注意：管理员创建的用户自动激活（跳过邮箱验证）；改到当前用户自身角色时会要求前端强制重登
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.responses import Response

from src.api.deps import require_permissions
from src.infra.user.manager import UserManager
from src.kernel.schemas.user import TokenPayload, User, UserCreate, UserListResponse, UserUpdate

router = APIRouter()


# GET /api/users/ —— 分页列出用户，需要 user:read 权限
# 查询参数：search 按用户名/关键词搜索，skip/limit 分页
@router.get("/", response_model=UserListResponse)
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    _: None = Depends(require_permissions("user:read")),
):
    """列出用户（分页）"""
    manager = UserManager()
    return await manager.list_users(skip, limit, search)


# POST /api/users/ —— 创建用户，需要 user:write 权限
# 管理员创建的用户强制 skip_verification=True，直接激活、无需邮箱验证
@router.post("/", response_model=User)
async def create_user(
    user_data: UserCreate,
    _: None = Depends(require_permissions("user:write")),
):
    """创建用户（管理员创建的用户自动激活，无需邮箱验证）"""
    manager = UserManager()
    # 管理员创建的用户跳过邮箱验证
    # 创建新的 UserCreate 对象，设置 skip_verification=True
    admin_user_data = user_data.model_copy(update={"skip_verification": True})
    return await manager.register(admin_user_data)


# GET /api/users/{user_id} —— 获取单个用户详情，需要 user:read 权限；不存在抛 404
@router.get("/{user_id}", response_model=User)
async def get_user(
    user_id: str,
    _: None = Depends(require_permissions("user:read")),
):
    """获取用户"""
    manager = UserManager()
    user = await manager.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


# PUT /api/users/{user_id} —— 更新用户，需要 user:write 权限；用户不存在抛 404
# 特殊处理：若改的是"当前登录用户自己"的角色(roles)，返回头 X-Force-Relogin=true 让前端强制重新登录
# （因为权限随角色变化，旧 token 需失效重取）
@router.put("/{user_id}", response_model=User)
async def update_user(
    user_id: str,
    user_data: UserUpdate,
    current_user: TokenPayload = Depends(require_permissions("user:write")),
):
    """更新用户"""
    manager = UserManager()
    user = await manager.update_user(user_id, user_data)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 如果修改了当前用户的角色，返回响应头让前端强制重新登录
    response = Response()
    if user_id == current_user.sub and user_data.roles is not None:
        response.headers["X-Force-Relogin"] = "true"
        # FastAPI 需要特殊处理来同时返回数据和自定义响应头
        from fastapi.responses import JSONResponse

        return JSONResponse(
            content=user.model_dump(mode="json"),
            headers={"X-Force-Relogin": "true"},
        )

    return user


# DELETE /api/users/{user_id} —— 删除用户，需要 user:delete 权限；不存在抛 404，成功返回 {"status": "deleted"}
@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    _: None = Depends(require_permissions("user:delete")),
):
    """删除用户"""
    manager = UserManager()
    success = await manager.delete_user(user_id)
    if not success:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"status": "deleted"}

"""Team CRUD routes."""

# 团队路由模块（挂载于 /api/teams）
# 职责：团队（多个 agent/角色的组合）的增删改查、收藏/置顶偏好、克隆
# 所有接口均需登录，且仅能操作 owner_user_id 为当前用户的团队；不存在统一抛 404（team_not_found）
# 路由用 redirect_slashes=False，并对 list/create 同时注册 "" 与 "/" 两个路径以兼容带/不带尾斜杠
from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_current_user_required
from src.infra.team.manager import TeamManager
from src.kernel.exceptions import NotFoundError
from src.kernel.schemas.team import (
    TeamCreate,
    TeamListResponse,
    TeamPreferenceUpdate,
    TeamResponse,
    TeamUpdate,
)
from src.kernel.schemas.user import TokenPayload

router = APIRouter(redirect_slashes=False)


# 工厂函数：创建团队管理器（业务与存储封装在 TeamManager），供各接口通过 Depends 注入
def _get_manager() -> TeamManager:
    return TeamManager()


# GET /api/teams（及 /）—— 分页列出当前用户拥有的团队，需登录
# 查询参数：favorite/pinned 过滤收藏或置顶，q 关键词、tag 标签过滤，skip/limit 分页
@router.get("", response_model=TeamListResponse)
@router.get("/", response_model=TeamListResponse)
async def list_teams(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    favorite: bool | None = None,
    pinned: bool | None = None,
    q: str | None = Query(None, max_length=100),
    tag: str | None = Query(None, max_length=80),
    user: TokenPayload = Depends(get_current_user_required),
    manager: TeamManager = Depends(_get_manager),
):
    return await manager.list_teams(
        owner_user_id=user.sub,
        skip=skip,
        limit=limit,
        favorite=favorite,
        pinned=pinned,
        q=q,
        tag=tag,
    )


# POST /api/teams（及 /）—— 创建团队，需登录，返回 201
# 请求体 TeamCreate；owner_user_id 绑定当前用户；校验失败（如引用了无权限的成员）抛 400
@router.post("", response_model=TeamResponse, status_code=201)
@router.post("/", response_model=TeamResponse, status_code=201)
async def create_team(
    body: TeamCreate,
    user: TokenPayload = Depends(get_current_user_required),
    manager: TeamManager = Depends(_get_manager),
):
    try:
        return await manager.create_team(body, owner_user_id=user.sub, user=user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# GET /api/teams/{team_id} —— 获取单个团队详情，需登录且仅限本人团队；不存在抛 404
@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: str,
    user: TokenPayload = Depends(get_current_user_required),
    manager: TeamManager = Depends(_get_manager),
):
    try:
        return await manager.get_team(team_id, owner_user_id=user.sub)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="team_not_found")


# PUT /api/teams/{team_id} —— 更新团队配置，需登录且仅限本人团队
# 请求体 TeamUpdate；不存在抛 404，校验失败抛 400
@router.put("/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: str,
    body: TeamUpdate,
    user: TokenPayload = Depends(get_current_user_required),
    manager: TeamManager = Depends(_get_manager),
):
    try:
        return await manager.update_team(team_id, body, owner_user_id=user.sub, user=user)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="team_not_found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# PATCH /api/teams/{team_id}/preference —— 更新当前用户对该团队的收藏/置顶偏好，需登录；不存在抛 404
@router.patch("/{team_id}/preference", response_model=TeamResponse)
async def update_team_preference(
    team_id: str,
    preference: TeamPreferenceUpdate,
    user: TokenPayload = Depends(get_current_user_required),
    manager: TeamManager = Depends(_get_manager),
):
    try:
        return await manager.update_preference(
            team_id,
            preference,
            owner_user_id=user.sub,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="team_not_found")


# DELETE /api/teams/{team_id} —— 删除团队，需登录且仅限本人团队，成功返回 204；不存在抛 404
@router.delete("/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    user: TokenPayload = Depends(get_current_user_required),
    manager: TeamManager = Depends(_get_manager),
):
    try:
        await manager.delete_team(team_id, owner_user_id=user.sub)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="team_not_found")


# POST /api/teams/{team_id}/clone —— 克隆一个团队为当前用户的新团队（副本），需登录，返回 201；源不存在抛 404
@router.post("/{team_id}/clone", response_model=TeamResponse, status_code=201)
async def clone_team(
    team_id: str,
    user: TokenPayload = Depends(get_current_user_required),
    manager: TeamManager = Depends(_get_manager),
):
    try:
        return await manager.clone_team(team_id, owner_user_id=user.sub)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="team_not_found")

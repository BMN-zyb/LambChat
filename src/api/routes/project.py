"""
项目路由

所有项目操作都需要认证，用户只能访问自己的项目。
"""

# 项目路由模块（挂载于 /api/projects）
# 职责：项目（会话分组）的增删改查；项目用于把会话归类，另有特殊的"收藏"项目由系统自动维护
# 所有接口均需登录且仅能操作本人项目；删除项目时会级联处理其下会话及相关引用
from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.deps import get_current_user_required
from src.infra.folder.storage import get_project_storage
from src.infra.session.manager import SessionManager
from src.infra.session.storage import SessionStorage
from src.kernel.schemas.project import Project, ProjectCreate, ProjectUpdate
from src.kernel.schemas.user import TokenPayload

router = APIRouter()


# 删除单个会话并清理其关联记录（与"单会话删除"走同一路径，保证 trace/文件/checkpoint 一并清理）
async def _delete_session_with_related_records(
    session_manager: SessionManager,
    session_id: str,
) -> bool:
    deleted = await session_manager.delete_session(session_id)
    # 会话删除失败则整体视为失败，交由调用方报错处理
    if not deleted:
        return False

    # 额外清理该会话延迟发现的工具缓存；失败不影响会话删除结果
    try:
        from src.infra.tool.deferred_manager import clear_discovered_tools

        await clear_discovered_tools(session_id)
    except Exception:
        pass

    return True


# GET /api/projects —— 列出当前用户的所有项目，需登录
# 会先确保"收藏"项目存在（没有则自动创建），再返回项目列表
@router.get("", response_model=list[Project])
async def list_projects(
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    列出所有项目

    自动确保收藏项目存在。
    """
    storage = get_project_storage()

    # Ensure favorites project exists
    await storage.ensure_favorites_project(user.sub)

    projects = await storage.list_projects(user.sub)
    return projects


# POST /api/projects —— 创建项目，需登录，返回 201
# 请求体 ProjectCreate；禁止手动创建 type="favorites" 的收藏项目（收藏项目由系统维护），否则 400
@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(
    project_data: ProjectCreate,
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    创建项目

    不允许创建 type="favorites" 的项目。
    """
    storage = get_project_storage()

    # Prevent creating favorites project manually
    if project_data.type == "favorites":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能创建收藏项目",
        )

    project = await storage.create(project_data, user.sub)
    return project


# PATCH /api/projects/{project_id} —— 更新项目（主要用于重命名），需登录且仅限本人项目
# 项目不存在 -> 404；存储层更新失败 -> 500
@router.patch("/{project_id}", response_model=Project)
async def update_project(
    project_id: str,
    project_data: ProjectUpdate,
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    更新项目（重命名）

    只能更新自己拥有的项目。
    """
    storage = get_project_storage()

    # Check if project exists and belongs to user
    project = await storage.get_by_id(project_id, user.sub)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="项目不存在",
        )

    updated_project = await storage.update(project_id, user.sub, project_data)
    if not updated_project:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="更新失败",
        )

    return updated_project


# DELETE /api/projects/{project_id} —— 删除项目，需登录且仅限本人项目
# 查询参数 delete_sessions：false 时把项目内会话移到"未分类"（清空其 project_id）；true 时连同会话一并删除
# 收藏项目不可删除（400）；此外还会清理揭示文件、渠道配置中对该项目的引用，避免残留悬空引用
@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    delete_sessions: bool = Query(False, description="是否同时删除项目内的所有会话"),
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    删除项目

    - 不能删除收藏项目
    - delete_sessions=false: 项目内的会话会被移动到未分类
    - delete_sessions=true: 同时删除项目内的所有会话
    """
    storage = get_project_storage()

    # Check if project exists and belongs to user
    project = await storage.get_by_id(project_id, user.sub)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="项目不存在",
        )

    # Prevent deleting favorites project
    if project.type == "favorites":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除收藏项目",
        )

    session_storage = SessionStorage()

    # 分支一：连同会话一起删除（逐个走完整删除流程，任一失败即 500）
    if delete_sessions:
        # Use the same path as single-session deletion so traces, files,
        # checkpoints, and related session data are cleaned up too.
        session_ids = await session_storage.list_ids_by_project(project_id, user.sub)
        session_manager = SessionManager()
        for session_id in session_ids:
            deleted = await _delete_session_with_related_records(session_manager, session_id)
            if not deleted:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="删除项目内会话失败",
                )
    else:
        # 分支二：仅解除关联——把这些会话的 project_id 清空，等价于移动到"未分类"
        # Clear project_id for all sessions in this project
        await session_storage.clear_project_id(project_id, user.sub)

    # 清理"揭示文件"(revealed file) 上对该项目的引用；失败仅告警、不阻断删除
    # Clear project_id on all revealed files belonging to this project
    try:
        from src.infra.revealed_file.storage import get_revealed_file_storage

        revealed_storage = get_revealed_file_storage()
        await revealed_storage.clear_project_id(project_id)
    except Exception as e:
        from src.infra.logging import get_logger

        get_logger(__name__).warning(f"Failed to clear revealed file project_id: {e}")

    # 清理渠道配置里对该项目的引用，避免以后由渠道自动创建的会话"复活"已删除的项目引用
    # Clear project_id on channel configs so future channel-created sessions
    # cannot resurrect a deleted project reference.
    try:
        from src.infra.channel.channel_storage import ChannelStorage

        channel_storage = ChannelStorage()
        await channel_storage.clear_project_id(project_id, user.sub)
    except Exception as e:
        from src.infra.logging import get_logger

        get_logger(__name__).warning(f"Failed to clear channel config project_id: {e}")

    # 最后删除项目记录本身；失败 -> 500
    # Delete the project
    success = await storage.delete(project_id, user.sub)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="删除失败",
        )

    return {"status": "deleted"}

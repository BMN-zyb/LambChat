"""
会话分享路由

允许用户分享会话，支持公开链接或需要登录访问。
"""

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.agents.core.base import get_agent_class
from src.api.deps import get_current_user_optional, get_current_user_required
from src.infra.logging import get_logger
from src.infra.session.dual_writer import get_dual_writer
from src.infra.session.manager import SessionManager
from src.infra.share.storage import ShareStorage
from src.infra.team.storage import TeamStorage
from src.infra.user.storage import UserStorage
from src.infra.utils.datetime import to_iso
from src.kernel.schemas.share import (
    ShareCreate,
    SharedContentOwner,
    SharedContentResponse,
    SharedSessionListItem,
    SharedSessionResponse,
    ShareListResponse,
    ShareType,
    ShareUpdate,
    ShareVisibility,
)
from src.kernel.schemas.user import TokenPayload
from src.kernel.types import Permission

# 会话分享路由（挂载于 /api/share）：创建/更新/列出/删除分享，以及公开访问分享内容。
# 公开访问接口按 visibility 决定是否需要登录（public 允许匿名访问，此时 user 为 None）
router = APIRouter()
logger = get_logger(__name__)

# 部分分享(PARTIAL)最多允许指定的 run_id 数量上限
SHARE_PARTIAL_RUN_IDS_LIMIT = 100


def _check_permission(user: TokenPayload, permission: str) -> bool:
    """检查用户是否拥有指定权限"""
    return permission in user.permissions


def _require_share_permission(user: TokenPayload) -> None:
    """要求用户拥有分享权限"""
    if not _check_permission(user, Permission.SESSION_SHARE.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="没有分享会话的权限",
        )


# 校验部分分享(PARTIAL)的 run_ids：非部分分享直接放行；
# 部分分享必须提供 run_ids，且数量不得超过上限，否则返回 400
def _validate_share_run_ids(share_data: ShareCreate | ShareUpdate) -> None:
    if share_data.share_type != ShareType.PARTIAL:
        return
    if not share_data.run_ids:
        raise HTTPException(
            status_code=400,
            detail="部分分享需要指定 run_ids",
        )
    if len(share_data.run_ids) > SHARE_PARTIAL_RUN_IDS_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"run_ids 数量不能超过 {SHARE_PARTIAL_RUN_IDS_LIMIT}",
        )


# 读取分享内容时对 run_ids 做上限截断，防止历史数据超量导致一次拉取过多事件
def _bounded_partial_run_ids(run_ids: list[str] | None) -> list[str] | None:
    if not run_ids:
        return None
    return run_ids[:SHARE_PARTIAL_RUN_IDS_LIMIT]


# 解析分享展示用的团队头像：优先团队头像，其次默认成员，
# 再次任一启用成员，最后取第一个成员的角色头像；都没有则返回 None
def _resolve_shared_team_avatar(team) -> str | None:
    if team.avatar:
        return team.avatar

    # 优先取团队指定的默认成员
    default_member = next(
        (member for member in team.members if member.member_id == team.default_member_id),
        None,
    )
    # 依次回退：默认成员 -> 任一启用成员 -> 第一个成员
    fallback_member = (
        default_member
        or next((member for member in team.members if member.enabled), None)
        or (team.members[0] if team.members else None)
    )
    return fallback_member.role_avatar if fallback_member else None


async def _attach_shared_team_metadata(
    session_info: dict,
    session,
    share,
) -> None:
    """Attach safe team display metadata for shared team sessions."""
    # 仅当会话是团队(agent_id=="team")且带 team_id 时才附加团队信息
    metadata = session.metadata or {}
    team_id = metadata.get("team_id") if session.agent_id == "team" else None
    if not team_id:
        return

    session_info["team_id"] = team_id
    # 加载团队名称与头像（只暴露安全的展示字段）；失败仅告警，不影响分享内容返回
    try:
        team = await TeamStorage().get_team(
            str(team_id),
            owner_user_id=session.user_id or share.owner_id,
        )
        if team:
            session_info["team_name"] = team.name
            team_avatar = _resolve_shared_team_avatar(team)
            if team_avatar:
                session_info["team_avatar"] = team_avatar
    except Exception:
        logger.warning("Failed to load shared team metadata", exc_info=True)


@router.post("", response_model=SharedSessionResponse)
async def create_share(
    share_data: ShareCreate,
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    创建会话分享

    需要 session:share 权限。
    """
    _require_share_permission(user)

    # 验证会话所有权
    manager = SessionManager()
    session = await manager.get_session(share_data.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 只能分享自己的会话
    if session.user_id != user.sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只能分享自己的会话",
        )

    # 部分分享需校验 run_ids 合法
    _validate_share_run_ids(share_data)

    # 创建分享
    storage = ShareStorage()
    shared_session = await storage.create(share_data, owner_id=user.sub)

    # 返回体带上前端可直接使用的公开访问路径 /shared/{share_id}
    return SharedSessionResponse(
        id=shared_session.id,
        share_id=shared_session.share_id,
        url=f"/shared/{shared_session.share_id}",
        session_id=shared_session.session_id,
        share_type=shared_session.share_type,
        visibility=shared_session.visibility,
        run_ids=shared_session.run_ids,
        created_at=shared_session.created_at,
    )


@router.get("", response_model=ShareListResponse)
async def list_shares(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    列出我创建的分享

    返回当前用户创建的所有分享记录。
    """
    storage = ShareStorage()
    # 分页获取当前用户创建的分享记录
    shares, total = await storage.list_by_owner(user.sub, skip=skip, limit=limit)

    # 获取会话名称（批量查询）
    # 去重收集涉及的会话 ID，一次性批量查出会话用于填充名称
    session_ids = list({share.session_id for share in shares})
    session_map = await SessionManager().get_sessions(session_ids) if session_ids else {}

    # 逐条组装列表项；会话可能已被删除，此时名称为 None
    result_shares = []
    for share in shares:
        session = session_map.get(share.session_id)
        result_shares.append(
            SharedSessionListItem(
                id=share.id,
                share_id=share.share_id,
                session_id=share.session_id,
                session_name=session.name if session else None,
                share_type=share.share_type,
                visibility=share.visibility,
                run_ids=share.run_ids,
                created_at=share.created_at,
            )
        )

    return ShareListResponse(shares=result_shares, total=total)


@router.patch("/{share_id}", response_model=SharedSessionResponse)
async def update_share(
    share_id: str,
    share_data: ShareUpdate,
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    更新已有会话分享

    保持公开链接不变，只更新分享范围与访问权限。
    """
    _require_share_permission(user)

    storage = ShareStorage()
    share = await storage.get_by_id(share_id)
    if not share:
        raise HTTPException(status_code=404, detail="分享不存在")

    # 只能编辑自己创建的分享
    if share.owner_id != user.sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只能编辑自己创建的分享",
        )

    manager = SessionManager()
    session = await manager.get_session(share.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    if session.user_id != user.sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只能分享自己的会话",
        )

    # 增量更新：未显式提供的字段沿用原分享的值
    next_share_type = share_data.share_type or share.share_type
    next_visibility = share_data.visibility or share.visibility
    next_run_ids = share_data.run_ids if share_data.run_ids is not None else share.run_ids
    normalized_update = ShareUpdate(
        share_type=next_share_type,
        run_ids=next_run_ids,
        visibility=next_visibility,
    )
    # 用合并后的最终值校验 run_ids 合法性
    _validate_share_run_ids(normalized_update)
    # 非部分分享则清空 run_ids（全量分享不需要指定轮次）
    if next_share_type != ShareType.PARTIAL:
        next_run_ids = None

    updated_share = await storage.update(
        share_id,
        owner_id=user.sub,
        share_type=next_share_type,
        run_ids=next_run_ids,
        visibility=next_visibility,
    )
    if not updated_share:
        raise HTTPException(status_code=500, detail="更新失败")

    return SharedSessionResponse(
        id=updated_share.id,
        share_id=updated_share.share_id,
        url=f"/shared/{updated_share.share_id}",
        session_id=updated_share.session_id,
        share_type=updated_share.share_type,
        visibility=updated_share.visibility,
        run_ids=updated_share.run_ids,
        created_at=updated_share.created_at,
    )


@router.get("/session/{session_id}", response_model=list[SharedSessionListItem])
async def list_session_shares(
    session_id: str,
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    列出指定会话的所有分享

    只有会话所有者可以查看。
    """
    # 验证会话所有权
    manager = SessionManager()
    session = await manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 只能查看自己会话的分享
    if session.user_id != user.sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只能查看自己会话的分享",
        )

    storage = ShareStorage()
    shares = await storage.list_by_session(session_id)

    # 添加会话名称
    result = []
    for share in shares:
        result.append(
            SharedSessionListItem(
                id=share.id,
                share_id=share.share_id,
                session_id=share.session_id,
                session_name=session.name,
                share_type=share.share_type,
                visibility=share.visibility,
                run_ids=share.run_ids,
                created_at=share.created_at,
            )
        )

    return result


@router.delete("/{share_id}")
async def delete_share(
    share_id: str,
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    删除分享

    只有分享所有者可以删除。
    """
    storage = ShareStorage()

    # 获取分享记录验证所有权
    share = await storage.get_by_id(share_id)
    if not share:
        raise HTTPException(status_code=404, detail="分享不存在")

    # 只能删除自己创建的分享
    if share.owner_id != user.sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只能删除自己创建的分享",
        )

    success = await storage.delete(share_id, user.sub)
    if not success:
        raise HTTPException(status_code=500, detail="删除失败")

    return {"status": "deleted"}


# ========================================
# 公开访问路由（无需认证或可选认证）
# ========================================


@router.get("/public/{share_id}", response_model=SharedContentResponse)
async def get_shared_content(
    share_id: str,
    event_limit: Annotated[int | None, Query(ge=1)] = None,
    user: Optional[TokenPayload] = Depends(get_current_user_optional),
):
    """
    查看分享的会话内容

    根据 visibility 决定是否需要登录：
    - public: 任何人都可以查看
    - authenticated: 需要登录才能查看
    """
    storage = ShareStorage()
    # 用对外公开的 share_id 查询分享记录（区别于内部主键 id）
    share = await storage.get_by_share_id(share_id)

    if not share:
        raise HTTPException(status_code=404, detail="分享不存在或已过期")

    # 检查访问权限
    # public 可匿名访问（user 允许为 None）；authenticated 则必须已登录
    if share.visibility == ShareVisibility.AUTHENTICATED:
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="需要登录才能查看此分享",
            )

    # 获取会话信息
    session_manager = SessionManager()
    session = await session_manager.get_session(share.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="原会话已不存在")

    # 获取会话事件
    dual_writer = get_dual_writer()

    # 部分分享：只取被分享的 run（并做数量上限截断）；全量分享则为 None
    partial_run_ids = (
        _bounded_partial_run_ids(share.run_ids) if share.share_type == ShareType.PARTIAL else None
    )
    # 只读取已完成的事件；带 limit 时多取一条用于探测是否被截断
    read_events_kwargs: dict[str, Any] = {"completed_only": True}
    if event_limit is not None:
        read_events_kwargs["max_events"] = event_limit + 1

    # 如果是部分分享，只获取指定 run 的事件
    if partial_run_ids:
        events = await dual_writer.read_session_events(
            share.session_id,
            run_ids=partial_run_ids,
            **read_events_kwargs,
        )
    else:
        events = await dual_writer.read_session_events(
            share.session_id,
            **read_events_kwargs,
        )
    # 实际数量超过 limit 则标记已截断并裁剪回 limit 条
    events_limited = event_limit is not None and len(events) > event_limit
    if events_limited and event_limit is not None:
        events = events[:event_limit]

    # 获取分享者信息
    # 展示分享者用户名与头像；用户不存在时回退为 Unknown
    user_storage = UserStorage()
    owner = await user_storage.get_by_id(share.owner_id)
    owner_info = SharedContentOwner(
        username=owner.username if owner else "Unknown",
        avatar_url=owner.avatar_url if owner else None,
    )

    # 构建会话信息（只返回安全的字段）
    # 尽量解析出更友好的 Agent 展示名，失败则回退为 agent_id
    agent_name = session.agent_id
    try:
        agent_cls = get_agent_class(session.agent_id)
        agent_name = agent_cls._agent_name
    except (ValueError, AttributeError):
        pass

    # 从会话元数据中取所用模型（可能不存在）
    model = (session.metadata or {}).get("agent_options", {}).get("model")

    # Extract persona info from session metadata (stored at top level)
    metadata = session.metadata or {}
    persona_preset_id = metadata.get("persona_preset_id")
    persona_preset_name = metadata.get("persona_preset_name")
    persona_avatar = metadata.get("persona_avatar")

    # 只挑选可安全公开的字段构建 session_info（裁剪掉敏感/内部数据）
    session_info = {
        "id": session.id,
        "name": session.name,
        "agent_id": session.agent_id,
        "agent_name": agent_name,
        "model": model,
        "created_at": to_iso(session.created_at),
        "updated_at": to_iso(session.updated_at),
        "task_status": session.task_status,
        "task_error": session.task_error,
        "completed_at": to_iso(session.completed_at),
    }

    # Add persona info if available
    if persona_preset_id:
        session_info["persona_preset_id"] = persona_preset_id
    if persona_preset_name:
        session_info["persona_preset_name"] = persona_preset_name
    if persona_avatar:
        session_info["persona_avatar"] = persona_avatar
    # 若为团队会话，附加安全的团队展示信息（名称/头像）
    await _attach_shared_team_metadata(session_info, session, share)

    # 组装公开返回体：会话信息、事件、分享者、分享类型与 run_ids、截断标记
    return SharedContentResponse(
        session=session_info,
        events=events,
        owner=owner_info,
        share_type=share.share_type,
        run_ids=partial_run_ids if share.share_type == ShareType.PARTIAL else share.run_ids,
        events_limited=events_limited,
        events_limit=event_limit,
    )

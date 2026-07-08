"""通知路由"""

# 站内通知路由模块（挂载于 /api/notifications）
# 职责：面向普通用户获取当前生效的通知、忽略(dismiss)通知；面向管理员进行通知的增删改查
# 权限：/active 与 /dismiss 仅需登录；/admin、创建、更新、删除均需 notification:manage 权限
from functools import lru_cache

from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.deps import get_current_user_required, require_permissions
from src.infra.notification.manager import NotificationManager
from src.kernel.schemas.notification import (
    Notification,
    NotificationCreate,
    NotificationListResponse,
    NotificationUpdate,
)
from src.kernel.schemas.user import TokenPayload

router = APIRouter()


# 用 lru_cache 把 NotificationManager 缓存为进程内单例（首次调用创建，后续复用同一实例）
@lru_cache
def get_notification_manager() -> NotificationManager:
    return NotificationManager()


# 应用关闭时调用：若单例已创建则关闭其底层连接，并清空 lru_cache 缓存
async def close_notification_manager() -> None:
    # currsize 为 0 表示从未创建过 manager，无需关闭
    if get_notification_manager.cache_info().currsize == 0:
        return
    try:
        await get_notification_manager().close()
    finally:
        get_notification_manager.cache_clear()


# GET /api/notifications/active —— 获取当前用户此刻应展示的生效通知列表，需登录
# 由 manager 按时间窗口/受众/是否已忽略等条件过滤，返回 Notification 列表
@router.get("/active")
async def get_active_notifications(
    user: TokenPayload = Depends(get_current_user_required),
    manager: NotificationManager = Depends(get_notification_manager),
) -> list[Notification]:
    return await manager.get_active_notifications(user.sub)


# GET /api/notifications/admin —— 管理端分页列出全部通知，需要 notification:manage 权限
# skip/limit 分页；返回 items + total
@router.get("/admin", response_model=NotificationListResponse)
async def list_notifications(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    _: None = Depends(require_permissions("notification:manage")),
    manager: NotificationManager = Depends(get_notification_manager),
) -> NotificationListResponse:
    items, total = await manager.list_notifications(skip=skip, limit=limit)
    return NotificationListResponse(items=items, total=total)


# POST /api/notifications/ —— 创建通知，需要 notification:manage 权限
# 请求体 NotificationCreate；记录创建者为当前用户(user.sub)
@router.post("/", response_model=Notification)
async def create_notification(
    data: NotificationCreate,
    user: TokenPayload = Depends(get_current_user_required),
    _: None = Depends(require_permissions("notification:manage")),
    manager: NotificationManager = Depends(get_notification_manager),
) -> Notification:
    return await manager.create(data, user.sub)


# PUT /api/notifications/{notification_id} —— 更新通知，需要 notification:manage 权限
# 非法 id 或不存在都返回 404（InvalidId/ValueError 被捕获后当作未找到处理）
@router.put("/{notification_id}", response_model=Notification)
async def update_notification(
    notification_id: str,
    data: NotificationUpdate,
    _: None = Depends(require_permissions("notification:manage")),
    manager: NotificationManager = Depends(get_notification_manager),
) -> Notification:
    try:
        result = await manager.update(notification_id, data)
    except (InvalidId, ValueError):
        result = None
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )
    return result


# DELETE /api/notifications/{notification_id} —— 删除通知，需要 notification:manage 权限；不存在抛 404
@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    _: None = Depends(require_permissions("notification:manage")),
    manager: NotificationManager = Depends(get_notification_manager),
) -> dict:
    success = await manager.delete(notification_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )
    return {"status": "deleted"}


# POST /api/notifications/{notification_id}/dismiss —— 当前用户忽略某条通知（之后不再对其展示），需登录
# 记录 (user, notification) 的已忽略状态，返回 {"status": "dismissed"}
@router.post("/{notification_id}/dismiss")
async def dismiss_notification(
    notification_id: str,
    user: TokenPayload = Depends(get_current_user_required),
    manager: NotificationManager = Depends(get_notification_manager),
) -> dict:
    await manager.dismiss(notification_id, user.sub)
    return {"status": "dismissed"}

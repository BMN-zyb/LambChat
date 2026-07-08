"""通知管理器"""

from __future__ import annotations

from typing import Optional

from src.infra.logging import get_logger
from src.infra.notification.storage import NotificationStorage
from src.kernel.schemas.notification import (
    Notification,
    NotificationCreate,
    NotificationUpdate,
)

logger = get_logger(__name__)


class NotificationManager:
    # 通知功能的业务门面：当前所有方法都是对 NotificationStorage 的薄封装，
    # 为上层（API 路由等）提供稳定的调用接口，后续如需加权限校验/缓存等逻辑可在此处扩展而不影响存储层。
    def __init__(self):
        self.storage = NotificationStorage()

    async def create(self, data: NotificationCreate, user_id: str) -> Notification:
        # 创建一条新通知，user_id 记录为创建者。
        return await self.storage.create(data, user_id)

    async def get_by_id(self, notification_id: str) -> Optional[Notification]:
        # 按主键查询单条通知，不存在则返回 None。
        return await self.storage.get_by_id(notification_id)

    async def list_notifications(
        self, skip: int = 0, limit: int = 50
    ) -> tuple[list[Notification], int]:
        # 后台管理场景下的分页列表查询，返回 (当前页数据, 总条数)。
        return await self.storage.list_notifications(skip=skip, limit=limit)

    async def update(
        self, notification_id: str, data: NotificationUpdate
    ) -> Optional[Notification]:
        # 局部更新通知内容/生效时间/启用状态等字段。
        return await self.storage.update(notification_id, data)

    async def delete(self, notification_id: str) -> bool:
        # 删除通知（连带清理该通知的忽略记录，具体逻辑见存储层）。
        return await self.storage.delete(notification_id)

    async def get_active_notifications(self, user_id: str, limit: int = 5) -> list[Notification]:
        # 供前端弹窗展示：获取当前生效且该用户尚未忽略过的通知列表。
        return await self.storage.get_active_notifications(user_id, limit=limit)

    async def dismiss(self, notification_id: str, user_id: str) -> bool:
        # 用户主动关闭/忽略某条通知，之后该通知不会再对该用户展示。
        return await self.storage.dismiss(notification_id, user_id)

    async def close(self) -> None:
        # 级联关闭底层存储的连接资源。
        await self.storage.close()

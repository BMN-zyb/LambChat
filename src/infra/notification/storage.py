"""通知存储层"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from bson import ObjectId

from src.infra.logging import get_logger
from src.infra.storage.mongodb import get_mongo_client
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.notification import (
    Notification,
    NotificationCreate,
    NotificationUpdate,
)

logger = get_logger(__name__)

# 单次查询最多返回的通知条数上限，防止调用方传入过大 limit 拖垮数据库
NOTIFICATION_LIST_LIMIT_MAX = 100


def _bounded_limit(limit: int) -> int:
    # 将传入的 limit 收敛到 [1, NOTIFICATION_LIST_LIMIT_MAX] 区间内，避免非法值
    return min(max(int(limit), 1), NOTIFICATION_LIST_LIMIT_MAX)


class NotificationStorage:
    """通知存储"""

    def __init__(self):
        # 延迟初始化的集合引用，首次访问对应 property 时才真正连接
        self._collection = None
        self._dismissal_collection = None

    @property
    def collection(self):
        # 通知主集合：存放通知的标题/内容/生效时间等信息，懒加载并缓存
        if self._collection is None:
            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db["notifications"]
        return self._collection

    @property
    def dismissal_collection(self):
        # 通知已读/已关闭记录集合：记录某用户对某条通知的"已忽略"状态
        if self._dismissal_collection is None:
            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._dismissal_collection = db["notification_dismissals"]
        return self._dismissal_collection

    async def create_indexes(self) -> None:
        # 按创建时间倒序，用于通知列表分页展示
        await self.collection.create_index([("created_at", -1)])
        # 按是否生效 + 创建时间倒序，用于快速筛选当前生效的通知
        await self.collection.create_index([("is_active", 1), ("created_at", -1)])
        # 同一用户对同一通知的忽略记录唯一，避免重复插入
        await self.dismissal_collection.create_index(
            [("notification_id", 1), ("user_id", 1)], unique=True
        )
        logger.info("Notification indexes created")

    async def create(self, data: NotificationCreate, user_id: str) -> Notification:
        # 创建一条新通知，创建时间与更新时间一致，记录创建者
        now = utc_now()
        doc = {
            "title_i18n": data.title_i18n.model_dump(),
            "content_i18n": data.content_i18n.model_dump(),
            "type": data.type.value if isinstance(data.type, Enum) else data.type,
            "start_time": data.start_time,
            "end_time": data.end_time,
            "is_active": data.is_active,
            "created_at": now,
            "updated_at": now,
            "created_by": user_id,
        }
        result = await self.collection.insert_one(doc)
        doc["id"] = str(result.inserted_id)
        return Notification.model_validate(doc)

    async def get_by_id(self, notification_id: str) -> Optional[Notification]:
        # 按主键查询单条通知，Mongo 的 _id 转成字符串形式的 id 字段返回
        try:
            doc = await self.collection.find_one({"_id": ObjectId(notification_id)})
            if doc:
                doc["id"] = str(doc.pop("_id"))
                return Notification.model_validate(doc)
            return None
        except Exception as e:
            # ObjectId 格式非法或查询异常时不抛出，统一返回 None
            logger.error(f"Error getting notification {notification_id}: {e}")
            return None

    async def list_notifications(
        self, skip: int = 0, limit: int = 50
    ) -> tuple[list[Notification], int]:
        # 分页获取通知列表（后台管理用），同时返回总数用于前端分页
        limit = _bounded_limit(limit)
        total = await self.collection.count_documents({})
        cursor = self.collection.find().sort("created_at", -1).skip(skip).limit(limit)
        items = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            items.append(Notification.model_validate(doc))
        return items, total

    async def update(
        self, notification_id: str, data: NotificationUpdate
    ) -> Optional[Notification]:
        # 局部更新通知：只更新调用方实际传入的字段，未传入字段保持原值不变
        try:
            update_fields: dict = {"updated_at": utc_now()}
            # model_fields_set 记录了 data 中被显式赋值过的字段名，用于区分
            # "未传"和"传了但是 None"两种语义，避免误将未修改字段清空
            provided = data.model_fields_set
            if "title_i18n" in provided and data.title_i18n is not None:
                update_fields["title_i18n"] = data.title_i18n.model_dump()
            if "content_i18n" in provided and data.content_i18n is not None:
                update_fields["content_i18n"] = data.content_i18n.model_dump()
            if "start_time" in provided:
                update_fields["start_time"] = data.start_time
            if "end_time" in provided:
                update_fields["end_time"] = data.end_time
            if "is_active" in provided:
                update_fields["is_active"] = data.is_active

            result = await self.collection.find_one_and_update(
                {"_id": ObjectId(notification_id)},
                {"$set": update_fields},
                return_document=True,
            )
            if result:
                result["id"] = str(result.pop("_id"))
                return Notification.model_validate(result)
            return None
        except Exception as e:
            logger.error(f"Error updating notification {notification_id}: {e}")
            return None

    async def delete(self, notification_id: str) -> bool:
        # 删除通知本身，同时联动清理该通知对应的所有"已忽略"记录，避免脏数据残留
        try:
            result = await self.collection.delete_one({"_id": ObjectId(notification_id)})
            if result.deleted_count > 0:
                await self.dismissal_collection.delete_many({"notification_id": notification_id})
                return True
            return False
        except Exception as e:
            logger.error(f"Error deleting notification {notification_id}: {e}")
            return False

    async def get_active_notifications(self, user_id: str, limit: int = 5) -> list[Notification]:
        """Get active notifications that the user hasn't dismissed, sorted by created_at desc."""
        # 供前端弹窗展示：只取当前生效、且当前用户尚未忽略过的通知
        now = utc_now()
        limit = _bounded_limit(limit)

        pipeline = [
            {
                "$match": {
                    # is_active 为开关字段，为 False 时视为已下线，直接排除
                    "is_active": True,
                    "$and": [
                        {
                            # start_time 未设置或已经过去，才算已经开始生效
                            "$or": [
                                {"start_time": {"$exists": False}},
                                {"start_time": None},
                                {"start_time": {"$lte": now}},
                            ]
                        },
                        {
                            # end_time 未设置或尚未到达，才算还没有过期
                            "$or": [
                                {"end_time": {"$exists": False}},
                                {"end_time": None},
                                {"end_time": {"$gte": now}},
                            ]
                        },
                    ],
                }
            },
            {"$sort": {"created_at": -1}},
            {
                # 关联查询 notification_dismissals，找出当前用户是否已忽略过该通知
                "$lookup": {
                    "from": "notification_dismissals",
                    "let": {"notification_id": {"$toString": "$_id"}},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$notification_id", "$$notification_id"]},
                                        {"$eq": ["$user_id", user_id]},
                                    ]
                                }
                            }
                        },
                        {"$limit": 1},
                    ],
                    "as": "dismissals",
                }
            },
            # 只保留 dismissals 关联结果为空的通知，即用户尚未忽略过的
            {"$match": {"dismissals": {"$eq": []}}},
            {"$limit": limit},
        ]

        cursor = self.collection.aggregate(pipeline)
        results = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            # dismissals 仅用于聚合过滤，不属于 Notification 模型字段，返回前需要剔除
            doc.pop("dismissals", None)
            results.append(Notification.model_validate(doc))
        return results

    async def dismiss(self, notification_id: str, user_id: str) -> bool:
        # 记录某用户忽略了某条通知；upsert=True 保证重复忽略也不会报唯一索引冲突
        try:
            await self.dismissal_collection.update_one(
                {"notification_id": notification_id, "user_id": user_id},
                {"$set": {"dismissed_at": utc_now()}},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Error dismissing notification: {e}")
            return False

    async def close(self) -> None:
        # 释放集合引用，下次访问 property 时会重新获取连接
        self._collection = None
        self._dismissal_collection = None

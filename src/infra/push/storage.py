"""Push 订阅存储层"""

from __future__ import annotations

from src.infra.logging import get_logger
from src.infra.storage.mongodb import get_mongo_client
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.push_subscription import PushSubscription

logger = get_logger(__name__)


class PushSubscriptionStorage:
    """Push 订阅存储"""

    def __init__(self):
        # 延迟初始化的 collection 缓存，首次访问 collection 属性时才真正连接。
        self._collection = None

    @property
    def collection(self):
        # 惰性获取 MongoDB 的 push_subscriptions 集合，避免在对象构造阶段就建立连接。
        if self._collection is None:
            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db["push_subscriptions"]
        return self._collection

    async def create_indexes(self) -> None:
        # user_id 索引：便于按用户批量查询/删除订阅。
        await self.collection.create_index([("user_id", 1)])
        # endpoint 唯一索引：同一浏览器订阅端点只能对应一条记录，用于后续 upsert 去重。
        await self.collection.create_index([("endpoint", 1)], unique=True)
        # created_at 索引：便于按创建时间做清理/统计。
        await self.collection.create_index([("created_at", 1)])
        logger.info("PushSubscription indexes created")

    async def create(self, user_id: str, data: dict, user_agent: str = "") -> PushSubscription:
        # 创建（或覆盖）一条 Push 订阅记录。
        # data 需包含浏览器 Push API 返回的 endpoint 与 keys（加密公钥等）。
        now = utc_now()
        doc = {
            "user_id": user_id,
            "endpoint": data["endpoint"],
            "keys": data["keys"],
            "user_agent": user_agent,
            "created_at": now,
        }
        # Upsert: if endpoint already exists under a different user, replace it
        await self.collection.update_one(
            {"endpoint": data["endpoint"]},
            {"$set": doc},
            upsert=True,
        )
        # 重新查询一次以取回完整文档（包含 Mongo 自动生成的 _id），再转换为返回模型。
        existing = await self.collection.find_one({"endpoint": data["endpoint"]})
        existing["id"] = str(existing.pop("_id"))
        return PushSubscription.model_validate(existing)

    async def delete_by_endpoint(self, endpoint: str) -> bool:
        # 按订阅端点删除单条记录，通常在浏览器主动取消订阅时调用。
        result = await self.collection.delete_one({"endpoint": endpoint})
        return result.deleted_count > 0

    async def delete_by_user(self, user_id: str) -> int:
        # 删除某用户的全部订阅记录（如账号注销、批量清理）。
        result = await self.collection.delete_many({"user_id": user_id})
        return result.deleted_count

    async def get_by_user(self, user_id: str) -> list[PushSubscription]:
        # 查询某用户名下的所有订阅（一个用户可能在多个设备/浏览器订阅）。
        cursor = self.collection.find({"user_id": user_id})
        items = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            items.append(PushSubscription.model_validate(doc))
        return items

    async def touch_last_used(self, endpoint: str) -> None:
        # 推送成功后更新 last_used_at，用于后续按活跃度清理失效订阅。
        await self.collection.update_one(
            {"endpoint": endpoint},
            {"$set": {"last_used_at": utc_now()}},
        )

    async def delete_expired(self, days: int = 90) -> int:
        # 清理长期未使用的订阅：优先看 last_used_at，若从未被使用过则回退看 created_at。
        from datetime import timedelta

        cutoff = utc_now() - timedelta(days=days)
        result = await self.collection.delete_many(
            {
                "$or": [
                    {"last_used_at": {"$lt": cutoff}},
                    {"last_used_at": None, "created_at": {"$lt": cutoff}},
                ]
            }
        )
        return result.deleted_count

    async def close(self) -> None:
        # 释放集合引用，供上层在应用关闭时统一清理资源。
        self._collection = None

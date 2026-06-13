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
        self._collection = None

    @property
    def collection(self):
        if self._collection is None:
            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db["push_subscriptions"]
        return self._collection

    async def create_indexes(self) -> None:
        await self.collection.create_index([("user_id", 1)])
        await self.collection.create_index([("endpoint", 1)], unique=True)
        await self.collection.create_index([("created_at", 1)])
        logger.info("PushSubscription indexes created")

    async def create(self, user_id: str, data: dict, user_agent: str = "") -> PushSubscription:
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
        existing = await self.collection.find_one({"endpoint": data["endpoint"]})
        existing["id"] = str(existing.pop("_id"))
        return PushSubscription.model_validate(existing)

    async def delete_by_endpoint(self, endpoint: str) -> bool:
        result = await self.collection.delete_one({"endpoint": endpoint})
        return result.deleted_count > 0

    async def delete_by_user(self, user_id: str) -> int:
        result = await self.collection.delete_many({"user_id": user_id})
        return result.deleted_count

    async def get_by_user(self, user_id: str) -> list[PushSubscription]:
        cursor = self.collection.find({"user_id": user_id})
        items = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            items.append(PushSubscription.model_validate(doc))
        return items

    async def touch_last_used(self, endpoint: str) -> None:
        await self.collection.update_one(
            {"endpoint": endpoint},
            {"$set": {"last_used_at": utc_now()}},
        )

    async def delete_expired(self, days: int = 90) -> int:
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
        self._collection = None

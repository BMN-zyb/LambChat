"""
MongoDB 存储实现
"""

# ---------------------------------------------------------------------------
# 模块说明：MongoDB 存储地基 + 审批(HITL)存储 + 分布式审批通知
#
# 本模块是数据层最底层的地基之一，包含三部分：
#   1. MongoDB 客户端工厂（get_mongo_client / close_mongo_client）：
#      难点——用 @lru_cache 实现「进程级单例」，全进程共享一个连接池（Motor
#      异步客户端），避免重复建连；按是否配置账号密码拼接连接串，凭据经
#      quote_plus URL 编码，并区分 mongodb:// 与 mongodb+srv:// 两种协议。
#   2. MongoDBStorage：实现 StorageBase 的通用键值存储（以 key 作 _id、存 value），
#      keys() 把 fnmatch 通配符转正则并限量返回，避免误扫全库。
#   3. 审批存储（Human-in-the-Loop）：ApprovalStorage 用 MongoDB 存审批请求，
#      配合 TTL 索引自动过期；respond_if_pending 用 find_one_and_update 做原子应答
#      防竞态；wait_for_response_distributed 采用「Redis Pub/Sub 唤醒 + Mongo 轮询
#      兜底」的双保险跨实例等待，Pub/Sub 只发轻量唤醒信号，响应主体仍以 Mongo 为准。
# ---------------------------------------------------------------------------

import asyncio
import fnmatch
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import TYPE_CHECKING, Any, List, Optional

from pydantic import BaseModel
from pymongo import ReturnDocument

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.pubsub_hub import get_pubsub_hub
from src.infra.storage.base import StorageBase
from src.infra.storage.redis import get_redis_client
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

logger = get_logger(__name__)
# keys() 模式匹配时的返回上限，避免误扫全库
MONGODB_STORAGE_KEYS_LIMIT = 1000
# 审批响应的 Redis Pub/Sub 频道名，用于跨实例唤醒等待者
APPROVAL_RESPONSE_CHANNEL = "approval:response"


@lru_cache
def get_mongo_client() -> "AsyncIOMotorClient":
    """获取 MongoDB 客户端（单例）- 使用 Motor 异步客户端"""
    # lru_cache 确保整个进程共享一个连接池，避免重复建连
    try:
        from urllib.parse import quote_plus

        from motor.motor_asyncio import AsyncIOMotorClient

        base_url = settings.MONGODB_URL
        username = settings.MONGODB_USERNAME
        password = settings.MONGODB_PASSWORD
        auth_source = settings.MONGODB_AUTH_SOURCE

        # 配置了账号密码时，把凭据 URL 编码后拼进连接串（区分 mongodb:// 与 mongodb+srv://）
        if username and password:
            if base_url.startswith("mongodb://"):
                rest = base_url[len("mongodb://") :]
                encoded_user = quote_plus(username)
                encoded_pass = quote_plus(password)
                connection_string = (
                    f"mongodb://{encoded_user}:{encoded_pass}@{rest}?authSource={auth_source}"
                )
            elif base_url.startswith("mongodb+srv://"):
                rest = base_url[len("mongodb+srv://") :]
                encoded_user = quote_plus(username)
                encoded_pass = quote_plus(password)
                connection_string = (
                    f"mongodb+srv://{encoded_user}:{encoded_pass}@{rest}?authSource={auth_source}"
                )
            else:
                connection_string = base_url
        else:
            # 未配置凭据则直接使用原始 URL
            connection_string = base_url

        # 连接池与超时参数：控制并发连接上限与选主/连接超时，tz_aware 让时间统一为 UTC
        client: AsyncIOMotorClient = AsyncIOMotorClient(
            connection_string,
            maxPoolSize=20,
            minPoolSize=2,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=10000,
            tz_aware=True,
            tzinfo=timezone.utc,
        )
        return client
    except ImportError:
        raise ImportError("请安装 motor: pip install motor")


async def close_mongo_client() -> None:
    """关闭 MongoDB 连接池"""
    try:
        # 缓存为空说明从未创建过客户端，无需关闭
        if get_mongo_client.cache_info().currsize == 0:
            return
        client = get_mongo_client()
        client.close()
        # 清空 lru_cache，使下次调用重新建连
        get_mongo_client.cache_clear()
        logger.info("MongoDB client closed")
    except Exception as e:
        logger.warning(f"Error closing MongoDB client: {e}")


# 通用键值存储的 MongoDB 实现：一个 collection 即一个键值空间，
# 以文档 _id 存 key、value 字段存值，实现 StorageBase 定义的接口
class MongoDBStorage(StorageBase):
    """
    MongoDB 存储实现
    """

    def __init__(self, collection_name: str = "storage"):
        self.collection_name = collection_name
        # 延迟加载的集合句柄
        self._collection: "AsyncIOMotorCollection[Any] | None" = None

    @property
    def collection(self):
        """延迟加载集合"""
        # 首次访问时才建连并绑定集合
        if self._collection is None:
            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db[self.collection_name]
        return self._collection

    async def get(self, key: str) -> Optional[Any]:
        """获取数据"""
        # 以 key 作为文档 _id，读取其 value 字段
        result = await self.collection.find_one({"_id": key})
        if result:
            return result.get("value")
        return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """设置数据"""
        # upsert 写入 {_id, value}；带 ttl 时附加 expires_at 供 TTL 索引清理
        doc = {"_id": key, "value": value}
        if ttl:
            from datetime import timedelta

            doc["expires_at"] = utc_now() + timedelta(seconds=ttl)
        await self.collection.update_one(
            {"_id": key},
            {"$set": doc},
            upsert=True,
        )

    async def delete(self, key: str) -> bool:
        """删除数据"""
        result = await self.collection.delete_one({"_id": key})
        return result.deleted_count > 0

    async def exists(self, key: str) -> bool:
        """检查键是否存在"""
        result = await self.collection.find_one({"_id": key})
        return result is not None

    async def keys(self, pattern: str) -> list[str]:
        """获取匹配的键列表，默认限制数量避免误扫全库。"""
        # 把 fnmatch 通配符转成正则匹配 _id，并限制返回条数
        regex = fnmatch.translate(pattern)
        cursor = self.collection.find({"_id": {"$regex": regex}}, {"_id": 1}).limit(
            MONGODB_STORAGE_KEYS_LIMIT
        )
        return [doc["_id"] async for doc in cursor]


# ============================================================================
# 审批存储 (Human-in-the-Loop)
# ============================================================================

# 默认过期时间 (秒)
APPROVAL_TTL = 3600  # 1 hour


class PendingApproval(BaseModel):
    """待处理的审批请求"""

    id: str
    message: str
    type: str = "form"  # form-based approval
    fields: List[dict] = []  # 表单字段列表
    status: str = "pending"
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    extensions: int = 0
    metadata: Optional[dict] = None  # structured data for specialized rendering


class ApprovalResponse(BaseModel):
    """审批响应"""

    approved: bool
    response: dict = {}  # 改为 dict 类型


# 审批（HITL）存储：把待人工确认的审批请求存入独立集合，支持分布式部署；
# 靠 TTL 索引自动清理过期请求，靠原子更新避免并发重复应答
class ApprovalStorage:
    """
    审批存储类

    使用 MongoDB 存储审批数据，支持分布式部署。
    """

    def __init__(self, collection_name: str = "approvals"):
        self.collection_name = collection_name
        self._collection: "AsyncIOMotorCollection[Any] | None" = None
        self._indexes_created = False

    @property
    def collection(self):
        """延迟加载 MongoDB 集合"""
        if self._collection is None:
            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db[self.collection_name]
        return self._collection

    async def ensure_indexes(self) -> None:
        """确保索引存在（幂等，可重复调用）"""
        # 用实例标志保证进程内只建一次索引
        if self._indexes_created:
            return
        coll = self.collection
        # _id already has a unique index by default, no need to recreate

        # 按状态+过期+用户 的查询索引（列出某用户待处理审批）
        await coll.create_index(
            [("status", 1), ("expires_at", 1), ("user_id", 1)],
            background=True,
        )
        # 按会话+状态 的查询索引
        await coll.create_index(
            [("session_id", 1), ("status", 1)],
            background=True,
        )
        # TTL index: MongoDB 自动删除过期文档（兜底清理）
        # expireAfterSeconds=0 表示到 expires_at 即过期
        await coll.create_index(
            "expires_at",
            expireAfterSeconds=0,
            background=True,
        )
        self._indexes_created = True

    async def create(self, approval: PendingApproval, ttl: int = APPROVAL_TTL) -> PendingApproval:
        """创建审批记录"""
        await self.ensure_indexes()
        now = utc_now()
        # 以审批 id 作为 _id，并按 ttl 计算过期时间
        doc = approval.model_dump()
        doc["_id"] = approval.id
        doc["created_at"] = now
        doc["expires_at"] = now + timedelta(seconds=ttl)

        await self.collection.insert_one(doc)
        return approval

    async def get(self, approval_id: str) -> Optional[PendingApproval]:
        """获取审批记录（排除 response 子文档，减少传输量）"""
        doc = await self.collection.find_one(
            {"_id": approval_id, "expires_at": {"$gt": utc_now()}},
            {"response": 0},
        )
        if not doc:
            return None
        doc.pop("_id", None)
        return PendingApproval(**doc)

    async def update_status(
        self,
        approval_id: str,
        status: str,
        response: Optional[ApprovalResponse] = None,
    ) -> bool:
        """更新审批状态"""
        update_doc = {"status": status, "updated_at": utc_now()}
        if response:
            update_doc["response"] = response.model_dump()

        result = await self.collection.update_one({"_id": approval_id}, {"$set": update_doc})
        return result.modified_count > 0

    async def respond_if_pending(
        self,
        approval_id: str,
        status: str,
        response: ApprovalResponse,
    ) -> Optional[PendingApproval]:
        """Atomically mark a pending approval as responded."""
        # 原子应答：仅当审批仍为 pending 且未过期时才写入响应，避免重复/竞态应答
        update_doc = {
            "status": status,
            "updated_at": utc_now(),
            "response": response.model_dump(),
        }
        doc = await self.collection.find_one_and_update(
            {
                "_id": approval_id,
                "status": "pending",
                "expires_at": {"$gt": utc_now()},
            },
            {"$set": update_doc},
            projection={"response": 0},
            return_document=ReturnDocument.AFTER,
        )
        if not doc:
            return None
        doc.pop("_id", None)
        return PendingApproval(**doc)

    async def extend_expires_at(
        self,
        approval_id: str,
        extra_seconds: int = 60,
        max_extensions: int = 10,
        max_total_seconds: int = 3600,
    ) -> Optional[datetime]:
        """
        延长审批过期时间（支持分布式）

        Args:
            approval_id: 审批 ID
            extra_seconds: 每次延长的秒数
            max_extensions: 最大延长次数
            max_total_seconds: 从创建时间起最大总有效时长

        Returns:
            新的 expires_at，或 None（已达上限）
        """
        doc = await self.collection.find_one({"_id": approval_id})
        if not doc:
            return None

        extensions = doc.get("extensions", 0)
        created_at = doc.get("created_at", utc_now())
        current_expires = doc.get("expires_at", utc_now())

        # 超过最大延长次数则拒绝
        if extensions >= max_extensions:
            return None

        # 新过期时间不得超过"创建时间 + 最大总时长"这一硬顶
        max_expires = created_at + timedelta(seconds=max_total_seconds)
        new_expires = min(current_expires + timedelta(seconds=extra_seconds), max_expires)

        # 已无法再前推则返回 None
        if new_expires <= current_expires:
            return None

        await self.collection.update_one(
            {"_id": approval_id},
            {"$set": {"expires_at": new_expires, "extensions": extensions + 1}},
        )
        return new_expires

    async def delete(self, approval_id: str) -> bool:
        """删除审批记录"""
        result = await self.collection.delete_one({"_id": approval_id})
        return result.deleted_count > 0

    async def list_pending(
        self,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[PendingApproval]:
        """获取待处理审批列表"""
        query = {"status": "pending", "expires_at": {"$gt": utc_now()}}
        if session_id:
            query["session_id"] = session_id
        if user_id:
            query["user_id"] = user_id

        bounded_limit = max(1, min(int(limit), 100))
        cursor = self.collection.find(query).sort("created_at", -1).limit(bounded_limit)
        approvals = []
        async for doc in cursor:
            doc.pop("_id", None)
            approvals.append(PendingApproval(**doc))
        return approvals

    async def has_response(self, approval_id: str) -> bool:
        """检查是否已有响应（轻量投影，用于轮询）"""
        doc = await self.collection.find_one(
            {"_id": approval_id, "response": {"$exists": True}},
            {"response": 1},
        )
        return doc is not None

    async def get_response(self, approval_id: str) -> Optional[ApprovalResponse]:
        """获取审批响应（仅返回 response 子文档）"""
        doc = await self.collection.find_one(
            {"_id": approval_id},
            {"response": 1},
        )
        if not doc or "response" not in doc:
            return None
        response_data = doc["response"]
        if not isinstance(response_data, dict):
            return None
        return ApprovalResponse(**response_data)


@lru_cache
def get_approval_storage() -> ApprovalStorage:
    """获取审批存储实例（单例）"""
    # lru_cache 实现进程级单例
    return ApprovalStorage()


def close_approval_storage() -> None:
    """Release cached approval storage without creating it during shutdown."""
    # 清缓存释放单例，供关停时调用
    get_approval_storage.cache_clear()


# ============================================================================
# 分布式通知 (Redis Pub/Sub + MongoDB 轮询兜底)
# ============================================================================


async def notify_approval_response(approval_id: str, response: ApprovalResponse) -> None:
    """
    通知等待的 Agent 审批已响应

    响应主体仍以 MongoDB 为准；Redis Pub/Sub 只负责唤醒跨实例等待者。
    """
    # 只发一个轻量唤醒信号（含 approval_id），真正的响应数据从 Mongo 读取
    try:
        redis_client = get_redis_client()
        payload = await run_blocking_io(json.dumps, {"approval_id": approval_id})
        await redis_client.publish(
            APPROVAL_RESPONSE_CHANNEL,
            payload,
        )
    except Exception as e:
        # 发布失败不致命：等待方还有 Mongo 轮询兜底
        logger.warning("Failed to publish approval response notification: %s", e)


async def wait_for_response_distributed(
    approval_id: str,
    timeout: float = 300,
) -> Optional[ApprovalResponse]:
    """
    等待审批响应 (Redis Pub/Sub 唤醒 + MongoDB 轮询兜底)

    Args:
        approval_id: 审批 ID
        timeout: 超时时间（秒）

    Returns:
        ApprovalResponse 或 None (超时)
    """
    # 双保险等待：Pub/Sub 收到通知立即唤醒；同时用逐步退避的轮询兜底防丢消息
    storage = get_approval_storage()
    start_time = asyncio.get_event_loop().time()
    min_interval = 0.5
    max_interval = 3.0
    current_interval = min_interval
    notification_event = asyncio.Event()
    subscription_token: str | None = None
    hub = None

    # Pub/Sub 回调：命中本 approval_id 时置位事件唤醒等待循环
    async def _handle_notification(message: dict[str, Any]) -> None:
        try:
            data = await run_blocking_io(json.loads, message.get("data") or "{}")
            if data.get("approval_id") == approval_id:
                notification_event.set()
        except json.JSONDecodeError:
            logger.warning("Invalid approval response notification: %s", message.get("data"))

    # 订阅通知频道；订阅失败则仅依赖轮询兜底
    try:
        hub = get_pubsub_hub()
        subscription_token = hub.subscribe(APPROVAL_RESPONSE_CHANNEL, _handle_notification)
        await hub.start()
    except Exception as e:
        logger.warning("Failed to subscribe approval response notifications: %s", e)
        hub = None
        subscription_token = None

    try:
        while True:
            # 超时退出
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                return None

            # 轻量检查：只查 response 字段是否存在
            if await storage.has_response(approval_id):
                return await storage.get_response(approval_id)

            # 本次等待时长不超过剩余超时
            remaining = timeout - elapsed
            wait_interval = min(current_interval, remaining)
            if wait_interval <= 0:
                return None

            try:
                # 等待唤醒信号或超时；超时则指数退避加大轮询间隔后继续
                await asyncio.wait_for(notification_event.wait(), timeout=wait_interval)
            except asyncio.TimeoutError:
                current_interval = min(current_interval * 1.5, max_interval)
                continue

            # 被唤醒：清位并读取响应
            notification_event.clear()
            response = await storage.get_response(approval_id)
            if response:
                return response
    finally:
        # 退出前退订并在无人使用时停止 hub
        if hub is not None and subscription_token is not None:
            hub.unsubscribe(subscription_token)
            await hub.stop_if_idle()

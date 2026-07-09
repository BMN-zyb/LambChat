"""File record storage for content-hash based deduplication."""

# ---------------------------------------------------------------------------
# 模块说明：文件记录存储（基于内容哈希的去重 + 引用计数）
#
# 本模块把上传文件的元信息存入 MongoDB 的 file_records 集合，核心是「内容哈希去重」：
#   - 以文件内容的 SHA-256 作为 hash 唯一键——内容相同的文件只存一份底层对象，
#     多处上传/引用共用同一条记录，从而节省存储；
#   - reference_count 记录该对象被多少条「已持久化的消息」引用：消息落库时
#     add_references +1，消息删除时 release_references -1（且不会减成负数）；
#     引用归零后底层对象即可被安全清理。
# 其余要点：集合与索引均惰性初始化（ensure_indexes_if_needed 以 fire-and-forget
# 后台任务只建一次索引，不阻塞请求）；hash 与 key 双唯一索引分别支撑去重与按 key 定位；
# 批量增减引用前先用 _bounded_unique_keys 清洗去重并限量，防止异常输入放大更新范围。
# ---------------------------------------------------------------------------

import asyncio
from typing import Optional

from src.infra.logging import get_logger
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings

# 单次「引用增减」操作允许处理的最大 key 数,防止异常输入导致一次更新过多文档。
REFERENCE_KEYS_MAX = 100


def _bounded_unique_keys(keys: list[str], *, limit: int = REFERENCE_KEYS_MAX) -> list[str]:
    # 清洗并去重 key 列表:去空白、丢弃空串与重复项,且最多保留 limit 个(有界)。
    unique_keys: list[str] = []
    seen = set()
    for key in keys:
        clean = str(key).strip() if key else ""
        if not clean or clean in seen:
            continue
        seen.add(clean)
        unique_keys.append(clean)
        if len(unique_keys) >= limit:
            break
    return unique_keys


# 文件记录存储：延迟持有 file_records 集合与「后台建索引」任务，
# 对外提供按 hash/key 查找、创建、引用计数增减与删除
class FileRecordStorage:
    """Storage layer for file records, keyed by content hash."""

    # 以内容哈希(hash)去重:相同内容只存一份对象,file_records 文档用 reference_count
    # 记录被多少条已持久化消息引用,引用归零后可安全清理底层对象。

    REFERENCE_KEYS_MAX = REFERENCE_KEYS_MAX

    def __init__(self):
        # MongoDB 集合(惰性加载)与「后台建索引」任务句柄。
        self._collection = None
        self._indexes_task: asyncio.Task[None] | None = None

    @property
    def collection(self):
        """Lazy-load MongoDB collection."""
        # 首次访问时才连接 Mongo 并取集合,避免模块导入期建立连接。
        if self._collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db["file_records"]
        return self._collection

    async def ensure_indexes_if_needed(self):
        """Ensure indexes exist (called lazily on first use)."""
        # 首次使用时「触发一次」后台建索引:用 _indexes_ensured 标志保证只触发一次,
        # 并以 fire-and-forget 任务执行(不阻塞当前请求);done 回调读取 exception 以免"未取回异常"告警。
        if not hasattr(self, "_indexes_ensured"):
            self._indexes_ensured = True
            task = asyncio.create_task(self._ensure_indexes())
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            self._indexes_task = task

    async def _ensure_indexes(self):
        """Create required indexes on the file_records collection."""
        # 建索引:hash/key 唯一(保证去重与按 key 定位),uploaded_by 普通索引(按上传者查询)。
        # background=True 后台建索引;失败仅告警,不影响主流程。
        try:
            collection = self.collection
            await collection.create_index("hash", unique=True, background=True)
            await collection.create_index("key", unique=True, background=True)
            await collection.create_index("uploaded_by", background=True)
        except Exception as e:
            get_logger(__name__).warning(f"Failed to create file_records indexes: {e}")

    async def find_by_hash(self, file_hash: str) -> Optional[dict]:
        """Look up a file record by content hash.

        Args:
            file_hash: SHA-256 hex digest.

        Returns:
            Document dict with ``id`` (instead of ``_id``), or None.
        """
        await self.ensure_indexes_if_needed()
        doc = await self.collection.find_one({"hash": file_hash})
        if doc:
            # 把 Mongo 的 _id(ObjectId)转成字符串 id,便于对外(JSON)使用。
            doc["id"] = str(doc.pop("_id"))
        return doc

    async def find_by_key(self, key: str) -> Optional[dict]:
        """Look up a file record by storage key.

        Args:
            key: Storage object key (e.g. "category/user_id/uuid.ext").

        Returns:
            Document dict with ``id`` (instead of ``_id``), or None.
        """
        await self.ensure_indexes_if_needed()
        doc = await self.collection.find_one({"key": key})
        if doc:
            doc["id"] = str(doc.pop("_id"))
        return doc

    async def create(
        self,
        file_hash: str,
        key: str,
        name: str,
        mime_type: str,
        size: int,
        category: str,
        uploaded_by: str,
    ) -> dict:
        """Insert a new file record.

        Args:
            file_hash: SHA-256 hex digest.
            key: Storage object key (e.g. "user_id/abc123hash").
            name: Original filename.
            mime_type: MIME type of the file.
            size: File size in bytes.
            category: One of "image", "video", "audio", "document".
            uploaded_by: User ID of the uploader.

        Returns:
            Document dict with ``id`` field.
        """
        await self.ensure_indexes_if_needed()
        now = utc_now()
        # 新记录初始 reference_count=0;引用计数在消息真正持久化时才通过 add_references 增加。
        doc = {
            "hash": file_hash,
            "key": key,
            "name": name,
            "mime_type": mime_type,
            "size": size,
            "category": category,
            "uploaded_by": uploaded_by,
            "reference_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        result = await self.collection.insert_one(doc)
        doc["id"] = str(result.inserted_id)
        return doc

    async def add_references(self, keys: list[str]) -> int:
        """Increment persisted message references for the given storage keys."""
        # 批量给这些 key 的引用计数 +1(先清洗去重);返回实际被修改的文档数。
        unique_keys = _bounded_unique_keys(keys)
        if not unique_keys:
            return 0

        await self.ensure_indexes_if_needed()
        result = await self.collection.update_many(
            {"key": {"$in": unique_keys}},
            {"$inc": {"reference_count": 1}, "$set": {"updated_at": utc_now()}},
        )
        return result.modified_count

    async def release_references(self, keys: list[str]) -> int:
        """Decrement persisted message references for the given storage keys."""
        # 批量 -1,但仅对 reference_count>0 的文档生效,避免计数被减成负数。
        unique_keys = _bounded_unique_keys(keys)
        if not unique_keys:
            return 0

        await self.ensure_indexes_if_needed()
        result = await self.collection.update_many(
            {
                "key": {"$in": unique_keys},
                "reference_count": {"$gt": 0},
            },
            {"$inc": {"reference_count": -1}, "$set": {"updated_at": utc_now()}},
        )
        return result.modified_count

    async def delete_by_key(self, key: str) -> bool:
        """Delete a file record by storage key.

        Args:
            key: Storage object key.

        Returns:
            True if a document was deleted, False otherwise.
        """
        await self.ensure_indexes_if_needed()
        result = await self.collection.delete_one({"key": key})
        return result.deleted_count > 0

    async def delete_by_hash(self, file_hash: str) -> bool:
        """Delete a file record by content hash.

        Args:
            file_hash: SHA-256 hex digest.

        Returns:
            True if a document was deleted, False otherwise.
        """
        await self.ensure_indexes_if_needed()
        result = await self.collection.delete_one({"hash": file_hash})
        return result.deleted_count > 0

    async def close(self) -> None:
        # 收尾:取消后台建索引任务、清除「已建索引」标志与集合引用,便于重置/释放。
        task = self._indexes_task
        self._indexes_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if hasattr(self, "_indexes_ensured"):
            delattr(self, "_indexes_ensured")
        self._collection = None

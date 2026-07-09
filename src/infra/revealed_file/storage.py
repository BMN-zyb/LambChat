"""Revealed file index storage — tracks all files/projects revealed via agent tools."""

import asyncio
import re
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

from src.infra.logging import get_logger
from src.infra.utils.datetime import to_iso, utc_now
from src.kernel.config import settings

logger = get_logger(__name__)
# 单页最多返回的记录条数上限。
REVEALED_FILE_PAGE_LIMIT_MAX = 50
# 按会话分组展示时，每个会话预览最多附带的文件条数上限（避免单个"热"会话把整页响应撑爆）。
REVEALED_FILE_GROUPED_FILES_PER_SESSION_MAX = 10
# 用户会话列表（get_user_sessions）最多返回的会话条数上限。
REVEALED_FILE_SESSION_LIST_LIMIT = 100


# 把用户输入转义后再用作 MongoDB $regex 模式，防止用户输入里的正则元字符被解释执行（避免 ReDoS / 正则注入）。
def _safe_search_pattern(text: str) -> str:
    """Escape user input for use as MongoDB $regex pattern to prevent ReDoS."""
    return re.escape(text)


def _bounded_page_limit(limit: int) -> int:
    # 收敛分页大小到 [1, REVEALED_FILE_PAGE_LIMIT_MAX] 区间，防止外部传入非法或过大的值。
    return min(max(int(limit), 1), REVEALED_FILE_PAGE_LIMIT_MAX)


def _normalize_dedupe_path(path: str) -> str:
    # 统一路径分隔符为 "/"，折叠连续的重复斜杠，并去掉末尾斜杠，得到用于去重比较的规范化路径。
    normalized = path.strip().replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized.rstrip("/") or normalized


def _normalize_dedupe_url(url: str) -> str:
    # URL 去重规范化：协议与域名统一小写，路径做百分号解码并去掉末尾斜杠，
    # 使得同一资源的不同写法（大小写、编码差异）能被视为同一条记录。
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = unquote(parsed.path).rstrip("/") or "/"
    return f"{scheme}://{netloc}{path}"


def _build_dedupe_key(file_key: str, source: str, data: Dict[str, Any]) -> str:
    # 计算"去重键"：优先使用调用方传入的 original_path（区分是远程 URL 还是本地路径分别规范化）；
    # 若没有 original_path，则退化为用 file_key 本身判断（同样先看是否为 URL）；
    # 最后兜底为 "source:file_key" 组合，保证老数据也能生成一个可用的去重键。
    original_path = data.get("original_path")
    if isinstance(original_path, str) and original_path.strip():
        parsed = urlparse(original_path.strip())
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return f"url:{_normalize_dedupe_url(original_path)}"
        return f"path:{_normalize_dedupe_path(original_path)}"

    parsed_key = urlparse(file_key.strip())
    if parsed_key.scheme in {"http", "https"} and parsed_key.netloc:
        return f"url:{_normalize_dedupe_url(file_key)}"

    return f"key:{source}:{file_key}"


# 揭示文件索引存储（MongoDB）：记录所有通过 agent 工具"揭示/生成"给用户的文件与项目，
# 支持按用户/类型/会话/项目筛选、搜索、收藏、按会话分组分页等。
# 核心难点是"同一资源"的去重（见 _build_dedupe_key）以及历史数据向 dedupe_key 唯一索引的迁移。
class RevealedFileStorage:
    """MongoDB storage for revealed file records."""

    def __init__(self):
        # 集合连接延迟初始化。
        self._collection = None

    @property
    def collection(self):
        # 惰性获取 revealed_files 集合。
        if self._collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db["revealed_files"]
        return self._collection

    async def ensure_indexes_if_needed(self):
        # 用实例属性做"进程内只执行一次"的标记，避免每次调用都重复检查/创建索引。
        if not hasattr(self, "_indexes_ensured"):
            self._indexes_ensured = True
            await self._ensure_indexes()

    async def _ensure_indexes(self):
        try:
            c = self.collection
            existing_indexes = await c.index_information()
            await c.create_index(
                [("user_id", 1), ("created_at", -1)],
                name="user_created_at_idx",
                background=True,
            )
            await c.create_index(
                [("user_id", 1), ("file_type", 1)],
                name="user_file_type_idx",
                background=True,
            )
            # Migrate away from the old name-based unique key so users can keep
            # multiple same-named reveals from different sessions/runs.
            if "user_name_source_unique_idx" in existing_indexes:
                await c.drop_index("user_name_source_unique_idx")

            if "user_key_source_unique_idx" in existing_indexes:
                await c.drop_index("user_key_source_unique_idx")

            # 数据迁移：为历史上还没有 dedupe_key 字段的旧文档补齐该字段
            # （用 source + file_key 拼出一个等价的去重键），以便后续统一按 dedupe_key 建唯一索引。
            await c.update_many(
                {"dedupe_key": {"$exists": False}},
                [{"$set": {"dedupe_key": {"$concat": ["key:", "$source", ":", "$file_key"]}}}],
            )

            # Remove duplicates before creating the unique index.
            # Keep the latest document per (user_id, dedupe_key, source) and
            # delete the rest so the unique index can be built.
            pipeline = [
                {
                    "$addFields": {
                        "_effective_dedupe_key": {"$ifNull": ["$dedupe_key", "$file_key"]}
                    }
                },
                {
                    "$group": {
                        "_id": {
                            "user_id": "$user_id",
                            "dedupe_key": "$_effective_dedupe_key",
                            "source": "$source",
                        },
                        "keep_id": {"$max": "$_id"},
                        "count": {"$sum": 1},
                    }
                },
                {"$match": {"count": {"$gt": 1}}},
            ]
            async for group in c.aggregate(pipeline):
                duplicate_key = group["_id"]
                result = await c.delete_many(
                    {
                        "user_id": duplicate_key["user_id"],
                        "dedupe_key": duplicate_key["dedupe_key"],
                        "source": duplicate_key["source"],
                        "_id": {"$ne": group["keep_id"]},
                    }
                )
                logger.info(
                    f"Removed {result.deleted_count} duplicate(s) for "
                    f"user_id={duplicate_key['user_id']}, "
                    f"dedupe_key={duplicate_key['dedupe_key']}, "
                    f"source={duplicate_key['source']}"
                )

            await c.create_index(
                [("user_id", 1), ("dedupe_key", 1), ("source", 1)],
                name="user_dedupe_source_unique_idx",
                unique=True,
                background=True,
            )
            await c.create_index(
                [("session_id", 1)],
                name="session_id_idx",
                background=True,
            )
            await c.create_index(
                [("user_id", 1), ("project_id", 1)],
                name="user_project_idx",
                background=True,
            )
        except Exception as e:
            logger.warning(f"Failed to create revealed_files indexes: {e}")

    # Fields that must never be overwritten from caller-provided data.
    # - _id / user_id: identity / ownership
    # - is_favorite: user's explicit bookmark, must survive re-reveals
    _PROTECTED_FIELDS = frozenset({"_id", "user_id", "is_favorite"})

    async def upsert_by_name(
        self,
        user_id: str,
        file_name: str,
        source: str,
        file_key: str,
        trace_id: str,
        data: Dict[str, Any],
    ) -> None:
        """Upsert a record, deduplicating by user_id + dedupe_key + source.

        ``dedupe_key`` is derived from original_path for generated/local files,
        from normalized URL for remote files, and falls back to file_key for
        older records.  Updates reset *created_at* so the entry bubbles to the
        top of time-sorted lists.  Preserves ``is_favorite`` on the existing doc.
        """
        # 关键字段缺失时直接放弃写入并记警告日志，而不是插入一条无法被正常检索/去重的脏数据。
        if not user_id or not file_name or not source:
            logger.warning(
                f"Skipping upsert_by_name: user_id={user_id!r}, "
                f"file_name={file_name!r}, source={source!r}"
            )
            return

        await self.ensure_indexes_if_needed()
        try:
            now = utc_now()
            dedupe_key = _build_dedupe_key(file_key, source, data)
            # Fields managed by this method — always authoritative
            set_fields: Dict[str, Any] = {
                "file_name": file_name,
                "source": source,
                "file_key": file_key,
                "dedupe_key": dedupe_key,
                "trace_id": trace_id,
                "created_at": now,
            }
            # Merge caller data, but skip protected fields to prevent
            # accidental overwrite of identity / user preference fields.
            for k, v in data.items():
                if k not in self._PROTECTED_FIELDS:
                    set_fields[k] = v

            # 按 (user_id, dedupe_key, source) 做 upsert：同一资源再次被揭示时更新已有记录
            # 并把 created_at 重置为当前时间，使其重新排到按时间排序列表的最前面。
            await self.collection.update_one(
                {
                    "user_id": user_id,
                    "dedupe_key": dedupe_key,
                    "source": source,
                },
                {"$set": set_fields},
                upsert=True,
            )
        except Exception as e:
            # 写入失败仅记录日志、不向上抛出异常，避免因为"记录一下揭示历史"这种次要功能影响主流程。
            logger.warning(f"Failed to upsert revealed file record by name: {e}")

    @staticmethod
    def _serialize_item(item: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize MongoDB records for API responses."""
        normalized = dict(item)

        # project_meta 只有 project 类型的记录才有意义，其他类型上如果混入了脏数据就顺手清掉。
        if normalized.get("file_type") != "project":
            normalized.pop("project_meta", None)

        if "_id" in normalized:
            normalized["id"] = str(normalized.pop("_id"))
        if "created_at" in normalized and isinstance(normalized["created_at"], datetime):
            normalized["created_at"] = to_iso(normalized["created_at"])

        return normalized

    async def _search_session_ids(self, search: str) -> list[str]:
        """Find session IDs whose name matches the search term."""
        # 允许按会话名称搜索：先在 sessions 集合里模糊匹配出候选 session_id 列表，
        # 供上层查询时通过 "$or" 把"文件名/描述匹配"与"所属会话名匹配"结合起来。
        try:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            sessions_col = db[settings.MONGODB_SESSIONS_COLLECTION]
            docs = await sessions_col.find(
                {"name": {"$regex": _safe_search_pattern(search), "$options": "i"}},
                {"session_id": 1},
            ).to_list(length=50)
            return [d["session_id"] for d in docs if d.get("session_id")]
        except Exception as e:
            logger.warning(f"Failed to search sessions by name: {e}")
            return []

    # 切换某条揭示文件记录的收藏状态（is_favorite 取反），返回切换后的新值；记录不存在则抛 ValueError。
    async def toggle_favorite(self, user_id: str, file_id: str) -> bool:
        """Toggle is_favorite on a revealed file record. Returns new value."""
        await self.ensure_indexes_if_needed()
        from bson import ObjectId

        # Use aggregation pipeline update for atomic toggle
        # 用聚合管道形式的 update（而非先读后写）保证"取反"操作是原子的，避免并发请求下的竞态。
        result = await self.collection.update_one(
            {"_id": ObjectId(file_id), "user_id": user_id},
            [{"$set": {"is_favorite": {"$not": {"$ifNull": ["$is_favorite", False]}}}}],
        )
        if result.matched_count == 0:
            raise ValueError(f"Revealed file {file_id} not found")
        # Fetch the new value
        doc = await self.collection.find_one({"_id": ObjectId(file_id)}, {"is_favorite": 1})
        return doc.get("is_favorite", False) if doc else False

    async def list_files(
        self,
        user_id: str,
        *,
        file_type: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        search: Optional[str] = None,
        favorites_only: bool = False,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        skip: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """List revealed files with pagination, filtering, and sorting."""
        await self.ensure_indexes_if_needed()
        limit = _bounded_page_limit(limit)

        query: Dict[str, Any] = {"user_id": user_id}
        if file_type:
            query["file_type"] = file_type
        if session_id:
            query["session_id"] = session_id
        if project_id == "none":
            # 特殊值 "none" 表示筛选"未归属任何项目"的文件，与"不传 project_id 参数"区分开。
            query["project_id"] = None
        elif project_id:
            query["project_id"] = project_id
        if favorites_only:
            query["is_favorite"] = True
        if search:
            safe_search = _safe_search_pattern(search)
            search_conditions: list[Dict[str, Any]] = [
                {"file_name": {"$regex": safe_search, "$options": "i"}},
                {"description": {"$regex": safe_search, "$options": "i"}},
            ]
            # Only search by session_name if not already filtering by session_id
            if not session_id:
                matching_session_ids = await self._search_session_ids(search)
                if matching_session_ids:
                    search_conditions.append({"session_id": {"$in": matching_session_ids}})
            query["$or"] = search_conditions

        sort_dir = -1 if sort_order == "desc" else 1
        if sort_by == "file_name":
            sort_key = "file_name"
        elif sort_by == "file_size":
            sort_key = "file_size"
        else:
            sort_key = "created_at"

        cursor = self.collection.find(query).sort(sort_key, sort_dir).skip(skip).limit(limit)
        # 并发发出"统计总数"与"取当前页数据"两个查询，减少一次往返等待时间。
        total, items = await asyncio.gather(
            self.collection.count_documents(query),
            cursor.to_list(length=limit),
        )

        # Enrich with session_name from sessions collection
        session_ids = list({item["session_id"] for item in items if item.get("session_id")})
        session_names: Dict[str, Optional[str]] = {}
        if session_ids:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            sessions_col = db[settings.MONGODB_SESSIONS_COLLECTION]
            sessions = await sessions_col.find(
                {"session_id": {"$in": session_ids}},
                {"session_id": 1, "name": 1},
            ).to_list(length=len(session_ids))
            session_names = {s["session_id"]: s.get("name") for s in sessions}

        items = [
            self._serialize_item(
                {
                    **item,
                    "session_name": session_names.get(item.get("session_id")),
                }
            )
            for item in items
        ]

        return {"items": items, "total": total, "skip": skip, "limit": limit}

    # 统计该用户各文件类型（file_type）各有多少条揭示记录，用于前端做分类计数展示。
    async def get_stats(self, user_id: str) -> Dict[str, int]:
        """Get file count per type for a user."""
        await self.ensure_indexes_if_needed()
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$group": {"_id": "$file_type", "count": {"$sum": 1}}},
        ]
        results = await self.collection.aggregate(pipeline).to_list(length=20)
        stats = {}
        for r in results:
            stats[r["_id"]] = r["count"]
        return stats

    async def list_files_grouped_by_session(
        self,
        user_id: str,
        *,
        file_type: Optional[str] = None,
        project_id: Optional[str] = None,
        search: Optional[str] = None,
        favorites_only: bool = False,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        skip: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """List revealed files grouped by session, with session-level pagination."""
        await self.ensure_indexes_if_needed()
        limit = _bounded_page_limit(limit)

        # Build base query (same as list_files minus session_id filter)
        query: Dict[str, Any] = {"user_id": user_id, "session_id": {"$ne": None}}
        if file_type:
            query["file_type"] = file_type
        if project_id == "none":
            query["project_id"] = None
        elif project_id:
            query["project_id"] = project_id
        if favorites_only:
            query["is_favorite"] = True

        if search:
            safe_search = _safe_search_pattern(search)
            search_conditions: list[Dict[str, Any]] = [
                {"file_name": {"$regex": safe_search, "$options": "i"}},
                {"description": {"$regex": safe_search, "$options": "i"}},
            ]
            matching_session_ids = await self._search_session_ids(search)
            if matching_session_ids:
                search_conditions.append({"session_id": {"$in": matching_session_ids}})
            query["$or"] = search_conditions

        # Determine sort for the "latest file in session"
        sort_dir = -1 if sort_order == "desc" else 1
        if sort_by in ("file_name", "file_size"):
            file_sort_key = sort_by
        else:
            file_sort_key = "created_at"

        # Aggregate: one doc per session with the latest matching file timestamp
        # 第一阶段聚合：按 session_id 分组，得到每个会话内"最新文件时间"和"文件总数"，
        # 这一步只用于确定会话级别的排序与分页，尚未取出具体文件内容。
        pipeline: list[Dict[str, Any]] = [
            {"$match": query},
            {
                "$group": {
                    "_id": "$session_id",
                    "latest_file_at": {"$max": "$created_at"},
                    "file_count": {"$sum": 1},
                }
            },
        ]
        if file_sort_key == "created_at":
            pipeline.append({"$sort": {"latest_file_at": sort_dir}})
        elif file_sort_key == "file_name":
            # Sort sessions by the alphabetically first/last file name within the session
            # 若按文件名排序会话，需要 $lookup 回原集合找出该会话内排序后的第一个文件名作为排序依据。
            pipeline.append(
                {
                    "$lookup": {
                        "from": self.collection.name,
                        "let": {"sid": "$_id"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$session_id", "$$sid"]},
                                            {"$eq": ["$user_id", user_id]},
                                        ]
                                    }
                                }
                            },
                            {"$sort": {"file_name": sort_dir}},
                            {"$limit": 1},
                            {"$project": {"file_name": 1}},
                        ],
                        "as": "_name_sample",
                    }
                }
            )
            pipeline.append(
                {"$unwind": {"path": "$_name_sample", "preserveNullAndEmptyArrays": True}}
            )
            pipeline.append({"$sort": {"_name_sample.file_name": sort_dir}})
        elif file_sort_key == "file_size":
            # 同上，按文件大小排序会话时同样需要 $lookup 找出该会话内排序后的第一个文件的大小。
            pipeline.append(
                {
                    "$lookup": {
                        "from": self.collection.name,
                        "let": {"sid": "$_id"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$session_id", "$$sid"]},
                                            {"$eq": ["$user_id", user_id]},
                                        ]
                                    }
                                }
                            },
                            {"$sort": {"file_size": sort_dir}},
                            {"$limit": 1},
                            {"$project": {"file_size": 1}},
                        ],
                        "as": "_size_sample",
                    }
                }
            )
            pipeline.append(
                {"$unwind": {"path": "$_size_sample", "preserveNullAndEmptyArrays": True}}
            )
            pipeline.append({"$sort": {"_size_sample.file_size": sort_dir}})

        # Count distinct sessions (before skip/limit)
        count_pipeline = pipeline.copy()
        count_pipeline.append({"$count": "total"})
        count_result = await self.collection.aggregate(count_pipeline).to_list(length=1)
        total_sessions = count_result[0]["total"] if count_result else 0

        # Paginate sessions
        pipeline.append({"$skip": skip})
        pipeline.append({"$limit": limit})

        session_results = await self.collection.aggregate(pipeline).to_list(length=limit)
        session_ids = [r["_id"] for r in session_results]

        if not session_ids:
            return {"sessions": [], "total_sessions": total_sessions, "skip": skip, "limit": limit}

        # Fetch a bounded preview per session. A single hot session can otherwise
        # fill the grouped response and materialize hundreds of file documents.
        file_query_base: Dict[str, Any] = {"user_id": user_id}
        if file_type:
            file_query_base["file_type"] = file_type
        if project_id == "none":
            file_query_base["project_id"] = None
        elif project_id:
            file_query_base["project_id"] = project_id
        if favorites_only:
            file_query_base["is_favorite"] = True
        # Re-apply file name/description search (but NOT session_id search to avoid conflict)
        if search:
            safe_search = _safe_search_pattern(search)
            file_query_base["$or"] = [
                {"file_name": {"$regex": safe_search, "$options": "i"}},
                {"description": {"$regex": safe_search, "$options": "i"}},
            ]

        file_sort_dir = -1 if sort_order == "desc" else 1
        if sort_by == "file_name":
            file_sort = [("file_name", file_sort_dir)]
        elif sort_by == "file_size":
            file_sort = [("file_size", file_sort_dir)]
        else:
            file_sort = [("created_at", file_sort_dir)]

        # Enrich with session names
        from src.infra.storage.mongodb import get_mongo_client

        client = get_mongo_client()
        db = client[settings.MONGODB_DB]
        sessions_col = db[settings.MONGODB_SESSIONS_COLLECTION]
        sessions = await sessions_col.find(
            {"session_id": {"$in": session_ids}},
            {"session_id": 1, "name": 1},
        ).to_list(length=len(session_ids))
        name_map: Dict[str, Optional[str]] = {s["session_id"]: s.get("name") for s in sessions}

        # Group files by session
        # 逐会话查询文件预览，每个会话最多取 REVEALED_FILE_GROUPED_FILES_PER_SESSION_MAX 条，
        # 用循环 + 单独查询而非一次性聚合，是为了保证"每会话限量"这一约束能被精确应用。
        files_by_session: Dict[str, list] = {sid: [] for sid in session_ids}
        for sid in session_ids:
            file_query = {**file_query_base, "session_id": sid}
            cursor = (
                self.collection.find(file_query)
                .sort(file_sort)
                .limit(REVEALED_FILE_GROUPED_FILES_PER_SESSION_MAX)
            )
            raw_files = await cursor.to_list(length=REVEALED_FILE_GROUPED_FILES_PER_SESSION_MAX)
            for item in raw_files:
                files_by_session[sid].append(
                    self._serialize_item(
                        {
                            **item,
                            "session_name": name_map.get(sid),
                        }
                    )
                )

        count_map = {r["_id"]: r["file_count"] for r in session_results}
        sessions_list = []
        for sid in session_ids:
            sessions_list.append(
                {
                    "session_id": sid,
                    "session_name": name_map.get(sid),
                    "file_count": count_map[sid],
                    "files": files_by_session[sid],
                }
            )

        return {
            "sessions": sessions_list,
            "total_sessions": total_sessions,
            "skip": skip,
            "limit": limit,
        }

    # 取该用户"有揭示文件"的会话列表（session_id + 会话名 + 文件数），按文件数倒序，作为按会话浏览的入口。
    async def get_user_sessions(self, user_id: str) -> list[Dict[str, Any]]:
        """Get distinct session_id + session_name pairs for a user's revealed files."""
        await self.ensure_indexes_if_needed()
        pipeline = [
            {"$match": {"user_id": user_id, "session_id": {"$ne": None}}},
            {"$group": {"_id": "$session_id", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": REVEALED_FILE_SESSION_LIST_LIMIT},
        ]
        results = await self.collection.aggregate(pipeline).to_list(
            length=REVEALED_FILE_SESSION_LIST_LIMIT
        )
        session_ids = [r["_id"] for r in results]

        if not session_ids:
            return []

        # Enrich with session names
        from src.infra.storage.mongodb import get_mongo_client

        client = get_mongo_client()
        db = client[settings.MONGODB_DB]
        sessions_col = db[settings.MONGODB_SESSIONS_COLLECTION]
        sessions = await sessions_col.find(
            {"session_id": {"$in": session_ids}},
            {"session_id": 1, "name": 1},
        ).to_list(length=len(session_ids))
        name_map: Dict[str, Optional[str]] = {s["session_id"]: s.get("name") for s in sessions}

        count_map = {r["_id"]: r["count"] for r in results}
        items = []
        for sid in session_ids:
            items.append(
                {
                    "session_id": sid,
                    "session_name": name_map.get(sid),
                    "file_count": count_map[sid],
                }
            )
        return items

    # 删除某会话下的全部揭示文件记录（如会话被删除时联动清理），返回删除条数。
    async def delete_by_session(self, session_id: str) -> int:
        """Delete all revealed file records for a session."""
        await self.ensure_indexes_if_needed()
        result = await self.collection.delete_many({"session_id": session_id})
        return result.deleted_count

    # 把某会话下所有揭示文件的 project_id 批量改为指定值（会话被归入/移出项目时调用），返回更新条数。
    async def update_project_id_by_session(self, session_id: str, project_id: Optional[str]) -> int:
        """Update project_id on all revealed files belonging to a session."""
        await self.ensure_indexes_if_needed()
        result = await self.collection.update_many(
            {"session_id": session_id},
            {"$set": {"project_id": project_id}},
        )
        return result.modified_count

    # 清空归属某项目的所有揭示文件的 project_id（如项目被删除时调用），返回更新条数。
    async def clear_project_id(self, project_id: str) -> int:
        """Clear project_id on all revealed files belonging to a project (e.g. on project delete)."""
        await self.ensure_indexes_if_needed()
        result = await self.collection.update_many(
            {"project_id": project_id},
            {"$set": {"project_id": None}},
        )
        return result.modified_count

    async def close(self) -> None:
        # 释放集合引用，供上层在应用关闭时统一清理。
        self._collection = None


# Singleton
# 进程级单例，避免重复创建存储实例及其底层连接。
_revealed_file_storage: Optional[RevealedFileStorage] = None


def get_revealed_file_storage() -> RevealedFileStorage:
    # 惰性获取单例。
    global _revealed_file_storage
    if _revealed_file_storage is None:
        _revealed_file_storage = RevealedFileStorage()
    return _revealed_file_storage


async def close_revealed_file_storage() -> None:
    # 应用关闭时释放单例资源。
    global _revealed_file_storage
    storage = _revealed_file_storage
    _revealed_file_storage = None
    if storage is not None:
        await storage.close()

"""
会话存储层
"""

# 会话存储层（SessionStorage）：封装会话文档在 MongoDB 中的全部读写细节。
# 关键设计：
#   - 双主键兼容：优先按业务自定义 session_id 定位，未命中再回退到 Mongo 的 _id，
#     几乎所有增删改查都遵循「先 session_id、后 ObjectId」的两段式匹配；
#   - 延迟建连 + 一次性后台建索引：collection 首次使用时才连库；索引通过类级
#     双重检查 + 锁只在全进程创建一次（background=True，不阻塞启动与写入）；
#   - 搜索索引：把会话名与用户消息拆成检索词落库，支持模糊搜索与高亮预览；增量
#     追加/回填用 CAS（比对更新时间）乐观锁 + 有限次重试，避免并发写相互覆盖；
#   - 会话级元数据维护：未读计数、收藏切换、项目归属、定时任务会话过滤等。
import asyncio
from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId

# run_blocking_io：把同步阻塞的 CPU/IO 操作丢到线程池执行，避免阻塞事件循环
from src.infra.async_utils import run_blocking_io
# favorites：会话收藏相关工具（判断是否收藏、规范化 metadata）
from src.infra.session.favorites import (
    is_session_favorite,
    normalize_session_metadata,
)
# search_index：会话搜索索引的构建/合并工具，把会话名与用户消息拆成检索词
from src.infra.session.search_index import (
    SESSION_SEARCH_INDEX_VERSION,
    append_message_to_search_index,
    build_backfilled_search_index,
    build_search_preview,
    build_search_query_terms,
    compose_session_search_index,
    merge_search_state,
)
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.session import Session, SessionCreate, SessionUpdate

# 批量按 session_id 查询时一次最多查多少条，防止 $in 列表过大拖垮查询
SESSION_BATCH_LOOKUP_LIMIT = 100
# 列表查询时单页最多返回多少条，作为分页 limit 的硬上限
SESSION_LIST_LOOKUP_LIMIT = 100


class SessionStorage:
    """
    会话存储类

    使用 MongoDB 存储会话数据。
    """

    SEARCH_BACKFILL_SKIP_RECENT_SECONDS = 120
    SEARCH_UPDATE_MAX_RETRIES = 3
    SEARCH_BACKFILL_MAX_USER_MESSAGES = 1000
    SEARCH_BACKFILL_BATCH_MAX = 100
    # 类级标志：索引是否已确保创建（进程内只需建一次，避免每次操作都尝试建索引）
    _indexes_done = False
    # 类级任务句柄：正在后台创建索引的 asyncio.Task，用于多协程共享同一次创建过程
    _indexes_task: asyncio.Task | None = None
    # 类级锁：保护 _indexes_done/_indexes_task 的并发访问，确保索引只创建一次
    _indexes_lock: asyncio.Lock | None = None

    def __init__(self):
        # 延迟初始化 MongoDB 集合，首次访问 collection 属性时再真正连接
        self._collection = None

    @property
    def collection(self):
        """延迟加载 MongoDB 集合"""
        # 延迟加载：避免在导入阶段就建立数据库连接，改为首次使用时初始化
        if self._collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db[settings.MONGODB_SESSIONS_COLLECTION]
        return self._collection

    async def ensure_indexes_if_needed(self):
        """Ensure session indexes exist."""
        # 通过类级状态确保全进程只创建一次索引；双重检查 + 锁避免并发重复创建
        cls = type(self)
        if cls._indexes_done:
            return

        if cls._indexes_lock is None:
            cls._indexes_lock = asyncio.Lock()

        async with cls._indexes_lock:
            # 拿到锁后再次检查，防止等待期间别的协程已经完成创建
            if cls._indexes_done:
                return
            # 复用同一个后台创建任务，多个协程共享其结果
            if cls._indexes_task is None or cls._indexes_task.cancelled():
                cls._indexes_task = asyncio.create_task(self._ensure_indexes())
            task = cls._indexes_task

        succeeded = await task
        if succeeded:
            cls._indexes_done = True
            return

        # 创建失败则清理任务句柄，允许后续调用重试
        async with cls._indexes_lock:
            if cls._indexes_task is task:
                cls._indexes_task = None

    async def _ensure_indexes(self) -> bool:
        # 实际创建各类查询索引；均为后台创建（background=True）以免阻塞写入
        try:
            collection = self.collection
            # 按 用户 + 活跃状态 + 更新时间 的会话列表主查询索引
            await collection.create_index(
                [("user_id", 1), ("is_active", 1), ("updated_at", -1)],
                name="user_status_updated_idx",
                background=True,
            )
            # 按 用户 + 项目 + 更新时间 的项目内会话列表索引
            await collection.create_index(
                [("user_id", 1), ("metadata.project_id", 1), ("updated_at", -1)],
                name="user_project_updated_idx",
                background=True,
            )
            # 自定义 session_id 的唯一定位索引（sparse：仅对存在该字段的文档建索引）
            await collection.create_index(
                [("session_id", 1)],
                name="session_id_idx",
                background=True,
                sparse=True,
            )
            # 按 用户 + 检索词 + 更新时间 的会话搜索索引
            await collection.create_index(
                [("user_id", 1), ("search_terms", 1), ("updated_at", -1)],
                name="user_search_terms_updated_idx",
                background=True,
            )
            # 按 搜索索引版本 + 更新时间 的索引，用于回填过期搜索索引时快速筛选
            await collection.create_index(
                [("search_index_version", 1), ("updated_at", -1)],
                name="search_index_version_updated_idx",
                background=True,
            )
            # 搜索索引更新时间索引，配合 CAS 乐观并发控制使用
            await collection.create_index(
                [("search_index_updated_at", 1)],
                name="search_index_updated_at_idx",
                background=True,
                sparse=True,
            )
            # 定时任务生成的会话索引，用于任务详情页下钻查询
            await collection.create_index(
                [("metadata.scheduled_task_id", 1), ("updated_at", -1)],
                name="scheduled_task_sessions_idx",
                background=True,
                sparse=True,
            )
            return True
        except Exception:
            # Search index creation is best-effort and should not block the app.
            # 建索引失败不应阻断应用启动，返回 False 让调用方后续重试
            return False

    @staticmethod
    def _build_session(
        session_dict: dict[str, Any],
        favorites_project_id: str | None = None,
    ) -> Session:
        """Convert a Mongo document into a normalized Session model."""
        # 把 Mongo 原始文档转换为对外的 Session 模型，并统一 id 字段来源
        normalized = dict(session_dict)
        # 规范化 metadata（补全收藏状态等派生字段）
        normalized["metadata"] = normalize_session_metadata(
            normalized.get("metadata"),
            favorites_project_id,
        )
        # 优先用自定义 session_id 作为对外 id，否则回退到 Mongo 的 _id
        normalized["id"] = normalized.get("session_id") or str(normalized.pop("_id"))
        # 若 id 来自 session_id，则丢弃 _id 避免多余字段
        if "session_id" in normalized and normalized["id"] == normalized["session_id"]:
            normalized.pop("_id", None)
        return Session(**normalized)

    async def create(
        self,
        session_data: SessionCreate,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        """创建会话"""
        await self.ensure_indexes_if_needed()
        now = utc_now()

        # 使用自定义 session_id 或生成新的
        actual_session_id = session_id or None
        # 新会话初始只有名称可供检索，消息检索词为空，后续随对话增量追加
        search_payload = compose_session_search_index(
            session_name=session_data.name,
            message_search_terms=[],
            search_text="",
            latest_user_message="",
        )

        # 组装待插入的会话文档，包含元数据、时间戳与各类搜索索引字段
        session_dict = {
            "name": session_data.name,
            "metadata": session_data.metadata,
            "user_id": user_id,
            "agent_id": session_data.metadata.get("agent_id", "fast"),
            "created_at": now,
            "updated_at": now,
            "is_active": True,
            "name_search_terms": search_payload.name_search_terms,
            "message_search_terms": search_payload.message_search_terms,
            "search_terms": search_payload.search_terms,
            "search_text": search_payload.search_text,
            "latest_user_message": search_payload.latest_user_message,
            "search_index_version": search_payload.search_index_version,
            "search_index_updated_at": now,
        }

        # 如果提供了自定义 session_id，存储它
        if actual_session_id:
            session_dict["session_id"] = actual_session_id

        result = await self.collection.insert_one(session_dict)

        # 返回时使用自定义 session_id 作为 id 字段
        session_dict["id"] = actual_session_id or str(result.inserted_id)

        return Session(**session_dict)

    async def get_by_session_id(self, session_id: str) -> Optional[Session]:
        """通过自定义 session_id 获取会话"""
        await self.ensure_indexes_if_needed()
        session_dict = await self.collection.find_one({"session_id": session_id})

        if not session_dict:
            return None

        return self._build_session(session_dict)

    async def get_by_session_ids(self, session_ids: list[str]) -> Dict[str, Session]:
        """通过 session_id 列表批量获取会话，返回 {session_id: Session} 映射"""
        if not session_ids:
            return {}
        await self.ensure_indexes_if_needed()
        # 去重并保序，同时限制批量上限，避免 $in 列表过大
        unique_ids = []
        seen_ids = set()
        for session_id in session_ids:
            if session_id in seen_ids:
                continue
            seen_ids.add(session_id)
            unique_ids.append(session_id)
            if len(unique_ids) >= SESSION_BATCH_LOOKUP_LIMIT:
                break
        cursor = self.collection.find({"session_id": {"$in": unique_ids}})
        result: Dict[str, Session] = {}
        async for doc in cursor:
            session = self._build_session(doc)
            result[session.id] = session
        return result

    async def update_user_id(self, session_id: str, user_id: str) -> bool:
        """通过自定义 session_id 更新 user_id"""
        await self.ensure_indexes_if_needed()
        # 仅当会话当前 user_id 为空时才认领，避免覆盖已归属用户（匿名会话登录后绑定）
        result = await self.collection.update_one(
            {"session_id": session_id, "user_id": None},
            {"$set": {"user_id": user_id, "updated_at": utc_now()}},
        )
        return result.modified_count > 0

    async def get_by_id(self, session_id: str) -> Optional[Session]:
        """通过 ID 获取会话"""
        await self.ensure_indexes_if_needed()
        # session_id 在此按 Mongo ObjectId 处理；格式非法时捕获异常返回 None
        try:
            session_dict = await self.collection.find_one({"_id": ObjectId(session_id)})
        except Exception:
            return None

        if not session_dict:
            return None

        return self._build_session(session_dict)

    async def update(self, session_id: str, session_data: SessionUpdate) -> Optional[Session]:
        """更新会话（支持自定义 session_id 或 ObjectId）"""
        await self.ensure_indexes_if_needed()
        update_dict: dict = {"updated_at": utc_now()}

        # 仅在需要改名时才回读旧文档，用于保留已有的消息检索词
        existing_doc = None
        if session_data.name is not None:
            existing_doc = await self._find_doc(
                session_id,
                {
                    "name": 1,
                    "message_search_terms": 1,
                },
            )

        if session_data.name is not None:
            update_dict["name"] = session_data.name
            # 改名会重建名称检索词，但保留原有消息检索词，避免丢失历史检索能力
            search_payload = compose_session_search_index(
                session_name=session_data.name,
                message_search_terms=(existing_doc or {}).get("message_search_terms") or [],
                search_text="",
                latest_user_message="",
            )
            update_dict["name_search_terms"] = search_payload.name_search_terms
            update_dict["search_terms"] = search_payload.search_terms
            update_dict["search_index_version"] = SESSION_SEARCH_INDEX_VERSION
            update_dict["search_index_updated_at"] = utc_now()

        if session_data.metadata is not None:
            # 使用深度合并而非直接覆盖，保留未指定的 metadata 字段
            for key, value in session_data.metadata.items():
                update_dict[f"metadata.{key}"] = value

        # 优先使用自定义 session_id 查询
        result = await self.collection.find_one_and_update(
            {"session_id": session_id},
            {"$set": update_dict},
            return_document=True,
        )

        # 如果没找到，尝试 ObjectId
        if not result:
            try:
                result = await self.collection.find_one_and_update(
                    {"_id": ObjectId(session_id)},
                    {"$set": update_dict},
                    return_document=True,
                )
            except Exception:
                return None

        if not result:
            return None

        return self._build_session(result)

    async def update_metadata_only(self, session_id: str, metadata: dict[str, Any]) -> bool:
        """Update session metadata without fetching and rebuilding the full document."""
        # 只更新 metadata，跳过回读整个文档与重建搜索索引，属于轻量高频写路径
        await self.ensure_indexes_if_needed()
        if not metadata:
            return True

        # 用点号路径逐字段更新，实现 metadata 的深度合并而非整体覆盖
        update_dict: dict[str, Any] = {"updated_at": utc_now()}
        for key, value in metadata.items():
            update_dict[f"metadata.{key}"] = value

        result = await self.collection.update_one(
            {"session_id": session_id},
            {"$set": update_dict},
        )
        if result.matched_count > 0:
            return True

        # 自定义 session_id 未命中时回退按 ObjectId 更新
        try:
            result = await self.collection.update_one(
                {"_id": ObjectId(session_id)},
                {"$set": update_dict},
            )
            return result.matched_count > 0
        except Exception:
            return False

    async def delete(self, session_id: str) -> bool:
        """删除会话（支持自定义 session_id 或 ObjectId）"""
        await self.ensure_indexes_if_needed()
        # 优先使用自定义 session_id
        result = await self.collection.delete_one({"session_id": session_id})
        if result.deleted_count > 0:
            return True

        # 未命中时回退按 ObjectId 删除
        try:
            result = await self.collection.delete_one({"_id": ObjectId(session_id)})
            return result.deleted_count > 0
        except Exception:
            return False

    async def list_sessions(
        self,
        user_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        is_active: Optional[bool] = None,
        project_id: Optional[str] = None,
        search: Optional[str] = None,
        favorites_only: bool = False,
        favorites_project_id: str | None = None,
    ) -> tuple[list[Session], int]:
        """列出会话，返回 (sessions, total_count)

        Args:
            user_id: 用户ID，如果提供则只返回该用户的会话
                     None 表示不过滤（仅管理员使用）
            project_id: 项目ID过滤
                       - None: 不过滤项目
                       - "none": 只返回未分类的会话（没有project_id）
                       - 其他值: 只返回该项目内的会话
            search: 搜索关键词，模糊匹配会话名称
        """
        await self.ensure_indexes_if_needed()
        # 归一化分页参数：skip 不小于 0，limit 落在 [1, 上限] 区间内
        skip = max(int(skip or 0), 0)
        limit = min(max(int(limit or 1), 1), SESSION_LIST_LOOKUP_LIMIT)
        query: dict[str, Any] = {}
        # 默认排除对话列表中隐藏的会话
        query["metadata.hidden_from_conversation_list"] = {"$ne": True}
        # 排除定时任务会话（session_id 以 sch_ 前缀）；它们只在任务详情页展示
        query["session_id"] = {"$not": {"$regex": "^sch_"}}
        if user_id is not None:
            # 严格匹配用户ID，空字符串也会被当作过滤条件
            query["user_id"] = user_id
        if is_active is not None:
            query["is_active"] = is_active

        if search:
            # 将搜索词拆分为多个检索 term，要求全部命中（$all）
            search_terms = build_search_query_terms(search)
            if search_terms:
                query["search_terms"] = {"$all": search_terms}
            else:
                # 无有效检索词时构造一个永不命中的条件，返回空结果
                query["session_id"] = {"$in": []}

        # Project filter
        if project_id == "none":
            # Unclassified conversation list excludes scheduled task sessions;
            # those are shown through the scheduled task drill-down view.
            # "none" 表示未分类：既没有项目也不属于定时任务
            query["metadata.project_id"] = None
            query["metadata.scheduled_task_id"] = None
        elif project_id is not None:
            query["metadata.project_id"] = project_id

        if favorites_only:
            # 收藏过滤：命中收藏标记，或（可选）属于指定收藏夹项目
            favorite_query: list[dict[str, Any]] = [{"metadata.is_favorite": True}]
            if favorites_project_id:
                favorite_query.append({"metadata.project_id": favorites_project_id})
            # 若查询里已有 $or，需要用 $and 包裹避免两个 $or 相互覆盖
            if "$or" in query:
                query = {
                    "$and": [
                        {k: v for k, v in query.items() if k != "$or"},
                        {"$or": query["$or"]},
                        {"$or": favorite_query},
                    ]
                }
            else:
                query["$or"] = favorite_query

        # Get total count
        # 先统计总数用于分页展示
        total = await self.collection.count_documents(query)

        # 按更新时间倒序取当前页，最新活动的会话排在前面
        cursor = self.collection.find(query).skip(skip).limit(limit).sort("updated_at", -1)
        sessions = []

        for session_dict in await cursor.to_list(length=limit):
            session = self._build_session(session_dict, favorites_project_id)
            if search:
                # 命中搜索时，从全文中截取带高亮的预览片段挂到 metadata 上
                match_preview = build_search_preview(session_dict.get("search_text"), search)
                if match_preview:
                    session = session.model_copy(
                        update={
                            "metadata": {
                                **session.metadata,
                                "search_match": match_preview,
                                "search_match_source": "user_message",
                            }
                        }
                    )
            sessions.append(session)

        return sessions, total

    async def list_sessions_for_task(
        self,
        scheduled_task_id: str,
        user_id: str,
        skip: int = 0,
        limit: int = 100,
    ) -> tuple[list[Session], int]:
        """List sessions created by a scheduled task.

        Unlike list_sessions(), this does NOT filter out
        hidden_from_conversation_list — scheduled task sessions
        are intentionally hidden from the regular sidebar but
        should be visible in the task drill-down view.
        """
        await self.ensure_indexes_if_needed()
        skip = max(int(skip or 0), 0)
        limit = min(max(int(limit or 1), 1), SESSION_LIST_LOOKUP_LIMIT)
        # 按定时任务 id 精确过滤，注意此处不排除 hidden_from_conversation_list
        query: dict[str, Any] = {
            "user_id": user_id,
            "metadata.scheduled_task_id": scheduled_task_id,
        }
        total = await self.collection.count_documents(query)
        cursor = self.collection.find(query).skip(skip).limit(limit).sort("updated_at", -1)
        sessions = []
        for session_dict in await cursor.to_list(length=limit):
            session = self._build_session(session_dict)
            sessions.append(session)
        return sessions, total

    async def get_unread_counts_for_scheduled_tasks(
        self,
        user_id: str,
        scheduled_task_ids: list[str],
    ) -> dict[str, int]:
        """Return unread totals keyed by scheduled task id."""
        await self.ensure_indexes_if_needed()
        if not scheduled_task_ids:
            return {}

        # 聚合管道：筛出有未读的会话，再按定时任务 id 分组累加未读数
        pipeline = [
            {
                "$match": {
                    "user_id": user_id,
                    "metadata.scheduled_task_id": {"$in": scheduled_task_ids},
                    "unread_count": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$metadata.scheduled_task_id",
                    "unread_count": {"$sum": "$unread_count"},
                }
            },
        ]

        # 将聚合结果整理为 {task_id: 未读总数} 映射
        counts: dict[str, int] = {}
        async for item in self.collection.aggregate(pipeline):
            task_id = item.get("_id")
            if isinstance(task_id, str):
                counts[task_id] = int(item.get("unread_count") or 0)
        return counts

    async def get(self, session_id: str) -> Optional[Session]:
        """获取会话 (兼容旧 API)"""
        return await self.get_by_id(session_id)

    async def clear_project_id(self, project_id: str, user_id: str) -> int:
        """Clear project_id for all sessions in a project (when project is deleted).

        Args:
            project_id: The project ID to clear
            user_id: The user ID to filter sessions

        Returns:
            Number of modified sessions
        """
        await self.ensure_indexes_if_needed()
        # 项目被删除时，把该项目下所有会话的 project_id 清空（转为未分类），而非删除会话
        result = await self.collection.update_many(
            {"user_id": user_id, "metadata.project_id": project_id},
            {"$set": {"metadata.project_id": None, "updated_at": utc_now()}},
        )
        return result.modified_count

    async def increment_unread_count(self, session_id: str) -> bool:
        """递增会话未读计数"""
        await self.ensure_indexes_if_needed()
        # 有新消息推送到会话时未读数 +1，供前端角标展示
        result = await self.collection.update_one(
            {"session_id": session_id},
            {"$inc": {"unread_count": 1}, "$set": {"updated_at": utc_now()}},
        )
        return result.modified_count > 0

    async def mark_read(self, session_id: str) -> bool:
        """将会话标记为已读（清除未读计数）"""
        await self.ensure_indexes_if_needed()
        result = await self.collection.update_one(
            {"session_id": session_id},
            {"$set": {"unread_count": 0}},
        )
        return result.modified_count > 0

    async def mark_all_read(
        self,
        user_id: str,
        project_id: str | None = None,
        scheduled_task_id: str | None = None,
    ) -> int:
        """批量将会话标记为已读（清除未读计数），支持按项目或定时任务过滤。"""
        await self.ensure_indexes_if_needed()
        # 只更新当前有未读的会话，减少无谓写入
        query: dict[str, Any] = {"user_id": user_id, "unread_count": {"$gt": 0}}
        if project_id:
            query["metadata.project_id"] = project_id
        if scheduled_task_id:
            query["metadata.scheduled_task_id"] = scheduled_task_id
        result = await self.collection.update_many(
            query,
            {"$set": {"unread_count": 0, "updated_at": utc_now()}},
        )
        return result.modified_count

    async def delete_by_project(self, project_id: str, user_id: str) -> int:
        """Delete all sessions in a project.

        Args:
            project_id: The project ID
            user_id: The user ID (for ownership verification)

        Returns:
            Number of deleted sessions
        """
        await self.ensure_indexes_if_needed()
        # 删除项目时一并删除其下所有会话；以 user_id 限定确保只删本人数据
        result = await self.collection.delete_many(
            {"user_id": user_id, "metadata.project_id": project_id},
        )
        return result.deleted_count

    async def list_ids_by_project(self, project_id: str, user_id: str) -> list[str]:
        """List session identifiers for all sessions in a project."""
        await self.ensure_indexes_if_needed()
        # 只投影出定位所需字段，减少数据传输
        cursor = self.collection.find(
            {"user_id": user_id, "metadata.project_id": project_id},
            {"session_id": 1, "_id": 1},
        )
        session_ids: list[str] = []
        async for doc in cursor:
            # 优先返回自定义 session_id，缺失时回退 ObjectId 字符串
            session_ids.append(doc.get("session_id") or str(doc["_id"]))
        return session_ids

    async def move_to_project(
        self, session_id: str, user_id: str, project_id: Optional[str]
    ) -> Optional[Session]:
        """Move a session to a project.

        Args:
            session_id: The session ID to move
            user_id: The user ID (for ownership verification)
            project_id: The target project ID, or None to uncategorize

        Returns:
            Updated Session if found and updated, None otherwise
        """
        update_dict = {
            "updated_at": utc_now(),
            "metadata.project_id": project_id,
        }

        # Try custom session_id first
        # 以 user_id 限定确保只能移动本人会话；优先按自定义 session_id 匹配
        result = await self.collection.find_one_and_update(
            {"session_id": session_id, "user_id": user_id},
            {"$set": update_dict},
            return_document=True,
        )

        # If not found, try ObjectId
        if not result:
            try:
                result = await self.collection.find_one_and_update(
                    {"_id": ObjectId(session_id), "user_id": user_id},
                    {"$set": update_dict},
                    return_document=True,
                )
            except Exception:
                return None

        if not result:
            return None

        return self._build_session(result)

    async def append_user_message_search_content(self, session_id: str, content: str) -> bool:
        """Persist user-message search terms and preview text on the session document."""
        # 每条新用户消息增量追加检索词与预览文本；用 CAS 乐观锁 + 重试避免并发覆盖
        await self.ensure_indexes_if_needed()
        for _ in range(self.SEARCH_UPDATE_MAX_RETRIES):
            # 回读当前检索状态，作为增量合并与 CAS 比对的基准
            existing_doc = await self._find_doc(
                session_id,
                {
                    "name": 1,
                    "message_search_terms": 1,
                    "search_text": 1,
                    "updated_at": 1,
                    "search_index_updated_at": 1,
                },
            )
            if not existing_doc:
                return False

            # 在旧检索状态基础上并入这条新消息，得到新的索引载荷
            payload = append_message_to_search_index(
                session_name=existing_doc.get("name"),
                existing_message_search_terms=existing_doc.get("message_search_terms") or [],
                existing_search_text=existing_doc.get("search_text"),
                latest_user_message=content,
            )
            update_dict = {
                "name_search_terms": payload.name_search_terms,
                "message_search_terms": payload.message_search_terms,
                "search_terms": payload.search_terms,
                "search_text": payload.search_text,
                "latest_user_message": payload.latest_user_message,
                "search_index_version": payload.search_index_version,
                "search_index_updated_at": utc_now(),
            }
            # 取 CAS 比对字段与期望值，仅当文档未被他人改动时才写入
            cas_field, cas_value = self._get_search_index_cas(existing_doc)
            result = await self._update_doc(
                session_id,
                {"$set": update_dict},
                expected_cas_field=cas_field,
                expected_cas_value=cas_value,
            )
            # 写入成功即返回；CAS 失败则循环重试
            if result.modified_count > 0:
                return True
        return False

    async def rebuild_search_index(self, session_id: str) -> bool:
        """Rebuild session search data from persisted user:message events."""
        # 从事件溯源里回放全部 user:message 事件，重建整份会话搜索索引
        await self.ensure_indexes_if_needed()
        existing_doc = await self._find_doc(
            session_id,
            {
                "name": 1,
            },
        )
        if not existing_doc:
            return False

        # 延迟导入避免与 trace_storage 形成循环依赖
        from src.infra.session.trace_storage import get_trace_storage

        # 拉取该会话所有用户消息事件（含未完成事件），作为重建数据源
        trace_storage = get_trace_storage()
        events = await trace_storage.get_session_events(
            session_id,
            event_types=["user:message"],
            completed_only=False,
            max_events=self.SEARCH_BACKFILL_MAX_USER_MESSAGES,
        )
        # 从事件里提取非空的用户消息文本
        user_messages = [
            data.get("content", "").strip()
            for event in events
            if isinstance((data := event.get("data")), dict)
            and isinstance(data.get("content"), str)
            and data.get("content", "").strip()
        ]

        # 分词/构建索引是 CPU 密集操作，放到线程池执行避免阻塞事件循环
        payload = await run_blocking_io(
            build_backfilled_search_index,
            session_name=existing_doc.get("name"),
            user_messages=user_messages,
        )
        for _ in range(self.SEARCH_UPDATE_MAX_RETRIES):
            # 重建期间可能有新消息写入，回读最新状态用于合并，避免覆盖新增检索词
            current_doc = await self._find_doc(
                session_id,
                {
                    "name": 1,
                    "message_search_terms": 1,
                    "search_text": 1,
                    "latest_user_message": 1,
                    "updated_at": 1,
                    "search_index_updated_at": 1,
                },
            )
            if not current_doc:
                return False

            # 将回填结果与期间新增的检索状态合并，保证两边数据都不丢
            merged = merge_search_state(
                session_name=current_doc.get("name") or existing_doc.get("name"),
                base_message_terms=payload.message_search_terms,
                base_search_text=payload.search_text,
                base_latest_user_message=payload.latest_user_message,
                extra_message_terms=current_doc.get("message_search_terms") or [],
                extra_search_text=current_doc.get("search_text"),
                extra_latest_user_message=current_doc.get("latest_user_message"),
            )
            update_dict = {
                "name_search_terms": merged.name_search_terms,
                "message_search_terms": merged.message_search_terms,
                "search_terms": merged.search_terms,
                "search_text": merged.search_text,
                "latest_user_message": merged.latest_user_message,
                "search_index_version": merged.search_index_version,
                "search_index_updated_at": utc_now(),
            }
            # CAS 乐观锁写入，冲突则重试
            cas_field, cas_value = self._get_search_index_cas(current_doc)
            result = await self._update_doc(
                session_id,
                {"$set": update_dict},
                expected_cas_field=cas_field,
                expected_cas_value=cas_value,
            )
            if result.modified_count > 0:
                return True
        return False

    async def backfill_search_indexes(self, batch_size: int = 100) -> int:
        """Backfill stale session search indexes in small batches."""
        # 后台小批量回填搜索索引版本过期的会话，供离线任务分批推进
        await self.ensure_indexes_if_needed()
        batch_size = min(max(int(batch_size), 1), self.SEARCH_BACKFILL_BATCH_MAX)
        # 跳过最近刚更新的会话，避免与实时写入抢锁造成 CAS 冲突
        cutoff = utc_now().timestamp() - self.SEARCH_BACKFILL_SKIP_RECENT_SECONDS
        cutoff_dt = datetime.fromtimestamp(cutoff)
        # 过期条件：索引版本不等于当前版本（或缺失），且更新时间早于冷却时间
        stale_query = {
            "$and": [
                {
                    "$or": [
                        {"search_index_version": {"$ne": SESSION_SEARCH_INDEX_VERSION}},
                        {"search_index_version": None},
                    ]
                },
                {"updated_at": {"$lt": cutoff_dt}},
            ]
        }
        cursor = (
            self.collection.find(stale_query, {"session_id": 1, "_id": 1})
            .sort("updated_at", -1)
            .limit(batch_size)
        )
        docs = await cursor.to_list(length=batch_size)
        # 逐个重建并统计成功数量
        rebuilt = 0
        for doc in docs:
            lookup_id = doc.get("session_id") or str(doc.get("_id"))
            if lookup_id and await self.rebuild_search_index(lookup_id):
                rebuilt += 1
        return rebuilt

    async def _find_doc(
        self,
        session_id: str,
        projection: dict[str, Any] | None = None,
    ) -> Optional[dict[str, Any]]:
        # 统一的文档查找：先按自定义 session_id，未命中再回退 ObjectId
        doc = await self.collection.find_one({"session_id": session_id}, projection)
        if doc:
            return doc
        try:
            return await self.collection.find_one({"_id": ObjectId(session_id)}, projection)
        except Exception:
            return None

    async def _update_doc(
        self,
        session_id: str,
        update: dict[str, Any],
        expected_cas_field: str | None = None,
        expected_cas_value: Any = None,
    ):
        # 统一的文档更新：先按 session_id 更新，未命中再回退 ObjectId
        result = await self._update_doc_with_query(
            {"session_id": session_id},
            update,
            expected_cas_field=expected_cas_field,
            expected_cas_value=expected_cas_value,
        )
        if result.modified_count > 0:
            return result
        try:
            return await self._update_doc_with_query(
                {"_id": ObjectId(session_id)},
                update,
                expected_cas_field=expected_cas_field,
                expected_cas_value=expected_cas_value,
            )
        except Exception:
            return result

    async def _update_doc_with_query(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        expected_cas_field: str | None = None,
        expected_cas_value: Any = None,
    ):
        # 把 CAS 期望值并入查询条件：只有该字段仍等于期望值时才更新，实现乐观并发控制
        actual_query = dict(query)
        if expected_cas_field is not None:
            actual_query[expected_cas_field] = expected_cas_value
        return await self.collection.update_one(actual_query, update)

    @staticmethod
    def _get_search_index_cas(doc: dict[str, Any]) -> tuple[str | None, Any]:
        # 选取用于 CAS 比对的字段与当前值，优先用搜索索引更新时间，退化用文档更新时间
        if "search_index_updated_at" in doc:
            return "search_index_updated_at", doc.get("search_index_updated_at")
        if "updated_at" in doc:
            return "updated_at", doc.get("updated_at")
        return None, None

    async def toggle_favorite(
        self,
        session_id: str,
        user_id: str,
        favorites_project_id: str | None = None,
    ) -> Optional[Session]:
        """Toggle a session's independent favorite state."""

        # 先取回会话：优先按 session_id，失败再按 ObjectId
        session = await self.get_by_session_id(session_id)
        if not session:
            try:
                session = await self.get_by_id(session_id)
            except Exception:
                session = None

        # 校验归属，非本人会话直接拒绝
        if not session or session.user_id != user_id:
            return None

        # 读出当前收藏态并取反
        current_favorite = is_session_favorite(
            session.metadata,
            favorites_project_id,
        )
        next_favorite = not current_favorite
        update_dict: dict[str, Any] = {
            "updated_at": utc_now(),
            "metadata.is_favorite": next_favorite,
        }
        # 取消收藏且该会话正属于收藏夹项目时，一并把它移出收藏夹项目
        if (
            not next_favorite
            and favorites_project_id
            and session.metadata.get("project_id") == favorites_project_id
        ):
            update_dict["metadata.project_id"] = None

        # 写回：同样先 session_id 再回退 ObjectId，均以 user_id 限定归属
        result = await self.collection.find_one_and_update(
            {"session_id": session_id, "user_id": user_id},
            {"$set": update_dict},
            return_document=True,
        )

        if not result:
            try:
                result = await self.collection.find_one_and_update(
                    {"_id": ObjectId(session_id), "user_id": user_id},
                    {"$set": update_dict},
                    return_document=True,
                )
            except Exception:
                return None

        if not result:
            return None

        return self._build_session(result, favorites_project_id)

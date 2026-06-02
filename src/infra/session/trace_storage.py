"""
Trace Storage - 按 trace 聚合事件存储

将同一 trace_id 的所有事件聚合到一条 MongoDB 文档中，
大幅减少文档数量，同时保留完整的事件上下文。

数据结构:
{
    "trace_id": "xxx",
    "session_id": "xxx",
    "run_id": "xxx",
    "agent_id": "xxx",
    "user_id": "xxx",
    "events": [
        {"seq": 1, "event_type": "message:chunk", "data": {...}, "timestamp": ...},
        {"seq": 2, "event_type": "thinking", "data": {...}, "timestamp": ...},
    ],
    "event_count": 2,
    "started_at": ISODate,
    "updated_at": ISODate,
    "completed_at": ISODate,
    "status": "running" | "completed" | "error",
    "metadata": {}
}

全局序号说明:
- 每个 session 有一个独立的递增序号计数器 (存储在 session_events_counter 集合)
- 每个事件写入时获取全局序号，用于断点续读
"""

import asyncio
from typing import Any, Dict, List, Optional

from src.infra.logging import get_logger
from src.infra.storage.mongodb import get_mongo_client
from src.infra.utils.datetime import utc_now, utc_now_iso
from src.kernel.config import settings

logger = get_logger(__name__)

_SESSION_EVENTS_BATCH_SIZE = 200
SESSION_EVENT_FILTER_LIST_LIMIT = 100
TRACE_EVENTS_DEFAULT_LIMIT = 1000
TRACE_EVENTS_READ_LIMIT = 5000
TRACE_LIST_LIMIT = 100


def _get_session_event_read_default_limit() -> int:
    configured = max(int(getattr(settings, "SESSION_EVENT_READ_DEFAULT_LIMIT", 1000) or 0), 1)
    return min(configured, TRACE_EVENTS_READ_LIMIT)


def _clamp_positive_int(value: int | None, *, default: int, maximum: int) -> int:
    try:
        candidate = int(value if value is not None else default)
    except (TypeError, ValueError):
        candidate = default
    return min(max(candidate, 1), maximum)


def _clamp_event_read_limit(value: int | None, *, default: int) -> int:
    try:
        candidate = int(value if value is not None else default)
    except (TypeError, ValueError):
        candidate = default
    if candidate <= 0:
        return 0
    return min(candidate, TRACE_EVENTS_READ_LIMIT)


def _clamp_nonnegative_int(value: int | None) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _bounded_unique_strings(
    values: Optional[List[str]],
    limit: int = SESSION_EVENT_FILTER_LIST_LIMIT,
) -> List[str]:
    if not values:
        return []
    bounded: List[str] = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        bounded.append(value)
        if len(bounded) >= limit:
            break
    return bounded


class TraceStorage:
    """
    Trace 存储类

    按 trace_id 聚合事件，使用 MongoDB $push 追加事件到数组。
    写入时按 Redis 顺序追加，读取时按 started_at 排序后合并。
    """

    def __init__(self):
        self._collection = None
        self._merger = None  # 事件合并器

    @property
    def collection(self):
        """延迟加载 MongoDB 集合"""
        if self._collection is None:
            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db[settings.MONGODB_TRACES_COLLECTION]
            # 索引创建在首次异步操作时触发，避免在 property getter 中调用 create_task
        return self._collection

    async def ensure_indexes_if_needed(self):
        """确保索引存在（由首次使用时调用）"""
        if not hasattr(self, "_indexes_ensured"):
            self._indexes_ensured = True
            task = asyncio.create_task(self._ensure_indexes())
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            # 启动事件合并器
            self._start_merger()

    async def _ensure_indexes(self):
        """确保必要的索引存在"""
        if self._collection is None:
            return
        try:
            # 复合索引：用于 get_session_events 查询
            # 查询模式: session_id + status (可选) + sort by started_at
            # 把 status 放在 session_id 后面、started_at 前面，使排序能利用索引
            await self._collection.create_index(
                [("session_id", 1), ("status", 1), ("started_at", 1)],
                name="session_status_started_at_idx",
                background=True,
            )
            # 复合索引：用于按 run_id 查询
            await self._collection.create_index(
                [("session_id", 1), ("run_id", 1), ("status", 1)],
                name="session_run_status_idx",
                background=True,
            )
            # 唯一索引：trace_id
            await self._collection.create_index(
                [("trace_id", 1)],
                unique=True,
                name="trace_id_unique_idx",
                background=True,
            )
            # 索引：用于按时间排序列出 traces
            await self._collection.create_index(
                [("started_at", -1)],
                name="started_at_idx",
                background=True,
            )
            # 复合索引：用于列表页 run 摘要查询
            await self._collection.create_index(
                [("session_id", 1), ("started_at", -1)],
                name="session_started_at_desc_idx",
                background=True,
            )
            # 索引：用于 EventMerger 查询未合并的已完成 traces
            await self._collection.create_index(
                [("status", 1), ("metadata.merged", 1)],
                name="status_merged_idx",
                background=True,
            )
            logger.info("MongoDB indexes ensured for trace_storage")
        except Exception as e:
            logger.warning(f"Failed to create indexes (non-critical): {e}")

    def _start_merger(self):
        """启动事件合并器"""
        if not settings.ENABLE_EVENT_MERGER:
            logger.info("EventMerger disabled by configuration")
            return

        if self._merger is None:
            try:
                from src.infra.session.event_merger import get_event_merger

                self._merger = get_event_merger(self)
                self._merger.start()
                logger.info("EventMerger started successfully")
            except Exception as e:
                logger.warning(f"Failed to start EventMerger: {e}")

    async def create_trace(
        self,
        trace_id: str,
        session_id: str,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        创建 trace 文档（幂等：若已存在则跳过）

        Args:
            trace_id: 唯一 trace 标识
            session_id: 会话 ID
            agent_id: Agent ID
            run_id: 运行 ID
            user_id: 用户 ID
            metadata: 额外元数据

        Returns:
            是否创建成功（已存在也返回 True）
        """
        from pymongo.errors import DuplicateKeyError

        now = utc_now()
        doc: Dict[str, Any] = {
            "trace_id": trace_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "run_id": run_id,
            "user_id": user_id,
            "events": [],
            "event_count": 0,
            "started_at": now,
            "updated_at": now,
            "status": "running",
            "metadata": metadata or {},
        }

        try:
            result = await self.collection.insert_one(doc)
            logger.info(
                f"Created trace {trace_id} for session {session_id}, inserted_id={result.inserted_id}"
            )
            return True
        except DuplicateKeyError:
            # Trace already exists (e.g., queued path created it before dequeue)
            logger.debug("Trace %s already exists, skipping", trace_id)
            return True
        except Exception as e:
            logger.error(f"Failed to create trace {trace_id}: {e}")
            import traceback

            traceback.print_exc()
            return False

    async def append_event(
        self,
        trace_id: str,
        event_type: str,
        data: Dict[str, Any],
    ) -> bool:
        """
        追加事件到 trace

        使用 $push 和 $inc 原子操作，保证一致性。

        Args:
            trace_id: Trace ID
            event_type: 事件类型
            data: 事件数据

        Returns:
            是否追加成功
        """
        try:
            result = await self.collection.update_one(
                {"trace_id": trace_id},
                {
                    "$push": {
                        "events": {
                            "event_type": event_type,
                            "data": data,
                            "timestamp": utc_now(),
                        }
                    },
                    "$inc": {"event_count": 1},
                    "$set": {"updated_at": utc_now()},
                },
            )
            if result.modified_count == 0:
                logger.warning(f"append_event: trace {trace_id} not found or not modified")
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Failed to append event to trace {trace_id}: {e}")
            return False

    async def _ensure_token_usage_event(self, trace_id: str) -> None:
        """Insert a zero token usage event before done when a trace has no usage event yet."""
        now = utc_now()
        usage_event = {
            "event_type": "token:usage",
            "data": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "duration": 0.0,
                "timestamp": utc_now_iso(),
            },
            "timestamp": now,
        }
        try:
            await self.collection.update_one(
                {
                    "trace_id": trace_id,
                    "events.event_type": {"$ne": "token:usage"},
                },
                [
                    {
                        "$set": {
                            "events": {
                                "$let": {
                                    "vars": {
                                        "done_index": {
                                            "$indexOfArray": ["$events.event_type", "done"]
                                        }
                                    },
                                    "in": {
                                        "$cond": [
                                            {"$gte": ["$$done_index", 0]},
                                            {
                                                "$concatArrays": [
                                                    {"$slice": ["$events", 0, "$$done_index"]},
                                                    [usage_event],
                                                    {
                                                        "$slice": [
                                                            "$events",
                                                            "$$done_index",
                                                            {
                                                                "$subtract": [
                                                                    {"$size": "$events"},
                                                                    "$$done_index",
                                                                ]
                                                            },
                                                        ]
                                                    },
                                                ]
                                            },
                                            {"$concatArrays": ["$events", [usage_event]]},
                                        ]
                                    },
                                }
                            },
                            "event_count": {"$add": [{"$ifNull": ["$event_count", 0]}, 1]},
                            "updated_at": now,
                        }
                    }
                ],
            )
        except Exception as e:
            logger.warning("Failed to ensure token usage event for trace %s: %s", trace_id, e)

    async def complete_trace(
        self,
        trace_id: str,
        status: str = "completed",
        metadata: Optional[Dict[str, Any]] = None,
        ensure_token_usage: bool = True,
    ) -> bool:
        """
        标记 trace 完成

        Args:
            trace_id: Trace ID
            status: 最终状态 (completed/error)
            metadata: 额外元数据

        Returns:
            是否更新成功
        """
        update = {
            "$set": {
                "status": status,
                "completed_at": utc_now(),
                "updated_at": utc_now(),
            }
        }
        if metadata:
            for key, value in metadata.items():
                update["$set"][f"metadata.{key}"] = value

        try:
            if ensure_token_usage:
                await self._ensure_token_usage_event(trace_id)
            result = await self.collection.update_one(
                {"trace_id": trace_id},
                update,
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Failed to complete trace {trace_id}: {e}")
            return False

    async def get_trace(
        self,
        trace_id: str,
        *,
        include_events: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        获取 trace 摘要，默认不加载大 events 数组。

        Args:
            trace_id: Trace ID
            include_events: 是否返回完整 events 数组

        Returns:
            trace 文档或 None
        """
        try:
            projection = {"_id": 0} if include_events else {"_id": 0, "events": 0}
            doc = await self.collection.find_one(
                {"trace_id": trace_id},
                projection,
            )
            return doc
        except Exception as e:
            logger.error(f"Failed to get trace {trace_id}: {e}")
            return None

    async def get_trace_events(
        self,
        trace_id: str,
        event_types: Optional[List[str]] = None,
        max_events: int = TRACE_EVENTS_DEFAULT_LIMIT,
    ) -> List[Dict[str, Any]]:
        """
        获取 trace 的事件列表

        Args:
            trace_id: Trace ID
            event_types: 可选的事件类型过滤
            max_events: 最大返回事件数，防止一次读取超大 trace

        Returns:
            事件列表
        """
        max_events = _clamp_event_read_limit(
            max_events,
            default=TRACE_EVENTS_DEFAULT_LIMIT,
        )
        if max_events <= 0:
            return []

        pipeline: List[Dict[str, Any]] = [
            {"$match": {"trace_id": trace_id}},
            {
                "$project": {
                    "events.event_type": 1,
                    "events.data": 1,
                    "events.timestamp": 1,
                }
            },
            {"$unwind": "$events"},
        ]
        if event_types:
            pipeline.append({"$match": {"events.event_type": {"$in": event_types}}})
        pipeline.append({"$limit": max_events})
        pipeline.append(
            {
                "$project": {
                    "_id": 0,
                    "event_type": "$events.event_type",
                    "data": "$events.data",
                    "timestamp": "$events.timestamp",
                }
            }
        )

        events: List[Dict[str, Any]] = []
        try:
            async for event in self.collection.aggregate(pipeline):
                events.append(event)
            return events
        except Exception as e:
            logger.error(f"Failed to get trace events for {trace_id}: {e}")
            return []

    async def get_first_trace_event(
        self,
        trace_id: str,
        event_types: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch the first matching event from one trace without loading the full events array."""
        pipeline: List[Dict[str, Any]] = [
            {"$match": {"trace_id": trace_id}},
            {
                "$project": {
                    "events.event_type": 1,
                    "events.data": 1,
                    "events.timestamp": 1,
                }
            },
            {"$unwind": "$events"},
        ]
        if event_types:
            pipeline.append({"$match": {"events.event_type": {"$in": event_types}}})
        pipeline.extend(
            [
                {"$limit": 1},
                {
                    "$project": {
                        "_id": 0,
                        "event_type": "$events.event_type",
                        "data": "$events.data",
                        "timestamp": "$events.timestamp",
                    }
                },
            ]
        )

        try:
            async for event in self.collection.aggregate(pipeline):
                return event
            return None
        except Exception as e:
            logger.error(f"Failed to get first trace event for {trace_id}: {e}")
            return None

    async def get_last_trace_event(
        self,
        trace_id: str,
        event_types: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch the latest matching event from one trace without returning the full events array."""
        pipeline: List[Dict[str, Any]] = [
            {"$match": {"trace_id": trace_id}},
            {
                "$project": {
                    "events.event_type": 1,
                    "events.data": 1,
                    "events.timestamp": 1,
                    "events.seq": 1,
                }
            },
            {"$unwind": "$events"},
        ]
        if event_types:
            pipeline.append({"$match": {"events.event_type": {"$in": event_types}}})
        pipeline.extend(
            [
                {"$sort": {"events.seq": -1, "events.timestamp": -1}},
                {"$limit": 1},
                {
                    "$project": {
                        "_id": 0,
                        "event_type": "$events.event_type",
                        "data": "$events.data",
                        "timestamp": "$events.timestamp",
                    }
                },
            ]
        )

        try:
            async for event in self.collection.aggregate(pipeline):
                return event
            return None
        except Exception as e:
            logger.error(f"Failed to get last trace event for {trace_id}: {e}")
            return None

    async def list_traces(
        self,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        skip: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        列出 traces

        Args:
            session_id: 按会话过滤
            user_id: 按用户过滤
            agent_id: 按 Agent 过滤
            status: 按状态过滤
            limit: 最大数量
            skip: 跳过数量

        Returns:
            trace 列表（不含 events 数组，仅摘要）
        """
        limit = _clamp_positive_int(limit, default=50, maximum=TRACE_LIST_LIMIT)
        skip = _clamp_nonnegative_int(skip)
        query = {}
        if session_id:
            query["session_id"] = session_id
        if user_id:
            query["user_id"] = user_id
        if agent_id:
            query["agent_id"] = agent_id
        if status:
            query["status"] = status

        try:
            cursor = (
                self.collection.find(
                    query,
                    {
                        "_id": 0,
                        "events": 0,  # 排除大数组
                    },
                )
                .sort("started_at", -1)
                .skip(skip)
                .limit(limit)
            )
            return await cursor.to_list(length=limit)
        except Exception as e:
            logger.error(f"Failed to list traces: {e}")
            return []

    async def list_run_summaries(
        self,
        session_id: str,
        limit: int = 50,
        skip: int = 0,
        trace_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出会话 run 摘要，并只投影第一条用户消息事件。"""
        limit = _clamp_positive_int(limit, default=50, maximum=TRACE_LIST_LIMIT)
        skip = _clamp_nonnegative_int(skip)
        query = {"session_id": session_id}
        if trace_id:
            query["trace_id"] = trace_id

        projection: Dict[str, Any] = {
            "_id": 0,
            "run_id": 1,
            "trace_id": 1,
            "agent_id": 1,
            "started_at": 1,
            "completed_at": 1,
            "status": 1,
            "event_count": 1,
            "events": {"$elemMatch": {"event_type": "user:message"}},
        }

        try:
            cursor = (
                self.collection.find(query, projection)
                .sort("started_at", -1)
                .skip(skip)
                .limit(limit)
            )
            traces = await cursor.to_list(length=limit)
            summaries: List[Dict[str, Any]] = []
            for trace in traces:
                user_message = None
                events = trace.get("events") or []
                if events:
                    data = events[0].get("data", {})
                    user_message = data.get("content") or data.get("message") or ""
                    if user_message and len(user_message) > 20:
                        user_message = user_message[:17] + "..."

                summaries.append(
                    {
                        "run_id": trace.get("run_id"),
                        "trace_id": trace.get("trace_id"),
                        "agent_id": trace.get("agent_id"),
                        "started_at": trace.get("started_at"),
                        "completed_at": trace.get("completed_at"),
                        "status": trace.get("status"),
                        "event_count": trace.get("event_count", 0),
                        "user_message": user_message,
                    }
                )
            return summaries
        except Exception as e:
            logger.error(f"Failed to list run summaries: {e}")
            return []

    async def get_session_events(
        self,
        session_id: str,
        event_types: Optional[List[str]] = None,
        run_id: Optional[str] = None,
        exclude_run_id: Optional[str] = None,
        completed_only: bool = True,
        run_ids: Optional[List[str]] = None,
        max_events: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取会话的所有事件（跨 traces 聚合）

        按 run 顺序（started_at）合并事件，每个 run 内的事件保持原有顺序。

        Args:
            session_id: 会话 ID
            event_types: 可选的事件类型过滤列表
            run_id: 可选的运行 ID 过滤（用于隔离多轮对话）
            exclude_run_id: 可选的运行 ID 排除（用于排除正在运行的 run）
            completed_only: 是否只返回成功完成的 trace 中的事件（默认 True）
            run_ids: 可选的运行 ID 列表过滤（用于部分分享等场景）
            max_events: 可选的最大返回事件数

        Returns:
            事件列表，按 run 顺序合并
        """
        try:
            event_types = _bounded_unique_strings(event_types, SESSION_EVENT_FILTER_LIST_LIMIT)
            run_ids = _bounded_unique_strings(run_ids, SESSION_EVENT_FILTER_LIST_LIMIT)
            # 构建查询条件
            match_query: Dict[str, Any] = {"session_id": session_id}
            if run_ids:
                match_query["run_id"] = {"$in": run_ids}
            elif run_id:
                match_query["run_id"] = run_id
            if exclude_run_id:
                match_query["run_id"] = {"$ne": exclude_run_id}
            # 排除正在运行的 trace（只返回 running 状态以外的）
            if completed_only:
                match_query["status"] = {"$ne": "running"}

            if max_events is None:
                max_events = _get_session_event_read_default_limit()
            else:
                max_events = _clamp_event_read_limit(
                    max_events,
                    default=_get_session_event_read_default_limit(),
                )

            if max_events <= 0:
                return []

            pipeline: List[Dict[str, Any]] = [
                {"$match": match_query},
                {"$sort": {"started_at": 1}},
                {
                    "$project": {
                        "trace_id": 1,
                        "run_id": 1,
                        "events.event_type": 1,
                        "events.data": 1,
                        "events.timestamp": 1,
                    }
                },
                {"$unwind": "$events"},
            ]
            if event_types:
                pipeline.append({"$match": {"events.event_type": {"$in": event_types}}})
            pipeline.extend(
                [
                    {"$limit": max_events},
                    {
                        "$project": {
                            "_id": 0,
                            "trace_id": 1,
                            "run_id": 1,
                            "event_type": "$events.event_type",
                            "data": "$events.data",
                            "timestamp": "$events.timestamp",
                        }
                    },
                ]
            )

            events: List[Dict[str, Any]] = []
            async for event in self.collection.aggregate(pipeline):
                events.append(event)
            logger.debug(
                f"Session {session_id} (run_id={run_id}) returned {len(events)} bounded events"
            )
            return events
        except Exception as e:
            logger.error(f"Failed to get session events: {e}")
            return []

    async def get_run_events(
        self,
        session_id: str,
        run_id: str,
        event_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取特定 run 的事件

        Args:
            session_id: 会话 ID
            run_id: 运行 ID
            event_types: 可选的事件类型过滤列表

        Returns:
            事件列表，按写入顺序
        """
        return await self.get_session_events(session_id, event_types, run_id=run_id)

    async def delete_trace(self, trace_id: str) -> bool:
        """删除 trace"""
        try:
            result = await self.collection.delete_one({"trace_id": trace_id})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Failed to delete trace {trace_id}: {e}")
            return False

    async def delete_session_traces(self, session_id: str) -> int:
        """删除会话的所有 traces"""
        try:
            result = await self.collection.delete_many({"session_id": session_id})
            return result.deleted_count
        except Exception as e:
            logger.error(f"Failed to delete session traces: {e}")
            return 0


# Singleton
_trace_storage: Optional[TraceStorage] = None


def get_trace_storage() -> TraceStorage:
    """获取 TraceStorage 单例"""
    global _trace_storage
    if _trace_storage is None:
        _trace_storage = TraceStorage()
    return _trace_storage

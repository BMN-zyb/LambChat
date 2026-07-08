"""Chunked trace event storage helpers for TraceStorage."""

from typing import Any, Dict, List, Optional

from pymongo import ReturnDocument

from src.infra.logging import get_logger
from src.infra.session import trace_storage as trace_storage_helpers
from src.infra.utils.datetime import utc_now

logger = get_logger(__name__)


# 分块存储 Mixin：给 TraceStorage 提供"把一个 trace 的事件拆到多条 chunk 文档"的能力
# 设计动机：单个 trace 的 events 数组可能撑爆 Mongo 单文档 16MB 上限，
# 因此按固定 chunk_size 把事件切成多条独立 chunk 文档存储，读时再按序号拼回
class TraceEventChunkMixin:
    @property
    def collection(self) -> Any:
        # 由宿主类提供 traces 主集合
        raise NotImplementedError

    @property
    def chunks_collection(self) -> Any:
        # 由宿主类提供事件分块集合
        raise NotImplementedError

    async def _has_event_chunks(self, trace_id: str) -> bool:
        # 探测该 trace 是否已启用分块存储（分块集合中是否存在其文档）
        try:
            chunk = await self.chunks_collection.find_one({"trace_id": trace_id}, {"_id": 1})
            return chunk is not None
        except Exception as e:
            logger.debug("Failed to probe trace event chunks for %s: %s", trace_id, e)
            return False

    async def read_trace_events_compat(
        self,
        trace_id: str,
        event_types: Optional[List[str]] = None,
        max_events: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Read trace events from chunks when present, otherwise legacy traces.events."""
        # 兼容读取：优先从分块集合按序号拼回，没有分块则回退读 legacy 的 events 数组
        # 归一化事件类型过滤列表（去重 + 限长）
        event_types = trace_storage_helpers._bounded_unique_strings(
            event_types,
            trace_storage_helpers.SESSION_EVENT_FILTER_LIST_LIMIT,
        )
        allowed_types = set(event_types)
        if max_events is not None:
            # 夹紧读取上限，<=0 直接返回空
            max_events = trace_storage_helpers._clamp_event_read_limit(
                max_events,
                default=trace_storage_helpers.TRACE_EVENTS_DEFAULT_LIMIT,
            )
            if max_events <= 0:
                return []

        # 类型过滤谓词：无过滤列表时全部接受
        def _accepts(event: Dict[str, Any]) -> bool:
            return not allowed_types or event.get("event_type") in allowed_types

        events: List[Dict[str, Any]] = []
        if await self._has_event_chunks(trace_id):
            # 找到分块中最靠前的一块，确定分块从哪个 seq 开始
            first_chunk = None
            first_chunk_cursor = (
                self.chunks_collection.find(
                    {"trace_id": trace_id},
                    {"_id": 0, "start_seq": 1, "events.seq": 1},
                )
                .sort("chunk_index", 1)
                .limit(1)
            )
            async for chunk in first_chunk_cursor:
                first_chunk = chunk
                break
            first_chunk_start_seq = 1
            if first_chunk:
                # 优先用 start_seq，缺失则取块内最小事件 seq 兜底
                first_chunk_start_seq = int(
                    first_chunk.get("start_seq")
                    or min(
                        (
                            trace_storage_helpers._event_seq(event, index + 1)
                            for index, event in enumerate(first_chunk.get("events", []) or [])
                        ),
                        default=1,
                    )
                )
            # 迁移期兼容：分块起始 seq>1 说明前半段事件仍在 legacy events 里，先补读它们
            if first_chunk_start_seq > 1:
                trace_doc = await self.collection.find_one(
                    {"trace_id": trace_id},
                    {"_id": 0, "events": 1},
                )
                for index, event in enumerate((trace_doc or {}).get("events", []) or [], start=1):
                    # 只取 seq 小于分块起点的 legacy 事件，避免与分块重复
                    if trace_storage_helpers._event_seq(event, index) >= first_chunk_start_seq:
                        continue
                    if not _accepts(event):
                        continue
                    events.append(event)
                    if max_events is not None and len(events) >= max_events:
                        return events

            # 再按 chunk_index 升序逐块读取，块内按 seq 排序，保证全局有序
            cursor = self.chunks_collection.find(
                {"trace_id": trace_id},
                {"_id": 0, "events": 1, "chunk_index": 1},
            ).sort("chunk_index", 1)
            async for chunk in cursor:
                chunk_events = sorted(
                    enumerate(chunk.get("events", []) or []),
                    key=lambda item: trace_storage_helpers._event_seq(item[1], item[0]),
                )
                for _index, event in chunk_events:
                    if not _accepts(event):
                        continue
                    events.append(event)
                    if max_events is not None and len(events) >= max_events:
                        return events
            return events

        # 无分块：直接读 legacy events 数组
        trace_doc = await self.collection.find_one(
            {"trace_id": trace_id},
            {"_id": 0, "events": 1},
        )
        for event in (trace_doc or {}).get("events", []) or []:
            if not _accepts(event):
                continue
            events.append(event)
            if max_events is not None and len(events) >= max_events:
                break
        return events

    async def replace_trace_events_with_chunks(
        self,
        trace_doc: Dict[str, Any],
        events: List[Dict[str, Any]],
        *,
        mark_storage_chunked: bool = True,
        remove_legacy_events: bool = True,
    ) -> None:
        """Replace all chunk docs for one trace with normalized event chunks."""
        # 用一批规范化事件整体重建某 trace 的所有 chunk 文档（常用于合并后重写）
        trace_id = str(trace_doc.get("trace_id") or "")
        if not trace_id:
            return

        now = utc_now()
        chunk_size = trace_storage_helpers._get_event_chunk_size()
        # 给每条事件重新编号 seq（从 1 递增），作为跨块的全局有序依据
        normalized_events: List[Dict[str, Any]] = []
        for index, event in enumerate(events, start=1):
            normalized_event = dict(event)
            normalized_event["seq"] = index
            normalized_events.append(normalized_event)

        # 先删除旧分块，再整体重建，保证结果与传入事件一致
        await self.chunks_collection.delete_many({"trace_id": trace_id})

        # 按 chunk_size 切片生成分块文档，记录每块的起止 seq 便于按序读取
        chunk_docs: List[Dict[str, Any]] = []
        for start in range(0, len(normalized_events), chunk_size):
            chunk_events = normalized_events[start : start + chunk_size]
            start_seq = int(chunk_events[0]["seq"])
            end_seq = int(chunk_events[-1]["seq"])
            chunk_docs.append(
                {
                    "trace_id": trace_id,
                    "session_id": trace_doc.get("session_id", ""),
                    "run_id": trace_doc.get("run_id", ""),
                    "trace_started_at": trace_doc.get("started_at"),
                    "chunk_index": trace_storage_helpers._event_chunk_index(start_seq),
                    "start_seq": start_seq,
                    "end_seq": end_seq,
                    "event_count": len(chunk_events),
                    "events": chunk_events,
                    "created_at": now,
                    "updated_at": now,
                }
            )

        if chunk_docs:
            await self.chunks_collection.insert_many(chunk_docs)

        # 在主 trace 文档上更新汇总字段（事件数、块数、首尾预览等），供列表展示无需读分块
        first_user_message = next(
            (event for event in normalized_events if event.get("event_type") == "user:message"),
            None,
        )
        update_fields: Dict[str, Any] = {
            "event_count": len(normalized_events),
            "chunk_count": len(chunk_docs),
            "first_event_preview": trace_storage_helpers._event_preview(
                normalized_events[0] if normalized_events else None
            ),
            "first_user_message_preview": trace_storage_helpers._event_preview(first_user_message),
            "last_event_preview": trace_storage_helpers._event_preview(
                normalized_events[-1] if normalized_events else None
            ),
            "updated_at": now,
        }
        # 标记该 trace 已切换到分块存储
        if mark_storage_chunked:
            update_fields["metadata.event_storage"] = "chunked"

        update_doc: Dict[str, Any] = {"$set": update_fields}
        # 事件已迁到分块后，移除主文档中冗余的 legacy events 数组
        if remove_legacy_events:
            update_doc["$unset"] = {"events": ""}

        await self.collection.update_one(
            {"trace_id": trace_id},
            update_doc,
        )

    async def reserve_event_sequence_range(
        self,
        trace_id: str,
        event_count: int,
    ) -> Optional[Dict[str, Any]]:
        """Atomically reserve a contiguous event seq range by incrementing event_count."""
        # 通过原子 $inc event_count 预留一段连续序号，避免并发写入时 seq 冲突
        if event_count <= 0:
            return await self.collection.find_one({"trace_id": trace_id}, {"_id": 0})
        now = utc_now()
        # 返回自增后的文档，调用方据此反推本批事件的起始 seq
        return await self.collection.find_one_and_update(
            {"trace_id": trace_id},
            {
                "$inc": {"event_count": event_count},
                "$set": {"updated_at": now},
            },
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )

    async def append_events_to_chunks(
        self,
        trace_doc: Dict[str, Any],
        events: List[Dict[str, Any]],
        start_seq: int,
    ) -> None:
        """Append a reserved event batch to chunk documents."""
        # 把已预留起始序号的一批事件按序号归入对应分块（可跨多个 chunk）
        trace_id = str(trace_doc.get("trace_id") or "")
        if not trace_id or not events:
            return

        now = utc_now()
        # 按 chunk_index 分组：每条事件根据 seq 落到所属分块
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for offset, event in enumerate(events):
            seq = start_seq + offset
            normalized_event = dict(event)
            normalized_event["seq"] = seq
            grouped.setdefault(
                trace_storage_helpers._event_chunk_index(seq),
                [],
            ).append(normalized_event)

        for chunk_index in sorted(grouped):
            chunk_events = grouped[chunk_index]
            start = int(chunk_events[0]["seq"])
            end = int(chunk_events[-1]["seq"])
            # 聚合管道表达式：先剔除块内落在 [start,end] 区间的旧事件，
            # 保证同一段序号重试写入时幂等（不会重复堆积）
            existing_events_without_range = {
                "$filter": {
                    "input": {"$ifNull": ["$events", []]},
                    "as": "event",
                    "cond": {
                        "$not": [
                            {
                                "$and": [
                                    {"$gte": [{"$ifNull": ["$$event.seq", 0]}, start]},
                                    {"$lte": [{"$ifNull": ["$$event.seq", 0]}, end]},
                                ]
                            }
                        ]
                    },
                }
            }
            # 用聚合管道式 update 原子地：合并事件、更新起止 seq、重算 event_count；不存在则 upsert
            await self.chunks_collection.update_one(
                {"trace_id": trace_id, "chunk_index": chunk_index},
                [
                    {
                        "$set": {
                            "trace_id": trace_id,
                            "session_id": trace_doc.get("session_id", ""),
                            "run_id": trace_doc.get("run_id", ""),
                            "trace_started_at": trace_doc.get("started_at"),
                            "chunk_index": chunk_index,
                            "created_at": {"$ifNull": ["$created_at", now]},
                            "updated_at": now,
                            "start_seq": {
                                "$min": [
                                    {"$ifNull": ["$start_seq", start]},
                                    start,
                                ]
                            },
                            "end_seq": {
                                "$max": [
                                    {"$ifNull": ["$end_seq", end]},
                                    end,
                                ]
                            },
                            # 保留区间外旧事件 + 追加本批新事件
                            "events": {
                                "$concatArrays": [
                                    existing_events_without_range,
                                    chunk_events,
                                ]
                            },
                        }
                    },
                    {"$set": {"event_count": {"$size": "$events"}}},
                ],
                upsert=True,
            )

        # 更新主 trace 文档：更新时间、存储模式、块数（取最大）
        end_seq = start_seq + len(events) - 1
        update_fields: Dict[str, Any] = {
            "updated_at": now,
            "metadata.event_storage": "chunked",
        }
        # 仅在写入首条事件时刷新首事件预览
        if start_seq == 1:
            update_fields["first_event_preview"] = trace_storage_helpers._event_preview(events[0])
        if start_seq == 1:
            # 首批里若含首条用户消息，记录其预览（用于会话列表展示）
            first_user_message = next(
                (event for event in events if event.get("event_type") == "user:message"),
                None,
            )
            if first_user_message is not None:
                update_fields["first_user_message_preview"] = trace_storage_helpers._event_preview(
                    first_user_message
                )

        await self.collection.update_one(
            {"trace_id": trace_id},
            {
                "$set": update_fields,
                "$max": {"chunk_count": max(grouped) + 1},
            },
        )
        # 仅当本批确实是目前最靠后的事件时才更新"最后事件预览"，避免乱序覆盖
        await self.collection.update_one(
            {
                "trace_id": trace_id,
                "$or": [
                    {"event_count": {"$lte": end_seq}},
                    {"event_count": {"$exists": False}},
                ],
            },
            {
                "$set": {
                    "last_event_preview": trace_storage_helpers._event_preview(events[-1]),
                    "updated_at": now,
                }
            },
        )

    async def rollback_event_sequence_range(
        self,
        trace_doc: Dict[str, Any],
        start_seq: int,
        event_count: int,
    ) -> None:
        """Undo a reserved chunk sequence range after a failed append attempt."""
        # 追加失败时回滚：从相关分块拉掉这段序号的事件，并把预留的 event_count 扣回
        trace_id = str(trace_doc.get("trace_id") or "")
        event_count = max(int(event_count or 0), 0)
        if not trace_id or event_count <= 0:
            return

        now = utc_now()
        try:
            reserved_end_count = int(trace_doc.get("event_count", 0))
        except (TypeError, ValueError):
            reserved_end_count = 0
        end_seq = start_seq + event_count - 1
        chunk_size = trace_storage_helpers._get_event_chunk_size()
        start_chunk = trace_storage_helpers._event_chunk_index(start_seq)
        end_chunk = trace_storage_helpers._event_chunk_index(end_seq)
        # 逐个受影响分块，按各自与 [start_seq,end_seq] 的交集拉掉事件并回扣计数
        for chunk_index in range(start_chunk, end_chunk + 1):
            chunk_start_seq = chunk_index * chunk_size + 1
            chunk_end_seq = chunk_start_seq + chunk_size - 1
            remove_start_seq = max(start_seq, chunk_start_seq)
            remove_end_seq = min(end_seq, chunk_end_seq)
            remove_count = remove_end_seq - remove_start_seq + 1
            seq_filter = {"$gte": remove_start_seq, "$lte": remove_end_seq}
            await self.chunks_collection.update_one(
                {
                    "trace_id": trace_id,
                    "chunk_index": chunk_index,
                    "events.seq": seq_filter,
                },
                {
                    "$pull": {"events": {"seq": seq_filter}},
                    "$inc": {"event_count": -remove_count},
                    "$set": {"updated_at": now},
                },
            )
        # CAS 式回扣主文档 event_count：仅当当前值仍等于预留后的值才扣，避免与后续写入冲突
        await self.collection.update_one(
            {"trace_id": trace_id, "event_count": reserved_end_count},
            {
                "$inc": {"event_count": -event_count},
                "$set": {"updated_at": now},
            },
        )

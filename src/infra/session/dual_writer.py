"""
Dual Event Writer - 双写事件到 Redis Stream + MongoDB

所有事件按 trace_id 聚合到 MongoDB，大幅减少文档数量。
- Redis: 所有事件立即写入，保证 SSE 实时性
- MongoDB: 批量缓冲写入，确保数据不丢失

性能优化:
- 使用 bulk_write 批量更新 MongoDB，减少 DB 往返
- 分离 Redis/Mongo 锁，减少锁竞争
- 使用 asyncio.Event 替代轮询标志
"""

import asyncio
import json
import time
from collections import OrderedDict, defaultdict
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.session.trace_storage import TraceStorage, get_trace_storage
from src.infra.storage.redis import RedisStorage
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings

logger = get_logger(__name__)


# MongoDB 批量写入配置
_MONGO_FLUSH_INTERVAL = 1.0  # 每 1000ms 刷新一次
_MONGO_BATCH_SIZE = 200  # 每 200 条立即刷新
_MONGO_BUFFER_MAX = 10000  # buffer 上限，防止 MongoDB 慢/宕机时 OOM
_TTL_SET_KEYS_MAX = 5000  # _ttl_set_keys 上限，防止内存泄漏
# 实时流阻塞读取的整体超时（24 小时），防止 producer 崩溃时读端无限等待
_LIVE_STREAM_READ_TIMEOUT_SECONDS = 24 * 60 * 60
# SSE 心跳间隔（秒）：定期发心跳以探测客户端断开
_SSE_HEARTBEAT_INTERVAL_SECONDS = 15
# 单次 XREAD 阻塞等待毫秒数
_REDIS_XREAD_BLOCK_MS = 5000
# 初始回放（xrange）时每批读取的事件数
_REDIS_REPLAY_BATCH_SIZE = 500
# Mongo 缓冲区中单条事件的元组结构别名（字段随重试逐步追加）
MongoBufferItem = tuple[Any, ...]


def _get_max_events_per_trace() -> int:
    """获取单个 trace 最多保留的事件数（可配置）"""
    return getattr(settings, "SESSION_MAX_EVENTS_PER_TRACE", 50000)


def _get_mongo_buffer_max() -> int:
    # Mongo 写缓冲上限（可配置，最小 1），达到后触发强制刷新以免 OOM
    return max(int(getattr(settings, "SESSION_EVENT_MONGO_BUFFER_MAX", _MONGO_BUFFER_MAX) or 0), 1)


def _get_ttl_set_keys_max() -> int:
    # TTL 刷新缓存的最大条目数（LRU 淘汰上限），防止内存泄漏
    return max(int(getattr(settings, "SESSION_EVENT_TTL_CACHE_MAX", _TTL_SET_KEYS_MAX) or 0), 1)


def _get_ttl_refresh_interval() -> float:
    # TTL 刷新间隔取流 TTL 的一半，并夹在 [1s, 300s]，避免每次写入都重设过期时间
    ttl_seconds = max(int(getattr(settings, "SSE_CACHE_TTL", 86400) or 0), 1)
    return max(min(ttl_seconds / 2, 300.0), 1.0)


def _get_redis_replay_batch_size() -> int:
    # 回放批大小（可配置，最小 1）
    return max(
        int(
            getattr(settings, "SESSION_EVENT_REDIS_REPLAY_BATCH_SIZE", _REDIS_REPLAY_BATCH_SIZE)
            or 0
        ),
        1,
    )


async def _serialize_event_data_for_redis(data: Any) -> str:
    # 写 Redis 前把事件 data 序列化为 JSON 字符串（dict 走 json，其余转 str）
    # json.dumps 可能较重，放线程池执行避免阻塞事件循环
    if isinstance(data, dict):
        return await run_blocking_io(json.dumps, data, ensure_ascii=False)
    return str(data)


async def _parse_event_data_from_redis(data: Any) -> Any:
    # 从 Redis 读回时反序列化 JSON；非法 JSON 原样返回，非字符串直接返回
    if isinstance(data, str):
        try:
            return await run_blocking_io(json.loads, data)
        except json.JSONDecodeError:
            return data
    return data


def _is_cancel_error_event(event: dict[str, Any]) -> bool:
    # 判断是否为"取消/中断"类错误事件，这类不应视为异常终止而中断流回放
    if event.get("event_type") != "error":
        return False
    data = event.get("data")
    if not isinstance(data, dict):
        return False
    return data.get("type") in {"CancelledError", "TaskInterruptedError"}


def _should_stop_stream_on_event(event: dict[str, Any]) -> bool:
    # 判断读到该事件后是否应结束流：complete/done 结束；error 结束（取消类除外）
    event_type = event.get("event_type")
    if event_type in ("complete", "done"):
        return True
    if event_type == "error":
        return not _is_cancel_error_event(event)
    return False


def _build_mongo_bulk_operations(
    batch: list[MongoBufferItem],
    *,
    now: datetime,
    max_events: int,
) -> list[UpdateOne]:
    # 事件溯源聚合核心：把一批事件按 trace_id 分组，每组用一条 UpdateOne 追加进对应 trace 文档
    # 从而实现"同 trace_id 的众多事件聚合成一条 Mongo 文档"，大幅减少文档数量
    grouped: dict[str, list[dict]] = defaultdict(list)
    trace_context: dict[str, tuple[str, Optional[str]]] = {}

    for item in batch:
        # 跳过标记为"仅走分块存储、不写 legacy events 数组"的项
        if _buffer_item_skip_legacy(item):
            continue
        trace_id, event_type, data, session_id, run_id, timestamp = _buffer_item_base(item)
        grouped[trace_id].append(
            {
                "event_type": event_type,
                "data": data,
                "timestamp": timestamp,
            }
        )
        # 记录该 trace 的上下文（首次出现时），用于 upsert 时初始化文档
        if trace_id not in trace_context:
            trace_context[trace_id] = (session_id, run_id)

    operations: list[UpdateOne] = []
    for trace_id, events in grouped.items():
        session_id, run_id = trace_context.get(trace_id, ("", None))
        operations.append(
            UpdateOne(
                {"trace_id": trace_id},
                {
                    # $push + $slice：把事件追加到 events 数组，并只保留最后 max_events 条，防止无限增长
                    "$push": {
                        "events": {
                            "$each": events,
                            "$slice": -max_events,
                        }
                    },
                    # 累加事件总数
                    "$inc": {"event_count": len(events)},
                    "$set": {"updated_at": now},
                    # 文档首次创建（upsert）时才写入的初始字段
                    "$setOnInsert": {
                        "session_id": session_id,
                        "run_id": run_id or "",
                        "status": "running",
                        "started_at": now,
                    },
                },
                # trace 不存在则新建（首个事件即建档）
                upsert=True,
            )
        )
    return operations


def _buffer_item_base(
    item: MongoBufferItem,
) -> tuple[str, str, dict, str, Optional[str], datetime]:
    # 取缓冲项的前 6 个基础字段（trace/类型/数据/会话/run/时间戳）
    trace_id, event_type, data, session_id, run_id, timestamp = item[:6]
    return trace_id, event_type, data, session_id, run_id, timestamp


def _buffer_item_reserved_start_seq(item: MongoBufferItem) -> int | None:
    # 第 7 个字段（若有）：分块写入重试时预留的起始序号，保证顺序不乱
    if len(item) < 7 or item[6] is None:
        return None
    return int(item[6])


def _buffer_item_skip_legacy(item: MongoBufferItem) -> bool:
    # 第 8 个字段（若有）：为真表示该项已成功写入分块，不再写 legacy events 数组
    return bool(len(item) >= 8 and item[7])


def _buffer_item_skip_chunk(item: MongoBufferItem) -> bool:
    # 第 9 个字段（若有）：为真表示该项已走过分块写入，仅需回退 legacy 时不再重复写分块
    return bool(len(item) >= 9 and item[8])


def _with_chunk_retry_metadata(
    item: MongoBufferItem,
    *,
    reserved_start_seq: int,
    skip_legacy: bool,
    skip_chunk: bool = False,
) -> MongoBufferItem:
    # 给缓冲项追加重试元数据（预留序号 / 跳过 legacy / 跳过分块），用于失败后重新入队
    base = (*_buffer_item_base(item), reserved_start_seq, skip_legacy)
    if skip_chunk:
        return (*base, True)
    return base


def _group_mongo_buffer_events(
    batch: list[MongoBufferItem],
) -> dict[str, list[dict[str, Any]]]:
    # 按 trace_id 分组并抽取纯事件结构（不含预留序号等重试元数据）
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in batch:
        trace_id, event_type, data, _session_id, _run_id, timestamp = _buffer_item_base(item)
        grouped[trace_id].append(
            {
                "event_type": event_type,
                "data": data,
                "timestamp": timestamp,
            }
        )
    return grouped


def _operation_trace_id(operation: Any) -> str | None:
    # 从 UpdateOne 操作的过滤条件里反查其 trace_id（用于定位 bulk 失败项）
    try:
        return operation._filter.get("trace_id")  # type: ignore[attr-defined]
    except AttributeError:
        return None


def _failed_bulk_write_trace_ids(
    error: BulkWriteError,
    operations: list[UpdateOne],
) -> set[str] | None:
    # 从 BulkWriteError 中解析出失败操作对应的 trace_id 集合，便于只重试失败部分
    # 任一环节无法可靠定位（索引越界/缺 trace_id）则返回 None，表示需整批重试
    failed_trace_ids: set[str] = set()
    for write_error in error.details.get("writeErrors", []) or []:
        try:
            index = int(write_error.get("index"))
        except (TypeError, ValueError):
            return None
        if index < 0 or index >= len(operations):
            return None
        trace_id = _operation_trace_id(operations[index])
        if trace_id is None:
            return None
        failed_trace_ids.add(trace_id)
    return failed_trace_ids or None


def _iter_chunk_write_groups(
    batch: list[MongoBufferItem],
) -> list[tuple[str, list[MongoBufferItem], list[dict[str, Any]], int | None]]:
    # 为分块写入切分批次：把连续、同 trace 且同预留序号的项聚成一组，保持写入顺序
    groups: list[tuple[str, list[MongoBufferItem], list[dict[str, Any]], int | None]] = []
    current_trace_id: str | None = None
    current_reserved_start_seq: int | None = None
    current_items: list[MongoBufferItem] = []
    current_events: list[dict[str, Any]] = []

    # 结算当前组到结果列表并重置累积状态
    def flush_current() -> None:
        nonlocal current_trace_id, current_reserved_start_seq, current_items, current_events
        if current_trace_id is not None and current_items:
            groups.append(
                (
                    current_trace_id,
                    current_items,
                    current_events,
                    current_reserved_start_seq,
                )
            )
        current_trace_id = None
        current_reserved_start_seq = None
        current_items = []
        current_events = []

    for item in batch:
        # 已写过分块的项跳过，避免重复写
        if _buffer_item_skip_chunk(item):
            continue
        trace_id, event_type, data, _session_id, _run_id, timestamp = _buffer_item_base(item)
        reserved_start_seq = _buffer_item_reserved_start_seq(item)
        # trace 或预留序号发生变化则结算上一组
        if current_items and (
            trace_id != current_trace_id or reserved_start_seq != current_reserved_start_seq
        ):
            flush_current()
        current_trace_id = trace_id
        current_reserved_start_seq = reserved_start_seq
        current_items.append(item)
        current_events.append(
            {
                "event_type": event_type,
                "data": data,
                "timestamp": timestamp,
            }
        )
    flush_current()
    return groups


class DualEventWriter:
    """
    双写事件到 Redis Stream + MongoDB (Trace 模式)

    - Redis: 所有事件立即写入，保证 SSE 实时性
    - MongoDB: 批量缓冲写入，使用 Lock 保护，确保数据不丢失

    性能优化:
    - Redis 和 MongoDB 操作使用不同的锁，减少争用
    - 使用 asyncio.Event 替代轮询标志，避免 busy wait
    - 使用 bulk_write 批量更新 MongoDB
    """

    def __init__(self):
        self._redis = None
        self._trace = None
        # 已刷新过 TTL 的 stream key -> 下次刷新时间点（LRU 有序字典，控制内存）
        self._ttl_set_keys: OrderedDict[str, float] = OrderedDict()
        # MongoDB 批量写入缓冲
        # (trace_id, event_type, data, session_id, run_id, timestamp)
        self._mongo_buffer: list[MongoBufferItem] = []
        self._mongo_lock = asyncio.Lock()  # 只保护 buffer 和 flush 操作
        self._flush_event = asyncio.Event()  # 使用 Event 替代轮询标志
        self._flush_event.set()  # 初始状态为已就绪
        # 当前延迟刷新任务句柄
        self._flush_task: asyncio.Task[None] | None = None
        # 延迟刷新任务是否处于"等待间隔"阶段（用于安全取消/抢占刷新）
        self._flush_task_waiting = False
        # 因缓冲满被丢弃的事件累计数与最近一次丢弃信息（诊断用）
        self._mongo_buffer_dropped_total = 0
        self._mongo_buffer_last_drop: dict[str, Any] | None = None

    @property
    def redis(self) -> RedisStorage:
        # 延迟初始化 Redis 存储客户端
        if self._redis is None:
            self._redis = RedisStorage()
        return self._redis

    @property
    def trace(self) -> TraceStorage:
        # 延迟获取 trace 存储（事件溯源持久化层）单例
        if self._trace is None:
            self._trace = get_trace_storage()
        return self._trace

    def _stream_key(self, session_id: str, run_id: Optional[str] = None) -> str:
        # 计算 Redis Stream 的 key：带 run_id 时按 run 隔离，否则退化为会话级
        if run_id:
            return f"session:{session_id}:run:{run_id}:events"
        return f"session:{session_id}:events"

    async def create_trace(
        self,
        trace_id: str,
        session_id: str,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        # 直接委托给 trace 存储创建 trace 文档
        return await self.trace.create_trace(
            trace_id=trace_id,
            session_id=session_id,
            agent_id=agent_id,
            run_id=run_id,
            user_id=user_id,
            metadata=metadata,
        )

    async def write_event(
        self,
        session_id: str,
        event_type: str,
        data: Dict[str, Any],
        trace_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> bool:
        """
        双写事件到 Redis + MongoDB

        - Redis: 立即写入（无锁）
        - MongoDB: 缓冲写入，批量刷新（使用 Event 触发）
        """
        # 统一时间戳，确保 Redis 和 MongoDB 使用相同的时间
        timestamp = utc_now()

        # ---- Redis 写入（立即，无锁） ----
        # 即时写入保证 SSE 实时性：事件先进 Redis Stream，前端立刻可读
        stream_key = self._stream_key(session_id, run_id)
        fields = {
            "event_type": event_type,
            "data": await _serialize_event_data_for_redis(data),
            "timestamp": timestamp.isoformat(),
        }
        redis_success = await self._write_to_redis_direct(stream_key, fields)

        # ---- MongoDB 写入（缓冲，使用 Event 触发） ----
        # 缓冲 + 批量落库保证不丢：先入内存缓冲，再按批量/间隔/终态刷新到 Mongo
        if trace_id:
            mongo_buffer_max = _get_mongo_buffer_max()
            async with self._mongo_lock:
                buffer_size = len(self._mongo_buffer)
            # 缓冲已满：先强制刷一次再继续接收，避免无界堆积
            if buffer_size >= mongo_buffer_max:
                logger.warning(
                    "MongoDB event buffer reached %s entries; flushing before accepting more",
                    mongo_buffer_max,
                )
                await self.flush_mongo_buffer()

            should_flush_now = False
            buffer_size = 0
            async with self._mongo_lock:
                buffer_size = len(self._mongo_buffer)
                # 当缓冲区达到 80% 时发出警告
                if buffer_size >= int(mongo_buffer_max * 0.8):
                    logger.warning(
                        f"MongoDB buffer at {buffer_size}/{mongo_buffer_max} ({buffer_size * 100 // mongo_buffer_max}%). "
                        f"Consider checking MongoDB performance."
                    )
                # 事件入缓冲（六元组基础形态）
                self._mongo_buffer.append(
                    (trace_id, event_type, data, session_id, run_id, timestamp)
                )
                # 达到批量大小立即刷新
                if len(self._mongo_buffer) >= _MONGO_BATCH_SIZE:
                    should_flush_now = True
                # 使用 Event 触发延迟刷新
                # 若当前没有在等待的刷新任务，则清除 Event 并起一个延迟刷新任务
                elif self._flush_event.is_set():
                    self._flush_event.clear()
                    self._flush_task = asyncio.create_task(self._schedule_flush())
                    self._flush_task.add_done_callback(self._on_flush_task_done)

            # 批量已满立刻刷新；或遇到终态事件（complete/error/done）也立即落库，确保最终状态不滞留
            if should_flush_now:
                await self.flush_mongo_buffer()
            elif event_type in ("complete", "error", "done"):
                await self.flush_mongo_buffer()

        return redis_success

    def get_diagnostics(self) -> dict[str, Any]:
        """Return lightweight writer diagnostics for health checks and tests."""
        # 暴露缓冲区/丢弃计数/TTL 跟踪数等轻量指标，供健康检查与测试使用
        return {
            "mongo_buffer_size": len(self._mongo_buffer),
            "mongo_buffer_max": _get_mongo_buffer_max(),
            "mongo_buffer_dropped_total": self._mongo_buffer_dropped_total,
            "mongo_buffer_last_drop": self._mongo_buffer_last_drop,
            "ttl_tracked_streams": len(self._ttl_set_keys),
        }

    def _on_flush_task_done(self, task: asyncio.Task[None]) -> None:
        # 延迟刷新任务完成回调：清理句柄并记录异常（取消不算错误）
        if self._flush_task is task:
            self._flush_task = None
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.warning("Scheduled MongoDB event flush failed: %s", exc)

    async def _schedule_flush(self) -> None:
        """调度延迟刷新"""
        # 先睡一个刷新间隔（此阶段可被抢占取消），到点后执行实际刷新
        try:
            self._flush_task_waiting = True
            await asyncio.sleep(_MONGO_FLUSH_INTERVAL)
        finally:
            self._flush_task_waiting = False
        await self._do_flush()

    async def _drain_scheduled_flush_task(self) -> bool:
        # 抢占/排空当前的延迟刷新任务，避免与外部强制 flush 重复执行
        task = self._flush_task
        if task is None:
            return False
        # 不能等待自身，防止死锁
        if task is asyncio.current_task():
            return False
        if task.done():
            if self._flush_task is task:
                self._flush_task = None
            return False

        # 任务仍在"等待间隔"阶段：直接取消，改由调用方立即刷新
        if self._flush_task_waiting:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if self._flush_task is task:
                self._flush_task = None
            return False

        # 任务已进入刷新执行阶段：等它跑完，视为本次刷新已完成
        try:
            await task
        except asyncio.CancelledError:
            return False
        except Exception as e:
            logger.warning("Scheduled MongoDB event flush failed while draining: %s", e)
            return False
        finally:
            if self._flush_task is task:
                self._flush_task = None
        return True

    async def _do_flush(self) -> None:
        """实际执行批量写入，使用 bulk_write 优化"""
        # 取出当前缓冲的整批并清空缓冲（锁内快速切换，尽量缩短持锁时间）
        async with self._mongo_lock:
            if not self._mongo_buffer:
                self._flush_event.set()
                return

            batch = self._mongo_buffer
            self._mongo_buffer = []

        now = utc_now()
        max_events = _get_max_events_per_trace()
        # 是否启用分块存储：大 trace 事件拆到独立分块集合，绕过 Mongo 单文档 16MB 限制
        chunk_storage_enabled = bool(
            getattr(settings, "SESSION_EVENT_CHUNK_STORAGE_ENABLED", False)
        )
        # 是否同时双写 legacy events 数组（迁移期兼容，便于回退）
        dual_write_legacy = bool(getattr(settings, "SESSION_EVENT_CHUNK_DUAL_WRITE_LEGACY", False))

        if chunk_storage_enabled:
            # 分块写入：按组预留序号并追加到分块集合；失败项收集起来稍后重试
            failed_chunk_items: list[MongoBufferItem] = []
            for trace_id, items, events, reserved_start_seq in _iter_chunk_write_groups(batch):
                trace_doc: dict[str, Any] | None = None
                start_seq = reserved_start_seq
                try:
                    if start_seq is None:
                        # 首次写入：向 trace 原子预留一段连续序号，保证事件全局有序
                        trace_doc = await self.trace.reserve_event_sequence_range(
                            trace_id,
                            len(events),
                        )
                        if not trace_doc:
                            logger.warning(
                                "Chunk write skipped because trace %s was not found", trace_id
                            )
                            failed_chunk_items.extend(items)
                            continue
                        # 由预留后的 event_count 反推本组起始序号
                        start_seq = int(trace_doc.get("event_count", 0)) - len(events) + 1
                    else:
                        # 重试路径：序号已在上次预留，直接复用，避免重复占号
                        trace_doc = {
                            "trace_id": trace_id,
                            "session_id": items[0][3],
                            "run_id": items[0][4],
                        }
                    await self.trace.append_events_to_chunks(trace_doc, events, start_seq)
                except Exception as e:
                    # 失败项重新入队重试；已知起始序号时逐条带上预留序号，保证顺序不乱
                    if start_seq is not None:
                        failed_chunk_items.extend(
                            _with_chunk_retry_metadata(
                                item,
                                reserved_start_seq=start_seq + offset,
                                skip_legacy=dual_write_legacy or _buffer_item_skip_legacy(item),
                            )
                            for offset, item in enumerate(items)
                        )
                    else:
                        failed_chunk_items.extend(items)
                    logger.warning(
                        "Chunk write failed for trace %s with %s events: %s",
                        trace_id,
                        len(events),
                        e,
                    )

            # 未开启 legacy 双写：分块即唯一存储，失败项放回缓冲后直接返回
            if not dual_write_legacy:
                if failed_chunk_items:
                    async with self._mongo_lock:
                        self._mongo_buffer = failed_chunk_items + self._mongo_buffer
                self._flush_event.set()
                return
            # 开启 legacy 双写：失败项放回缓冲，但仍继续往下写 legacy events 数组
            if failed_chunk_items:
                async with self._mongo_lock:
                    self._mongo_buffer = failed_chunk_items + self._mongo_buffer

        # 构建 bulk 操作是 CPU 计算，放线程池执行
        operations = await run_blocking_io(
            _build_mongo_bulk_operations,
            batch,
            now=now,
            max_events=max_events,
        )

        # 批量执行
        if operations:
            try:
                # ordered=False：各操作相互独立、可并行，单条失败不阻断其余
                result = await self.trace.collection.bulk_write(operations, ordered=False)
                logger.debug(
                    f"Bulk write: {result.modified_count} modified, {result.upserted_count} upserted"
                )
            except BulkWriteError as e:
                # 部分失败：尽量只重试失败 trace 对应的项，无法定位则整批重试
                logger.warning(f"Bulk write failed: {e}")
                failed_trace_ids = _failed_bulk_write_trace_ids(e, operations)
                if failed_trace_ids is None:
                    retry_source_items = batch
                else:
                    retry_source_items = [
                        item for item in batch if _buffer_item_base(item)[0] in failed_trace_ids
                    ]
                # 分块+双写模式下，重试项标记 skip_chunk 只补写 legacy，避免重复写分块
                if chunk_storage_enabled and dual_write_legacy:
                    retry_items = [
                        _with_chunk_retry_metadata(
                            item,
                            reserved_start_seq=_buffer_item_reserved_start_seq(item) or 0,
                            skip_legacy=False,
                            skip_chunk=True,
                        )
                        for item in retry_source_items
                        if not _buffer_item_skip_legacy(item)
                    ]
                else:
                    retry_items = retry_source_items
                async with self._mongo_lock:
                    self._mongo_buffer = retry_items + self._mongo_buffer
            except Exception as e:
                # 整体失败：整批放回缓冲重试（同样处理分块+双写的 skip_chunk 标记）
                logger.warning(f"Bulk write failed: {e}")
                if chunk_storage_enabled and dual_write_legacy:
                    retry_items = [
                        _with_chunk_retry_metadata(
                            item,
                            reserved_start_seq=_buffer_item_reserved_start_seq(item) or 0,
                            skip_legacy=False,
                            skip_chunk=True,
                        )
                        for item in batch
                        if not _buffer_item_skip_legacy(item)
                    ]
                else:
                    retry_items = batch
                async with self._mongo_lock:
                    self._mongo_buffer = retry_items + self._mongo_buffer

        # 标记完成，允许下次刷新
        self._flush_event.set()

    async def flush_mongo_buffer(self, *, require_empty: bool = False) -> None:
        """强制刷新缓冲（外部调用）"""
        # 先接管进行中的延迟刷新任务，避免重复刷；若没有则自己刷一次
        flushed_by_scheduled_task = await self._drain_scheduled_flush_task()
        if not flushed_by_scheduled_task:
            await self._do_flush()
        # require_empty：要求刷完后缓冲必须为空，否则报错（用于确保数据全部落库）
        if require_empty:
            async with self._mongo_lock:
                remaining = len(self._mongo_buffer)
            if remaining:
                raise RuntimeError(f"MongoDB event buffer still has {remaining} pending events")

    async def _flush_redis_buffer(self) -> None:
        """保留兼容性"""
        # Redis 为即时写入、无缓冲，此方法仅为兼容旧调用而保留空实现
        pass

    async def complete_trace(
        self,
        trace_id: str,
        status: str = "completed",
        metadata: Optional[Dict[str, Any]] = None,
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
        # 委托 trace 存储把 trace 置为终态（completed/error）
        return await self.trace.complete_trace(trace_id, status, metadata)

    async def _write_to_redis_direct(
        self,
        stream_key: str,
        fields: Dict[str, str],
    ) -> bool:
        """
        单条立即写入 Redis Stream（用于流式事件，保证实时性）

        Args:
            stream_key: Redis Stream key
            fields: 已序列化的字段 dict

        Returns:
            是否写入成功
        """
        try:
            # XADD 追加一条事件到 Stream
            await self.redis.xadd(
                stream_key,
                fields,
            )

            # 维护 stream 的过期时间：用单调时钟 + 缓存节流，避免每次写入都调用 EXPIRE
            now = time.monotonic()
            next_ttl_refresh_at = self._ttl_set_keys.get(stream_key)
            if next_ttl_refresh_at is None:
                # 首次见到该 key：若尚未设置过期（ttl==-1）则补设 TTL
                ttl = await self.redis.ttl(stream_key)
                if ttl == -1:
                    await self.redis.expire(stream_key, settings.SSE_CACHE_TTL)
                self._ttl_set_keys[stream_key] = now + _get_ttl_refresh_interval()
            elif now >= next_ttl_refresh_at:
                # 到达刷新时点：续设 TTL 并推后下次刷新时间
                await self.redis.expire(stream_key, settings.SSE_CACHE_TTL)
                self._ttl_set_keys[stream_key] = now + _get_ttl_refresh_interval()
            else:
                # 未到刷新时点：仅把 key 挪到 LRU 末尾表示最近使用
                self._ttl_set_keys.move_to_end(stream_key)

            # 仅在真正刷新过的分支做 LRU 淘汰，控制缓存字典规模
            if next_ttl_refresh_at is None or now >= next_ttl_refresh_at:
                self._ttl_set_keys.move_to_end(stream_key)
                # LRU eviction
                while len(self._ttl_set_keys) > _get_ttl_set_keys_max():
                    self._ttl_set_keys.popitem(last=False)
            return True
        except Exception as e:
            # Redis 写失败不抛出，仅告警并返回 False（Mongo 仍会持久化，不丢数据）
            logger.warning(f"Redis xadd failed (streaming event): {e}")
            return False

    async def read_from_redis(
        self,
        session_id: str,
        run_id: Optional[str] = None,
        overall_timeout: float = _LIVE_STREAM_READ_TIMEOUT_SECONDS,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        从 Redis Stream 读取事件（阻塞读取，直到流结束）

        通过定期发送 SSE 心跳注释检测客户端断开，避免僵尸连接占用资源。
        SSE 注释（以 : 开头的行）会被 EventSource 客户端自动忽略。

        Args:
            session_id: 会话 ID
            run_id: 运行 ID（用于隔离多轮对话）
            overall_timeout: 整体超时（秒），默认 24 小时，防止无限等待

        Yields:
            事件字典，包含 id, event_type, data
            心跳事件: event_type="heartbeat"（用于检测死连接）
        """
        stream_key = self._stream_key(session_id, run_id)
        last_id = "0"
        block = _REDIS_XREAD_BLOCK_MS
        heartbeat_interval = _SSE_HEARTBEAT_INTERVAL_SECONDS
        start_time = asyncio.get_event_loop().time()
        last_heartbeat = start_time
        logger.info(f"[Redis] Reading from stream: {stream_key}")

        try:
            # 第一阶段：用 XRANGE 分批回放已存在的历史事件（追赶断点/迟到订阅）
            replay_min = "-"
            replay_batch_size = _get_redis_replay_batch_size()
            replayed_count = 0
            while True:
                entries = await self.redis.xrange(
                    stream_key,
                    min=replay_min,
                    max="+",
                    count=replay_batch_size,
                )
                if not entries:
                    break
                replayed_count += len(entries)
                logger.debug(
                    "[Redis] Initial xrange replayed %d entries from %s",
                    len(entries),
                    stream_key,
                )
                for entry_id, fields in entries:
                    event = {
                        "id": entry_id,
                        "event_type": fields.get("event_type"),
                        "data": await _parse_event_data_from_redis(fields.get("data", "{}")),
                        "timestamp": fields.get("timestamp"),
                    }
                    yield event
                    last_id = entry_id
                    # 回放中若遇终态事件即结束，无需再进入阻塞监听
                    if _should_stop_stream_on_event(event):
                        return
                # 用排他区间 (last_id 继续下一批，直到取尽
                replay_min = f"({last_id}"
                if len(entries) < replay_batch_size:
                    break
            logger.info(
                f"[Redis] Initial xrange replayed {replayed_count} entries from {stream_key}"
            )

            # 第二阶段：进入阻塞式 XREAD 循环，实时消费后续新增事件
            logger.info(f"[Redis] Entering blocking xread loop for {stream_key}")
            while True:
                now = asyncio.get_event_loop().time()

                # 整体超时检查，防止 producer 崩溃导致无限等待
                elapsed = now - start_time
                if elapsed >= overall_timeout:
                    logger.warning(
                        f"[Redis] SSE read timed out after {overall_timeout}s for {stream_key}"
                    )
                    yield {
                        "id": "timeout",
                        "event_type": "error",
                        "data": {"error": "Stream read timed out"},
                        "timestamp": utc_now().isoformat(),
                    }
                    return

                # 心跳检测：定期 yield，如果客户端已断开，FastAPI 会在写入时
                # 抛出 CancelledError，从而提前释放资源
                if now - last_heartbeat >= heartbeat_interval:
                    last_heartbeat = now
                    yield {
                        "id": "heartbeat",
                        "event_type": "heartbeat",
                        "data": {},
                        "timestamp": utc_now().isoformat(),
                    }

                try:
                    # 阻塞读取新事件（最多阻塞 block 毫秒），从 last_id 之后开始
                    results = await self.redis.xread(
                        {stream_key: last_id},
                        count=replay_batch_size,
                        block=block,
                    )
                    if results:
                        logger.debug(
                            f"[Redis] xread returned {len(results)} results from {stream_key}"
                        )
                        for _, entries in results:
                            for entry_id, fields in entries:
                                event = {
                                    "id": entry_id,
                                    "event_type": fields.get("event_type"),
                                    "data": await _parse_event_data_from_redis(
                                        fields.get("data", "{}")
                                    ),
                                    "timestamp": fields.get("timestamp"),
                                }
                                yield event
                                last_id = entry_id
                                # 遇终态事件结束流
                                if _should_stop_stream_on_event(event):
                                    return
                except Exception as xread_error:
                    # 单次读取失败不致命：短暂退避后重试
                    logger.warning(f"xread failed (non-fatal): {xread_error}")
                    await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Redis read failed: {e}")
            return

    async def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """获取完整的 trace"""
        # 委托 trace 存储读取完整 trace 文档
        return await self.trace.get_trace(trace_id)

    async def get_trace_events(
        self,
        trace_id: str,
        event_types: Optional[List[str]] = None,
        max_events: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 trace 的事件列表"""
        # 委托 trace 存储按类型/上限读取某个 trace 的事件
        return await self.trace.get_trace_events(trace_id, event_types, max_events=max_events)

    async def list_traces(
        self,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        skip: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出 traces"""
        # 委托 trace 存储按会话/用户/agent/状态等条件分页列出 traces
        return await self.trace.list_traces(
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            status=status,
            limit=limit,
            skip=skip,
        )

    async def read_session_events(
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
        从 MongoDB 读取会话的所有事件（跨 traces 聚合）

        Args:
            session_id: 会话 ID
            event_types: 可选的事件类型过滤
            run_id: 可选的运行 ID 过滤（用于隔离多轮对话）
            exclude_run_id: 可选的运行 ID 排除（用于排除正在运行的 run）
            completed_only: 是否只返回完成的 trace 中的事件（默认 True）
            run_ids: 可选的运行 ID 列表过滤
            max_events: 可选的最大返回事件数

        Returns:
            事件列表
        """
        # 跨 trace 聚合读取整个会话的事件（委托 trace 存储的 get_session_events）
        return await self.trace.get_session_events(
            session_id,
            event_types,
            run_id=run_id,
            exclude_run_id=exclude_run_id,
            completed_only=completed_only,
            run_ids=run_ids,
            max_events=max_events,
        )

    async def get_stream_length(self, session_id: str, run_id: Optional[str] = None) -> int:
        """
        获取 Redis Stream 长度

        Args:
            session_id: 会话 ID
            run_id: 运行 ID（可选）
        """
        stream_key = self._stream_key(session_id, run_id)
        try:
            # XLEN 返回流中事件条数；出错则返回 0
            return await self.redis.xlen(stream_key)
        except Exception:
            return 0

    async def clear_stream(self, session_id: str, run_id: Optional[str] = None) -> None:
        """
        清除 Redis Stream

        Args:
            session_id: 会话 ID
            run_id: 运行 ID（可选）
        """
        stream_key = self._stream_key(session_id, run_id)
        try:
            # 直接删除整个 stream key
            await self.redis.delete(stream_key)
        except Exception as e:
            logger.warning(f"Failed to clear stream: {e}")

    async def expire_stream(
        self,
        session_id: str,
        run_id: Optional[str] = None,
        ttl_seconds: int = 60,
    ) -> bool:
        """
        Shorten Redis Stream TTL after a run reaches a terminal state.

        Keeping a short grace period avoids racing active SSE readers that still
        need the terminal event, while preventing completed runs from occupying
        Redis for the full live-stream TTL.
        """
        # run 结束后缩短流 TTL：留一小段宽限期给仍在读的 SSE，同时尽快回收 Redis 空间
        stream_key = self._stream_key(session_id, run_id)
        try:
            ttl = max(int(ttl_seconds), 1)
            success = await self.redis.expire(stream_key, ttl)
            # 从 TTL 刷新缓存移除该 key，避免后续写入又把 TTL 拉长
            self._ttl_set_keys.pop(stream_key, None)
            return bool(success)
        except Exception as e:
            logger.warning(f"Failed to expire stream: {e}")
            return False


# Singleton instance
# 进程级单例，保证全局共享同一份 Mongo 写缓冲与后台刷新任务
_dual_writer: Optional[DualEventWriter] = None


def get_dual_writer() -> DualEventWriter:
    """获取 DualEventWriter 单例"""
    global _dual_writer
    if _dual_writer is None:
        _dual_writer = DualEventWriter()
    return _dual_writer


async def close_dual_writer() -> None:
    """Flush and release the DualEventWriter singleton without creating it."""
    # 优雅关闭：先摘除全局引用，再把缓冲中剩余事件刷入 Mongo，确保不丢数据
    global _dual_writer
    writer = _dual_writer
    _dual_writer = None
    if writer is not None:
        await writer.flush_mongo_buffer()

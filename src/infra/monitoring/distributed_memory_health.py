"""Distributed memory health snapshot helpers."""

from __future__ import annotations

import json
import os
import socket
import time
from collections import deque
from datetime import date, datetime, timezone
from hashlib import sha1
from typing import Any, Mapping

from src.infra.async_utils.blocking import run_blocking_io
from src.infra.logging import get_logger
from src.infra.storage.redis import get_redis_client
from src.infra.utils.datetime import utc_now

logger = get_logger(__name__)
# 本模块把单个进程（实例）的内存监控快照发布到 Redis，并汇总整个集群（多实例部署）内
# 所有实例的快照，从而在没有集中式 APM 的情况下也能定位"哪个实例内存异常"。
# 每个进程用基于 hostname+pid+启动时间 生成的短哈希作为稳定的 instance_id，
# 快照写入 Redis 时带较短的 TTL（约为采集间隔的 2 倍），实例下线/僵死后快照会自动过期消失。
_INSTANCE_KEY_PREFIX = "health:memory:instance:"
# 进程级唯一种子：hostname + pid + 纳秒级时间戳，三者组合极大概率保证跨进程不重复
_PROCESS_SEED = f"{socket.gethostname()}:{os.getpid()}:{time.time_ns()}"
# 对种子做哈希并截断为 12 位十六进制，作为对外展示的稳定实例 ID（进程存活期间保持不变）
_INSTANCE_ID = sha1(_PROCESS_SEED.encode("utf-8")).hexdigest()[:12]
# 汇总集群快照时用 SCAN 扫描 key 的数量上限，防止实例数异常暴涨拖慢健康检查接口
CLUSTER_SNAPSHOT_SCAN_LIMIT = 100


# 递归地把任意 Python 值转换为可安全 JSON 序列化 / 写入 Redis 的基础类型：
# 日期时间转 ISO 字符串，bytes 转字符串，dict/list 递归处理，其余未知类型兜底转成 str()，
# 确保不会因为快照里混入不可序列化的对象（比如自定义类实例）而导致写入失败。
def _to_redis_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(key): _to_redis_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, deque)):
        return [_to_redis_safe(item) for item in value]
    return str(value)


# 按 instance_id 字符串排序快照列表，保证多次调用返回顺序稳定（便于前端展示/测试断言）
def _sort_snapshots(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(snapshots, key=lambda item: str(item.get("instance_id") or ""))


# 校验并清洗 instance_id：必须是去除首尾空白后非空的字符串，否则视为无效返回 None
def _normalize_instance_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


# 校验一份快照 payload 是否结构合法：必须是 dict 且带有合法的 instance_id；
# summary 字段可以缺失，但如果存在则必须是 Mapping 类型（否则说明数据被破坏或版本不兼容）
def _is_valid_snapshot_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if _normalize_instance_id(payload.get("instance_id")) is None:
        return False
    summary = payload.get("summary")
    return summary is None or isinstance(summary, Mapping)


# 以下两个函数统一把可能是 None / 非法类型的行数据、summary 数据兜底为普通 dict，
# 避免下游代码到处写 isinstance 判断
def _normalize_row(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {}


def _normalize_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


# 规整一份快照：确保 instance_id 和 summary 字段都是标准化后的值，
# 其余字段通过 **snapshot 原样保留
def _normalize_snapshot_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **snapshot,
        "instance_id": _normalize_instance_id(snapshot.get("instance_id")),
        "summary": _normalize_summary(snapshot.get("summary")),
    }


# JSON 序列化/反序列化可能涉及较大的快照数据，丢到线程池执行，避免阻塞事件循环
async def _json_dumps_snapshot(snapshot: Mapping[str, Any]) -> str:
    return await run_blocking_io(json.dumps, snapshot)


async def _json_loads_snapshot(raw_value: str) -> Any:
    return await run_blocking_io(json.loads, raw_value)


# 把快照的 captured_at 字段转换为可比较的排序 key，用于在同一 instance_id 出现多份快照时
# 挑出"最新"的一份：
#   - 能成功解析为带时区的时间 -> (2, 时间戳, 原始字符串)，优先级最高
#   - 解析失败但存在字符串   -> (1, 0.0, 原始字符串)，次之
#   - 完全没有 captured_at   -> (0, 0.0, "")，最低优先级
# 三元组按元组比较规则排序，数字越大代表越"新"。
def _captured_at_order_key(snapshot: Mapping[str, Any]) -> tuple[int, float, str]:
    captured_at = snapshot.get("captured_at")
    if isinstance(captured_at, str):
        try:
            parsed = datetime.fromisoformat(captured_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            return (2, parsed.timestamp(), captured_at)
        except ValueError:
            return (1, 0.0, captured_at)
    return (0, 0.0, "")


# 把可能是 list/tuple/set/deque 的明细行统一转换为 list[dict]，非法类型兜底为空列表
def _normalize_detail_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple, set, deque)):
        return []
    return [_normalize_row(row) for row in value]


# 规整内存诊断明细（tracemalloc/gc 等采样结果的四类明细表）；
# 非 Mapping 类型直接返回空字典，避免下游按 key 取值时抛异常
def _normalize_details(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        "top_growth": _normalize_detail_rows(value.get("top_growth")),
        "top_allocations": _normalize_detail_rows(value.get("top_allocations")),
        "top_object_types": _normalize_detail_rows(value.get("top_object_types")),
        "top_objects": _normalize_detail_rows(value.get("top_objects")),
    }


# 把字节数格式化为带 MB 单位、保留两位小数的展示字符串；None 原样返回 None（表示无数据）
def _format_mb(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{round(value / 1024 / 1024, 2)}MB"


# 根据内存监控 summary 构建给前端展示用的精简概览：
#   - available=False           -> status=unavailable（比如 psutil 不可用等环境限制）
#   - suspected_leak=True       -> status=suspected_leak（怀疑内存泄漏，需要关注）
#   - 其余                       -> status=stable
# 同时把原始字节数字段格式化为易读的 MB 字符串
def _build_memory_overview(summary: Mapping[str, Any]) -> dict[str, Any]:
    status = "unavailable"
    if summary.get("available"):
        status = "suspected_leak" if summary.get("suspected_leak") else "stable"

    return {
        "status": status,
        "rss": _format_mb(summary.get("rss_bytes")),
        "vms": _format_mb(summary.get("vms_bytes")),
        "growth": _format_mb(summary.get("growth_bytes")),
        "threads": summary.get("thread_count"),
        "open_files": summary.get("open_file_count"),
        "history_size": summary.get("history_size"),
        "last_sample_at": summary.get("last_sample_at"),
    }


# 从 summary + details 中提炼出若干条"高亮"要点，用于在健康检查面板顶部快速展示异常信号：
#   1) 总是先给出整体状态（stable/suspected_leak，或直接 unavailable 并附带原因）
#   2) 若存在 tracemalloc 内存增长采样（top_growth），取增长最多的一条
#   3) 若存在内存分配采样（top_allocations），取占用最大的一条
#   4) 若存在对象计数采样（top_object_types 或 top_objects），取数量最多的一类
# 每一类信息只有在关键字段都存在时才会被加入，避免展示不完整/无意义的数据。
def _build_highlight_items(
    summary: Mapping[str, Any],
    details: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not summary.get("available"):
        return [{"kind": "unavailable", "reason": summary.get("reason", "unknown")}]

    highlights: list[dict[str, Any]] = [
        {
            "kind": "status",
            "status": "suspected_leak" if summary.get("suspected_leak") else "stable",
            "severity": "warning" if summary.get("suspected_leak") else "info",
        }
    ]

    # 取 tracemalloc 内存增长采样中的第一条（约定调用方已按增长量排序）
    growth_rows = (details or {}).get("top_growth") or []
    if growth_rows:
        top = _normalize_row(growth_rows[0])
        if top.get("location") is not None and top.get("size_diff_bytes") is not None:
            highlights.append(
                {
                    "kind": "top_growth",
                    "location": top.get("location"),
                    "size_diff": _format_mb(top.get("size_diff_bytes")) or "N/A",
                }
            )

    # 取内存分配采样中的第一条（约定调用方已按占用量排序）
    allocation_rows = (details or {}).get("top_allocations") or []
    if allocation_rows:
        top = _normalize_row(allocation_rows[0])
        if top.get("location") is not None and top.get("size_bytes") is not None:
            highlights.append(
                {
                    "kind": "top_allocation",
                    "location": top.get("location"),
                    "size": _format_mb(top.get("size_bytes")) or "N/A",
                }
            )

    # top_object_types / top_objects 是新旧字段名的兼容处理，任一存在即可
    object_rows = (
        (details or {}).get("top_object_types") or (details or {}).get("top_objects") or []
    )
    if object_rows:
        top = _normalize_row(object_rows[0])
        if top.get("type") is not None and top.get("count") is not None:
            highlights.append(
                {
                    "kind": "top_object_type",
                    "type": top.get("type"),
                    "count": top.get("count"),
                }
            )

    return highlights


# 以下三个函数把明细行中的原始字节数/计数字段格式化为便于展示的字符串，
# 供前端渲染增长/分配/对象计数三类明细表格
def _format_growth_rows(rows: Any) -> list[dict[str, Any]]:
    return [
        {
            **row_data,
            "size_diff": _format_mb(row_data.get("size_diff_bytes")),
        }
        for row_data in (_normalize_row(row) for row in (rows or []))
    ]


def _format_allocation_rows(rows: Any) -> list[dict[str, Any]]:
    return [
        {
            **row_data,
            "size": _format_mb(row_data.get("size_bytes")),
        }
        for row_data in (_normalize_row(row) for row in (rows or []))
    ]


# 拼出形如 "dict=1234" 的展示标签；关键字段缺失时 label 为 None，前端据此判断是否展示该行
def _format_object_rows(rows: Any) -> list[dict[str, Any]]:
    return [
        {
            **row_data,
            "label": (
                f"{row_data.get('type')}={row_data.get('count')}"
                if row_data.get("type") is not None and row_data.get("count") is not None
                else None
            ),
        }
        for row_data in (_normalize_row(row) for row in (rows or []))
    ]


# 供其他模块（如定时上报任务、诊断接口）引用本进程的稳定实例 ID
def get_instance_id() -> str:
    """Return a stable instance identifier for the current process."""
    return _INSTANCE_ID


def build_instance_key(instance_id: str) -> str:
    """Build the Redis key for one instance memory snapshot."""
    # 同一 instance_id 对应固定的 Redis key，写入时覆盖式 SET；
    # 读取集群视图时按前缀 SCAN 批量枚举所有实例的 key
    return f"{_INSTANCE_KEY_PREFIX}{instance_id}"


def calculate_snapshot_ttl(interval_seconds: float) -> int:
    """Derive a Redis TTL from the monitor interval."""
    # TTL 取采集间隔的 2 倍，且不低于 120 秒兜底：
    # 既保证两次采集之间快照不会提前过期，又能在实例下线/僵死后合理时间内自动从集群视图中消失
    return max(int(interval_seconds * 2), 120)


def build_instance_snapshot(
    *,
    instance_id: str | None = None,
    captured_at: Any | None = None,
    summary: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Redis-safe snapshot for one process instance."""
    # 组装一份完整的单实例快照：保留原始 summary/details，并派生出 overview（精简概览）、
    # highlights（高亮要点）、以及格式化后的三类明细表；最终整体过一遍 _to_redis_safe
    # 确保可以安全序列化写入 Redis
    normalized_summary = _normalize_summary(summary)
    normalized_details = _normalize_details(details)
    snapshot = {
        "instance_id": _normalize_instance_id(instance_id) or get_instance_id(),
        "captured_at": captured_at if captured_at is not None else utc_now(),
        "summary": normalized_summary,
        "overview": _build_memory_overview(normalized_summary),
        "highlights": _build_highlight_items(normalized_summary, normalized_details),
        "top_growth": _format_growth_rows(normalized_details.get("top_growth")),
        "top_allocations": _format_allocation_rows(normalized_details.get("top_allocations")),
        "top_objects": _format_object_rows(
            normalized_details.get("top_object_types") or normalized_details.get("top_objects")
        ),
    }
    return _to_redis_safe(snapshot)


async def publish_instance_snapshot(
    snapshot: Mapping[str, Any],
    *,
    interval_seconds: float,
    redis_client: Any | None = None,
) -> dict[str, Any]:
    """Publish one instance snapshot into Redis with a short-lived TTL."""
    # 允许调用方注入 redis_client（便于测试），否则使用全局共享客户端
    client = redis_client or get_redis_client()
    serialized_snapshot = _to_redis_safe(dict(snapshot))
    instance_id = (
        _normalize_instance_id(serialized_snapshot.get("instance_id")) or get_instance_id()
    )
    serialized_snapshot["instance_id"] = instance_id
    payload = await _json_dumps_snapshot(serialized_snapshot)
    # ex 参数直接使用 Redis 原生的秒级过期机制，无需额外维护后台清理任务
    await client.set(
        build_instance_key(instance_id),
        payload,
        ex=calculate_snapshot_ttl(interval_seconds),
    )
    return serialized_snapshot


async def _scan_keys(
    client: Any,
    pattern: str,
    *,
    count: int = 100,
    limit: int = CLUSTER_SNAPSHOT_SCAN_LIMIT,
) -> list[str]:
    """Collect matching Redis keys with SCAN to avoid blocking Redis."""
    # 用 SCAN 而非 KEYS 遍历匹配的 key，避免在 key 数量较大时长时间阻塞 Redis 的单线程事件循环；
    # cursor 归 0 表示一轮扫描结束（Redis SCAN 协议的约定）
    cursor: int | str = 0
    keys: list[str] = []
    while True:
        cursor, batch = await client.scan(cursor=cursor, match=pattern, count=count)
        for key in batch:
            keys.append(str(key))
            if len(keys) >= limit:
                # 达到扫描上限时提前退出并告警，防止实例数异常暴涨时把健康检查接口拖慢
                logger.warning(
                    "[DistributedMemoryHealth] reached cluster snapshot scan limit: %d",
                    limit,
                )
                return keys
        if int(cursor) == 0:
            return keys


async def load_cluster_snapshots(*, redis_client: Any | None = None) -> list[dict[str, Any]]:
    """Load all known instance snapshots from Redis."""
    client = redis_client or get_redis_client()
    try:
        # 枚举出集群内所有实例的快照 key
        keys = await _scan_keys(client, f"{_INSTANCE_KEY_PREFIX}*")
        snapshots_by_instance_id: dict[str, dict[str, Any]] = {}
        # 排序只是为了让日志/异常出现顺序稳定，不影响最终聚合结果
        for key in sorted(keys):
            try:
                raw_value = await client.get(key)
            except Exception as exc:
                # 单个 key 读取失败不应影响整体加载，捕获后跳过并记录告警
                logger.warning(
                    "[DistributedMemoryHealth] skipping unreadable cluster snapshot key=%s: %s",
                    key,
                    exc,
                )
                continue
            if not raw_value:
                # key 存在但值为空（例如恰好在 TTL 边界被清空）时跳过
                continue
            try:
                if isinstance(raw_value, bytes):
                    raw_value = raw_value.decode("utf-8", errors="replace")
                payload = await _json_loads_snapshot(raw_value)
            except (TypeError, json.JSONDecodeError) as exc:
                # JSON 解析失败（数据损坏/版本不兼容）同样跳过而不是抛出，
                # 避免一条脏数据拖垮整个集群视图的加载
                logger.warning(
                    "[DistributedMemoryHealth] skipping malformed cluster snapshot key=%s: %s",
                    key,
                    exc,
                )
                continue
            if _is_valid_snapshot_payload(payload):
                normalized_payload = _normalize_snapshot_payload(payload)
                instance_id = str(normalized_payload["instance_id"])
                existing_payload = snapshots_by_instance_id.get(instance_id)
                # 同一 instance_id 出现多条时，保留 captured_at 更新的一条
                # （正常情况下同一实例只应有一个 key，出现多条通常是历史遗留数据）
                if existing_payload is None or _captured_at_order_key(
                    normalized_payload
                ) >= _captured_at_order_key(existing_payload):
                    snapshots_by_instance_id[instance_id] = normalized_payload
            else:
                logger.warning(
                    "[DistributedMemoryHealth] skipping invalid cluster snapshot key=%s",
                    key,
                )
        return _sort_snapshots(list(snapshots_by_instance_id.values()))
    except Exception as exc:
        logger.warning("[DistributedMemoryHealth] failed to load cluster snapshots: %s", exc)
        return []


def select_instance_snapshot(
    snapshots: list[dict[str, Any]],
    *,
    requested_instance_id: str | None = None,
    local_instance_id: str | None = None,
) -> dict[str, Any] | None:
    """Select a specific, local, or deterministic fallback instance snapshot."""
    ordered_snapshots = _sort_snapshots(
        [
            _normalize_snapshot_payload(snapshot)
            for snapshot in snapshots
            if _is_valid_snapshot_payload(snapshot)
        ]
    )
    normalized_requested_id = _normalize_instance_id(requested_instance_id)
    normalized_local_id = _normalize_instance_id(local_instance_id)

    # 优先返回调用方明确指定的实例（例如前端下拉框选择了某个具体实例）
    if normalized_requested_id:
        for snapshot in ordered_snapshots:
            if snapshot.get("instance_id") == normalized_requested_id:
                return snapshot

    # 其次返回当前处理请求的本机实例，让默认视图优先展示"离自己最近"的数据
    if normalized_local_id:
        for snapshot in ordered_snapshots:
            if snapshot.get("instance_id") == normalized_local_id:
                return snapshot

    # 都没有指定/命中时，兜底返回排序后的第一个实例，保证接口始终有确定性的返回值
    if ordered_snapshots:
        return ordered_snapshots[0]
    return None


def build_cluster_overview(
    snapshots: list[dict[str, Any]],
    *,
    requested_instance_id: str | None = None,
    local_instance_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate instance snapshots into a small cluster overview."""
    normalized_snapshots = [
        _normalize_snapshot_payload(snapshot)
        for snapshot in snapshots
        if _is_valid_snapshot_payload(snapshot)
    ]
    ordered_snapshots = _sort_snapshots(normalized_snapshots)
    selected_snapshot = select_instance_snapshot(
        ordered_snapshots,
        requested_instance_id=requested_instance_id,
        local_instance_id=local_instance_id,
    )

    return {
        "instance_count": len(ordered_snapshots),
        # 统计有多少实例的内存监控本身是可用的（summary.available 为真）
        "available_instance_count": sum(
            bool((snapshot.get("summary") or {}).get("available")) for snapshot in ordered_snapshots
        ),
        # 统计有多少实例被标记为疑似内存泄漏，用于集群级别的整体告警判断
        "suspected_leak_count": sum(
            bool((snapshot.get("summary") or {}).get("suspected_leak"))
            for snapshot in ordered_snapshots
        ),
        "local_instance_id": local_instance_id,
        "selected_instance_id": (
            str(selected_snapshot.get("instance_id")) if selected_snapshot else None
        ),
        "selected_instance": selected_snapshot,
        "instances": ordered_snapshots,
    }

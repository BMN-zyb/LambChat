"""校验脚本：核对某个 trace 在事件分块迁移前后的数据是否一致。

配合 migrate_trace_events_to_chunks.py 使用：迁移脚本默认不会删除旧的
events 字段（需显式传 --remove-legacy-events 才会删除），本脚本正是利用
这一点，同时读取旧版内嵌事件（traces 集合文档里的 events 字段）和新版
分块事件（trace_event_chunks 集合按 chunk_index 顺序拼接还原），比较两边
的事件总数、首个事件、最后一个事件是否一致，从而确认迁移没有丢事件/错序。

用法:
    python scripts/verify_trace_event_chunks.py --trace-id <ID> [--event-types a,b,c]

说明:
    - 只读取数据，不修改任何文档，可安全地反复执行。
    - --event-types 可选，传入后只比较指定类型的事件（逗号分隔）；不传则比较全部事件。
    - 进程退出码：0 表示一致（打印 status: ok），1 表示发现不一致（打印 mismatches），
      便于在运维脚本/CI 中据此判断迁移是否成功。
    - 若目标 trace 已经用 --remove-legacy-events 清理过旧字段，legacy 侧会读到
      空事件列表，比较必然报 mismatch，因此本脚本应在清理旧字段之前使用。
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any


def _event_signature(event: dict[str, Any] | None) -> tuple[Any, Any, Any] | None:
    """提取事件的关键字段用于比较，忽略 seq 等迁移过程中可能变化的辅助字段。"""
    if not event:
        return None
    return (
        event.get("event_type"),
        event.get("data", {}),
        event.get("timestamp"),
    )


def compare_trace_events(
    legacy_events: list[dict[str, Any]],
    chunk_events: list[dict[str, Any]],
    *,
    event_types: list[str] | None = None,
) -> list[str]:
    """比较旧版内嵌事件列表与新版分块事件列表，返回发现的不一致描述列表。

    只做抽样式校验（总数 + 首尾事件），而不是逐条深度比较，
    足以发现"切分算法漏事件/重复事件/顺序错乱"这类典型迁移问题，
    同时避免对事件很多的 trace 逐条比较带来的开销。

    Args:
        legacy_events: 迁移前的原始事件列表（来自 traces.events）
        chunk_events: 从 trace_event_chunks 按 chunk_index 拼接还原出的事件列表
        event_types: 若提供，只比较 event_type 属于该集合的事件

    Returns:
        不一致描述字符串列表；空列表表示未发现不一致
    """
    mismatches: list[str] = []
    # 若指定了 event_types，先在两侧用同一规则过滤掉不关心的事件类型，保证公平对比
    if event_types:
        allowed = set(event_types)
        legacy_events = [event for event in legacy_events if event.get("event_type") in allowed]
        chunk_events = [event for event in chunk_events if event.get("event_type") in allowed]

    # 依次检查：事件总数是否一致、首个事件是否一致、最后一个事件是否一致
    if len(legacy_events) != len(chunk_events):
        mismatches.append(f"event_count legacy={len(legacy_events)} chunk={len(chunk_events)}")
    if _event_signature(legacy_events[0] if legacy_events else None) != _event_signature(
        chunk_events[0] if chunk_events else None
    ):
        mismatches.append("first_event mismatch")
    if _event_signature(legacy_events[-1] if legacy_events else None) != _event_signature(
        chunk_events[-1] if chunk_events else None
    ):
        mismatches.append("last_event mismatch")
    return mismatches


async def read_chunk_events(trace_storage: Any, trace_id: str) -> list[dict[str, Any]]:
    """从 trace_event_chunks 集合按 chunk_index 顺序读回并拼接出完整事件列表。

    Args:
        trace_storage: TraceStorage 实例（提供 chunks_collection）
        trace_id: 目标 trace 的 ID

    Returns:
        按 seq 顺序排列的事件列表；若该 trace 尚未迁移（没有任何 chunk），返回空列表
    """
    events: list[dict[str, Any]] = []
    # 按 chunk_index 升序遍历该 trace 的所有分块文档
    cursor = trace_storage.chunks_collection.find(
        {"trace_id": trace_id},
        {"_id": 0, "events": 1, "chunk_index": 1},
    ).sort("chunk_index", 1)
    async for chunk in cursor:
        # 块内事件再按 seq 排序一次（若事件没有 seq 字段则退化为按原始下标排序），
        # 不依赖数据库返回顺序
        chunk_events = sorted(
            enumerate(chunk.get("events", []) or []),
            key=lambda item: item[1].get("seq", item[0]),
        )
        events.extend(event for _index, event in chunk_events)
    return events


async def verify_trace(trace_storage: Any, trace_id: str, event_types: list[str]) -> list[str]:
    """针对单个 trace_id，同时读取旧版与新版事件数据并比较。

    Args:
        trace_storage: TraceStorage 实例
        trace_id: 目标 trace 的 ID
        event_types: 需要比较的事件类型列表，空列表表示比较全部类型

    Returns:
        compare_trace_events 返回的不一致描述列表
    """
    trace_doc = await trace_storage.collection.find_one(
        {"trace_id": trace_id},
        {"_id": 0, "events": 1},
    )
    # 旧版事件：直接读 traces 集合文档里内嵌的 events 字段
    # （若已用 --remove-legacy-events 迁移过，这里会是空列表）
    legacy_events = list((trace_doc or {}).get("events") or [])
    chunk_events = await read_chunk_events(trace_storage, trace_id)
    return compare_trace_events(
        legacy_events,
        chunk_events,
        event_types=event_types or None,
    )


async def run_verification(args: argparse.Namespace) -> int:
    """命令行校验流程：执行比较并打印结果，返回进程退出码。

    Returns:
        0 表示一致，1 表示发现不一致（供 shell/CI 判断本次校验是否通过）
    """
    from src.infra.session.trace_storage import get_trace_storage

    storage = get_trace_storage()
    # 将逗号分隔的字符串参数解析为列表，并过滤空字符串（未传该参数时 args.event_types 为空字符串）
    event_types = [item for item in args.event_types.split(",") if item]
    mismatches = await verify_trace(storage, args.trace_id, event_types)
    if mismatches:
        print({"trace_id": args.trace_id, "mismatches": mismatches})
        return 1
    print({"trace_id": args.trace_id, "status": "ok"})
    return 0


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="Verify legacy trace events against chunks.")
    parser.add_argument("--trace-id", required=True)
    # 逗号分隔的事件类型白名单，留空表示比较全部事件类型
    parser.add_argument("--event-types", default="")
    return parser.parse_args()


# 命令行入口：以校验结果作为进程退出码（0=一致，1=不一致），便于脚本化调用
def main() -> None:
    raise SystemExit(asyncio.run(run_verification(parse_args())))


# 支持直接执行：`python scripts/verify_trace_event_chunks.py --trace-id ...`
if __name__ == "__main__":
    main()

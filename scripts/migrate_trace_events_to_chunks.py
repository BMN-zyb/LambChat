"""数据迁移脚本：将旧版 trace 文档中内嵌的 events 数组迁移为独立的分块 (chunk) 文档。

背景：早期的 trace 存储方案把一次运行产生的所有事件都塞进 traces 集合中
单个文档的 events 数组字段里；当一次运行事件很多时，单个文档会越来越大，
有逼近/超过 MongoDB 单文档 16MB 大小上限的风险。新方案把 events 按固定大小
（settings.SESSION_EVENT_CHUNK_SIZE）切分成多条 trace_event_chunks 集合文档，
每条只保存一段连续的 seq 区间，从根本上避免单个文档过大。

用法:
    python scripts/migrate_trace_events_to_chunks.py [--session-id ID] [--trace-id ID]
        [--batch-size N] [--dry-run] [--remove-legacy-events]

运行方式与副作用:
    - 每次运行只处理 traces 集合中最多 --batch-size 条仍带有旧版 events
      字段的文档（默认 100 条），因此通常需要重复运行本脚本，直到打印的
      migrated 数量为 0，才代表全部迁移完成。
    - --dry-run 只打印将被迁移的 trace_id/事件数，不会写入任何数据，适合先预览范围。
    - 默认不会删除旧的 events 字段（即 remove_legacy_events 默认 False），
      新旧两份数据会同时存在，便于用 verify_trace_event_chunks.py 校验一致性；
      确认无误后再加上 --remove-legacy-events 重新运行以清理旧字段、释放空间。
    - 实际写库操作由 TraceStorage.replace_trace_events_with_chunks 完成：
      它会先删除该 trace 已有的 chunk 文档，再按当前 events 重新写入，
      因此对同一个 trace 重复执行本脚本是安全的（幂等）。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from typing import Any


# 生成带 UTC 时区信息的当前时间，用于写入 chunk 文档的 created_at/updated_at 字段
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_chunk_docs(
    trace_doc: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    chunk_size: int,
) -> list[dict[str, Any]]:
    """按 chunk_size 把一条 trace 的 events 列表切分成多个 chunk 文档（纯函数，不访问数据库）。

    这里单独实现一份切分逻辑（而不是直接复用 TraceStorage 内部方法），
    是为了能在不连接 MongoDB 的情况下用单元测试验证切分算法本身是否正确
    （seq 是否从 1 开始连续编号、chunk_index/start_seq/end_seq 计算是否准确）；
    实际迁移写库仍然通过 migrate_trace_doc -> replace_trace_events_with_chunks 完成。

    Args:
        trace_doc: 原始 trace 文档（用于取 trace_id/session_id/run_id/started_at）
        events: 该 trace 下的原始事件列表，顺序即事件发生顺序
        chunk_size: 每个 chunk 最多包含的事件数

    Returns:
        按 chunk_index 升序排列的 chunk 文档列表，可直接插入 trace_event_chunks 集合
    """
    trace_id = str(trace_doc.get("trace_id") or "")
    # chunk_size 至少为 1，防止外部传入 0 或负数时导致后面的 range() 死循环/除零
    size = max(int(chunk_size or 0), 1)
    now = _utc_now()
    # 重新赋值 seq（从 1 开始的连续序号），不信任事件里可能已存在的旧 seq，
    # 确保后续按区间切分时是连续、无空洞的
    normalized: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        item = dict(event)
        item["seq"] = index
        normalized.append(item)

    # 按固定步长 size 对事件列表做滑动切片，每一段生成一份独立的 chunk 文档
    chunks: list[dict[str, Any]] = []
    for offset in range(0, len(normalized), size):
        chunk_events = normalized[offset : offset + size]
        start_seq = int(chunk_events[0]["seq"])
        end_seq = int(chunk_events[-1]["seq"])
        chunks.append(
            {
                "trace_id": trace_id,
                "session_id": trace_doc.get("session_id", ""),
                "run_id": trace_doc.get("run_id", ""),
                "trace_started_at": trace_doc.get("started_at"),
                # chunk_index 由起始 seq 反推，需与 TraceStorage._event_chunk_index 的算法保持一致
                "chunk_index": (start_seq - 1) // size,
                "start_seq": start_seq,
                "end_seq": end_seq,
                "event_count": len(chunk_events),
                "events": chunk_events,
                "created_at": now,
                "updated_at": now,
            }
        )
    return chunks


async def migrate_trace_doc(
    trace_storage: Any,
    trace_doc: dict[str, Any],
    *,
    dry_run: bool,
    remove_legacy_events: bool,
) -> dict[str, Any]:
    """迁移单条 trace 文档：读取其内嵌 events，视情况写入 chunk 集合。

    Args:
        trace_storage: TraceStorage 实例（提供 replace_trace_events_with_chunks）
        trace_doc: 从 traces 集合读到的原始文档，需包含内嵌的 events 数组
        dry_run: 为 True 时只统计不写库，用于预览迁移范围
        remove_legacy_events: 转发给 replace_trace_events_with_chunks，
            控制迁移完成后是否顺带删除旧的 events 字段

    Returns:
        本次迁移的摘要 {trace_id, event_count, dry_run}，供调用方打印/记录
    """
    trace_id = str(trace_doc.get("trace_id") or "")
    events = list(trace_doc.get("events") or [])
    result = {"trace_id": trace_id, "event_count": len(events), "dry_run": dry_run}
    # dry-run 模式下到此为止，不做任何写操作
    if dry_run:
        return result

    # 真正的写库操作：内部会先删除该 trace 已有的 chunk 文档，再按当前 events 重新生成，
    # 因此本函数可以安全地对同一个 trace 重复调用
    await trace_storage.replace_trace_events_with_chunks(
        trace_doc,
        events,
        remove_legacy_events=remove_legacy_events,
    )
    return result


async def run_migration(args: argparse.Namespace) -> None:
    """按命令行参数批量迁移 traces 集合中仍带有旧版 events 字段的文档。

    每次调用只处理一批（最多 args.batch_size 条），因此需要重复运行本脚本，
    直到最后打印的 {"migrated": 0, ...} 表示已经没有待迁移的文档为止。
    """
    from src.infra.session.trace_storage import get_trace_storage

    storage = get_trace_storage()
    # 只筛选仍保留旧版内嵌 events 字段的文档；一旦迁移时带上 --remove-legacy-events，
    # 该字段会被 $unset 掉，文档自然不会再被本查询选中，避免重复迁移
    query: dict[str, Any] = {"events": {"$exists": True}}
    if args.trace_id:
        query["trace_id"] = args.trace_id
    if args.session_id:
        query["session_id"] = args.session_id

    # 每次运行只取一批，避免一次性加载过多大文档；未指定排序，按 MongoDB 自然顺序返回
    cursor = storage.collection.find(query).limit(max(int(args.batch_size), 1))
    migrated = 0
    async for trace_doc in cursor:
        summary = await migrate_trace_doc(
            storage,
            trace_doc,
            dry_run=args.dry_run,
            remove_legacy_events=args.remove_legacy_events,
        )
        migrated += 1
        print(summary)
    # migrated 为 0 说明本次查询已无匹配文档，即全部迁移完成，可以停止重复执行本脚本
    print({"migrated": migrated, "dry_run": args.dry_run})


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="Migrate legacy trace events into chunks.")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--trace-id", default="")
    # 每次运行处理的文档数上限，需要多次运行才能迁移完全部历史数据
    parser.add_argument("--batch-size", type=int, default=100)
    # 仅打印将被迁移的内容，不实际写库
    parser.add_argument("--dry-run", action="store_true")
    # 迁移后同时删除旧的 events 字段；默认保留，便于先用 verify 脚本核对再清理
    parser.add_argument("--remove-legacy-events", action="store_true")
    return parser.parse_args()


# 命令行入口：解析参数后以 asyncio 运行迁移协程
def main() -> None:
    asyncio.run(run_migration(parse_args()))


# 支持直接执行：`python scripts/migrate_trace_events_to_chunks.py ...`
if __name__ == "__main__":
    main()

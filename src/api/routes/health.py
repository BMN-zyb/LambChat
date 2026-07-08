"""
健康检查路由
"""

# 健康检查路由模块（挂载于根路径，提供 /health、/ready、/health/memory）
# 职责：存活探针（/health 返回服务状态与版本）、就绪探针（/ready）、以及需要权限的详细内存诊断
# /health 附带内存概况；/health/memory 汇总集群内各实例的内存快照，用于排查内存泄漏
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import require_permissions
from src.infra.monitoring import get_memory_monitor
from src.kernel.config import settings
from src.kernel.schemas.agent import HealthResponse, MemoryHealthSummary

router = APIRouter()


# 工具函数：把字节数格式化为保留两位小数的 "xxMB" 字符串；传入 None 时原样返回 None
def _format_mb(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{round(value / 1024 / 1024, 2)}MB"


# 根据内存监控 summary 构建概览字典：状态 + RSS/VMS/增长量 + 线程数/打开文件数等（字节值统一格式化为 MB）
def _build_memory_overview(summary: dict) -> dict:
    # 监控不可用时状态为 unavailable；可用时按是否疑似泄漏区分 suspected_leak / stable
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


# 构建"重点提示"列表：从 summary 与最近一次告警中提取最值得关注的几条信息，供前端高亮展示
def _build_highlight_items(summary: dict, last_alert: dict | None) -> list[dict]:
    # 监控不可用：直接返回一条 unavailable 提示（附原因）
    if not summary.get("available"):
        reason = summary.get("reason", "unknown")
        return [{"kind": "unavailable", "reason": reason}]

    # 第一条固定为整体状态：疑似泄漏标 warning，否则 info
    highlights = [
        {
            "kind": "status",
            "status": "suspected_leak" if summary.get("suspected_leak") else "stable",
        }
    ]
    if summary.get("suspected_leak"):
        highlights[0]["severity"] = "warning"
    else:
        highlights[0]["severity"] = "info"

    # 取增长最多的一处内存位置（若有）
    growth_rows = (last_alert or {}).get("top_growth") or []
    if growth_rows:
        top = growth_rows[0]
        highlights.append(
            {
                "kind": "top_growth",
                "location": top["location"],
                "size_diff": _format_mb(top["size_diff_bytes"]) or "N/A",
            }
        )

    # 取占用最大的一处内存分配（若有）
    allocation_rows = (last_alert or {}).get("top_allocations") or []
    if allocation_rows:
        top = allocation_rows[0]
        highlights.append(
            {
                "kind": "top_allocation",
                "location": top["location"],
                "size": _format_mb(top["size_bytes"]) or "N/A",
            }
        )

    # 取数量最多的一类对象（若有）
    object_rows = (last_alert or {}).get("top_object_types") or []
    if object_rows:
        top = object_rows[0]
        highlights.append(
            {
                "kind": "top_object_type",
                "type": top["type"],
                "count": top["count"],
            }
        )

    return highlights


# 将"内存增长"明细行批量格式化：为每行补充人类可读的 size_diff（MB）字段
def _format_growth_rows(rows: list[dict] | None) -> list[dict]:
    return [
        {
            **row,
            "size_diff": _format_mb(row.get("size_diff_bytes")),
        }
        for row in (rows or [])
    ]


# 将"内存分配"明细行批量格式化：为每行补充人类可读的 size（MB）字段
def _format_allocation_rows(rows: list[dict] | None) -> list[dict]:
    return [
        {
            **row,
            "size": _format_mb(row.get("size_bytes")),
        }
        for row in (rows or [])
    ]


# 将"对象类型"明细行批量格式化：为每行补充 "类型=数量" 的展示标签
def _format_object_rows(rows: list[dict] | None) -> list[dict]:
    return [
        {
            **row,
            "label": f"{row['type']}={row['count']}",
        }
        for row in (rows or [])
    ]


# GET /health —— 存活探针（无需鉴权）：返回状态 ok、应用版本，并附带内存监控概况
@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """健康检查"""
    summary = await get_memory_monitor().get_summary()
    return HealthResponse(
        status="ok",
        version=settings.APP_VERSION,
        memory=MemoryHealthSummary.model_validate(summary),
    )


# GET /ready —— 就绪探针（无需鉴权）：仅返回 {"status": "ready"}，供负载均衡/编排判断能否接入流量
@router.get("/ready")
async def readiness_check():
    """就绪检查"""
    return {"status": "ready"}


# GET /health/memory —— 详细内存诊断，需要 settings:manage 权限
# refresh=True 时强制重新采样；会发布本实例快照并汇总集群内所有实例快照，辅助定位内存泄漏
@router.get("/health/memory")
async def memory_health_check(
    refresh: bool = False,
    _=Depends(require_permissions("settings:manage")),
):
    """详细内存诊断"""
    from src.infra.monitoring.distributed_memory_health import (
        build_cluster_overview,
        build_instance_snapshot,
        get_instance_id,
        load_cluster_snapshots,
        publish_instance_snapshot,
    )

    diagnostics = await get_memory_monitor().get_diagnostics(refresh=refresh)
    summary = diagnostics.get("summary", {})
    last_alert = diagnostics.get("last_alert") or diagnostics.get("current_snapshot") or {}

    # 生成本实例的内存快照（实例 id + summary + 最近告警明细）
    local_instance_id = get_instance_id()
    local_snapshot = build_instance_snapshot(
        instance_id=local_instance_id,
        summary=summary,
        details=last_alert,
    )

    # 把本实例快照发布到共享存储（最多每 60s 一次），供聚合使用；失败则静默忽略，不影响接口
    try:
        await publish_instance_snapshot(local_snapshot, interval_seconds=60.0)
    except Exception:
        pass

    # 读取集群内所有实例的快照，并逐条标注哪一条属于当前实例
    cluster_snapshots = await load_cluster_snapshots()

    for snapshot in cluster_snapshots:
        snapshot["is_local"] = snapshot.get("instance_id") == local_instance_id

    # 若共享存储里还没有本实例（如刚发布未同步），补入本地快照，保证结果至少包含自己
    local_in_cluster = any(s.get("is_local") for s in cluster_snapshots)
    if not local_in_cluster:
        local_snapshot["is_local"] = True
        cluster_snapshots.append(local_snapshot)

    # 汇总集群概览（实例数、可用实例数、疑似泄漏实例数）并选出一个重点展示实例
    cluster_overview = build_cluster_overview(
        cluster_snapshots,
        local_instance_id=local_instance_id,
    )
    selected_instance = cluster_overview["selected_instance"]

    # 返回聚合结果：优先取选中实例的现成字段，缺失时回退到基于本地 summary/last_alert 的即时计算
    return {
        "local_instance_id": local_instance_id,
        "cluster_overview": {
            "instance_count": cluster_overview["instance_count"],
            "available_instance_count": cluster_overview["available_instance_count"],
            "suspected_leak_count": cluster_overview["suspected_leak_count"],
            "selected_instance_id": cluster_overview["selected_instance_id"],
        },
        "instances": cluster_snapshots,
        "selected_instance": selected_instance,
        "summary": summary,
        "last_alert": diagnostics.get("last_alert"),
        "last_error": diagnostics.get("last_error"),
        "current_snapshot": diagnostics.get("current_snapshot"),
        "overview": selected_instance.get("overview", _build_memory_overview(summary)),
        "highlights": selected_instance.get(
            "highlights", _build_highlight_items(summary, last_alert)
        ),
        "top_growth": selected_instance.get(
            "top_growth", _format_growth_rows(last_alert.get("top_growth"))
        ),
        "top_allocations": selected_instance.get(
            "top_allocations", _format_allocation_rows(last_alert.get("top_allocations"))
        ),
        "top_objects": selected_instance.get(
            "top_objects", _format_object_rows(last_alert.get("top_object_types"))
        ),
    }

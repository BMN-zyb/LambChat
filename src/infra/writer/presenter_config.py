"""Presenter 配置与工具函数"""
# 中文说明：本模块是 Presenter（统一 SSE 事件输出器，见 present.py）的配套设施，
# 提供：
#   1）PresenterConfig 数据类——描述一次 Presenter 运行所需的会话/用户/追踪信息；
#   2）run_id/trace_id 的生成规则——用于把同一次对话运行中产生的所有事件、
#      日志、LangSmith 追踪串联起来；
#   3）附件（attachment）相关的小工具——用于从事件负载里提炼/裁剪附件信息，
#      避免把过多附件明细重复写入事件流。

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.infra.utils.datetime import utc_now

# 单次事件/请求中最多提取/保留的附件 key 数量，避免附件列表无限增长
ATTACHMENT_KEYS_MAX = 100


def should_increment_unread_for_trace_status(status: str) -> bool:
    """Return whether a trace terminal status should require user attention."""
    # 中文：只有 trace 走到"完成"或"出错"这类终态时，才需要提醒用户查看
    # （递增未读计数），中间过程状态（如 running）不应触发未读提醒
    return status in {"completed", "error"}


def _extract_attachment_keys(attachments: Optional[List[Dict[str, Any]]]) -> list[str]:
    """Extract unique storage keys from attachment payloads."""
    if not attachments:
        return []
    keys: list[str] = []
    seen = set()
    # 中文：按出现顺序去重，同时限制最多收集 ATTACHMENT_KEYS_MAX 个 key，
    # 防止一次消息附带海量附件时把 key 列表撑得过大
    for attachment in attachments:
        key = str(attachment.get("key", "")).strip() if attachment.get("key") else ""
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
        if len(keys) >= ATTACHMENT_KEYS_MAX:
            break
    return keys


def _bounded_attachments(
    attachments: Optional[List[Dict[str, Any]]],
    *,
    limit: int = ATTACHMENT_KEYS_MAX,
) -> list[Dict[str, Any]]:
    # 中文：与 _extract_attachment_keys 类似，但保留完整附件对象（而非只取 key），
    # 同样做数量截断，供需要附件全量信息（而不仅是 key）的场景使用
    if not attachments:
        return []
    return list(attachments[:limit])


def _generate_trace_id() -> str:
    """生成唯一 trace_id (时间戳 + 完整 UUID，确保不重复)"""
    # 时间戳前缀方便按时间排序/肉眼定位，UUID 后缀保证唯一性
    ts = utc_now().strftime("%Y%m%d%H%M%S%f")
    return f"trace_{ts}_{uuid.uuid4().hex}"


def _generate_run_id() -> str:
    """生成唯一 run_id (时间戳 + 完整 UUID，用于 LangSmith 关联)"""
    # 与 _generate_trace_id 同构，专用于关联 LangSmith 的一次 run
    ts = utc_now().strftime("%Y%m%d%H%M%S%f")
    return f"run_{ts}_{uuid.uuid4().hex}"


@dataclass
class PresenterConfig:
    """Presenter 配置"""

    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_name: str = "Agent"
    user_id: Optional[str] = None  # 用户 ID，用于绑定 session
    run_id: Optional[str] = None  # 运行 ID
    trace_id: Optional[str] = None  # Trace ID (自动生成或手动指定)
    chunk_delay: float = 0.0  # 流式输出延迟 (秒)
    max_result_length: int = 100000  # 结果最大长度
    enable_storage: bool = True  # 是否启用事件存储

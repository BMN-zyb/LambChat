"""Shared private helpers for middleware modules."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import SystemMessage


def _normalize_prompt_text(text: str) -> str:
    """Normalize injected prompt sections so equivalent content has the same shape."""
    # 归一化提示文本：去首尾空白 + 每行去尾空格，保证等价内容产生完全相同的字符串
    # （用于提示注入去重与提示缓存命中的一致性）
    return "\n".join(line.rstrip() for line in text.strip().splitlines()).strip()


def _system_message_to_blocks(system_message: Any) -> list[Any]:
    """Convert a system message payload into mutable content blocks."""
    # 把 system message 统一转成可变的内容块列表，便于后续追加注入内容
    if system_message is None:
        return []

    content = getattr(system_message, "content", None)
    if content is None:
        return []

    # 字符串内容：归一化后包成单个 text 块
    if isinstance(content, str):
        normalized = _normalize_prompt_text(content)
        return [{"type": "text", "text": normalized}] if normalized else []

    # 已是块列表：复制一份返回（避免就地修改原消息）
    if isinstance(content, list):
        return list(content)

    return []


def _append_system_text_block(system_message: Any, text: str) -> SystemMessage:
    """Append a deterministic text block to the system message."""
    # 向 system message 追加一个归一化文本块，返回新的 SystemMessage
    normalized = _normalize_prompt_text(text)
    blocks = _system_message_to_blocks(system_message)
    if normalized:
        blocks.append({"type": "text", "text": normalized})
    return SystemMessage(content=blocks)


def _append_system_text_blocks(
    system_message: Any, texts: list[str] | tuple[str, ...]
) -> SystemMessage:
    """Append multiple deterministic text blocks to the system message."""
    # 批量追加多个归一化文本块（空块跳过）
    blocks = _system_message_to_blocks(system_message)
    for text in texts:
        normalized = _normalize_prompt_text(text)
        if normalized:
            blocks.append({"type": "text", "text": normalized})
    return SystemMessage(content=blocks)


def _tool_sort_key(tool: Any) -> tuple[str, str]:
    """Stable ordering for dynamically appended tools."""
    # 动态追加工具时按 (server, name) 稳定排序，保证工具顺序确定（利于提示缓存）
    name = getattr(tool, "name", "") or ""
    server = getattr(tool, "server", "") or ""
    return (server, name)

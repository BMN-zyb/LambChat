"""Normalize LangGraph and MCP tool outputs for presenter events."""

import json
from itertools import islice
from typing import Any

from src.infra.agent.events.types import TOOL_ERROR_INDICATORS

# MCP 媒体块类型：这些类型的块被当作媒体（图片/文件）而非文本
MCP_MEDIA_TYPES = frozenset(("image", "file"))
# 归一化媒体块时需要剔除的键（如内部 id）
MCP_SKIP_KEYS = frozenset(("id",))
# 由错误关键字集合转成 tuple，供 str.startswith 判断错误前缀
TOOL_ERROR_PREFIXES = tuple(TOOL_ERROR_INDICATORS)
# 序列化工具输出时的防爆上限：字符串长度、列表项数、字典键数、递归深度
TOOL_OUTPUT_SERIALIZE_MAX_STRING_CHARS = 100_000
TOOL_OUTPUT_SERIALIZE_MAX_LIST_ITEMS = 100
TOOL_OUTPUT_SERIALIZE_MAX_DICT_ITEMS = 100
TOOL_OUTPUT_SERIALIZE_MAX_DEPTH = 8


def _compact_serializable_value(value: Any, *, depth: int = 0) -> Any:
    # 递归压缩可序列化值：对超深/超长/超量做截断，防止工具输出把内存/前端撑爆
    if depth >= TOOL_OUTPUT_SERIALIZE_MAX_DEPTH:
        return "[truncated: max depth exceeded]"

    if isinstance(value, str):
        # 字符串超限则截断并标注原长
        if len(value) <= TOOL_OUTPUT_SERIALIZE_MAX_STRING_CHARS:
            return value
        return (
            value[:TOOL_OUTPUT_SERIALIZE_MAX_STRING_CHARS].rstrip()
            + f"\n[truncated from {len(value)} chars]"
        )

    if isinstance(value, list):
        # 列表逐项递归压缩，超出上限的以计数占位
        items = [
            _compact_serializable_value(item, depth=depth + 1)
            for item in value[:TOOL_OUTPUT_SERIALIZE_MAX_LIST_ITEMS]
        ]
        omitted = len(value) - TOOL_OUTPUT_SERIALIZE_MAX_LIST_ITEMS
        if omitted > 0:
            items.append({"_truncated_items": omitted})
        return items

    if isinstance(value, dict):
        # 字典按 islice 取前 N 个键递归压缩，剩余键数记为 _truncated_keys
        compacted: dict[Any, Any] = {}
        key_limit = TOOL_OUTPUT_SERIALIZE_MAX_DICT_ITEMS
        for key in islice(value, key_limit):
            compacted[key] = _compact_serializable_value(value[key], depth=depth + 1)
        omitted = len(value) - key_limit
        if omitted > 0:
            compacted["_truncated_keys"] = omitted
        return compacted

    # 其余标量类型原样返回
    return value


def _json_dumps_compacted(value: Any) -> str:
    # 先压缩再序列化为 JSON 字符串（default=str 兜底不可序列化对象）
    return json.dumps(
        _compact_serializable_value(value),
        ensure_ascii=False,
        default=str,
    )


def _compact_text(value: str) -> str:
    # 文本压缩的便捷入口（等价于对字符串调用 _compact_serializable_value）
    return _compact_serializable_value(value)


def extract_tool_output(out: Any) -> Any:
    """Extract displayable content from LangGraph tool node output."""
    # None -> 空串
    if out is None:
        return ""
    # 已是字符串直接返回
    if isinstance(out, str):
        return out

    # 非 dict/list/str 的对象（如 ToolMessage、Command 等）走属性提取
    if not isinstance(out, (dict, list, str)):
        # tuple(content, artifact) 形态：取第一个元素递归
        if isinstance(out, tuple) and len(out) >= 1:
            return extract_tool_output(out[0])
        # Command.update 形态：优先取其中的 messages
        update = getattr(out, "update", None)
        if isinstance(update, dict):
            messages = update.get("messages")
            if messages:
                return process_messages(messages)
            return update
        # 普通消息对象：取 content
        content = getattr(out, "content", None)
        if content is not None:
            return normalize_content(content)
        # 退回 artifact（结构化产物）
        artifact = getattr(out, "artifact", None)
        if artifact is not None:
            return artifact
        return ""

    if isinstance(out, list):
        # 列表首元素不是 dict/str -> 视为消息对象列表；否则视为内容块列表
        if out and not isinstance(out[0], (dict, str)):
            return process_messages(out)
        return normalize_content(out)

    # 到这里 out 必为 dict（前面已排除其它类型），但保留防御性判断
    if not isinstance(out, dict):
        return out

    # dict 里的 update.messages 优先
    update = out.get("update")
    if isinstance(update, dict):
        messages = update.get("messages")
        if messages:
            return process_messages(messages)
        return update

    # 直接带 content 字段
    if "content" in out:
        return normalize_content(out["content"])

    # 嵌套 output 字段
    nested = out.get("output")
    if nested is not None:
        if isinstance(nested, dict):
            return normalize_content(nested.get("content", nested))
        return nested

    return out


def detect_tool_error(out: Any, raw: Any) -> tuple[bool, str | None]:
    """Detect tool errors from status fields first, then conservative content markers."""
    # 第一优先：ToolMessage.status == "error"
    tool_status = get_tool_status(out)
    if tool_status == "error":
        return True, str(raw) if raw else "Tool execution failed"

    # 第二：结构化结果里显式的 error/status 字段
    if isinstance(raw, dict) and (raw.get("error") or raw.get("status") == "error"):
        return True, raw.get("error") or raw.get("message") or str(raw)

    # 第三（保守）：文本首行以已知错误关键字开头才判定为错误，避免误判正常输出
    if isinstance(raw, str) and raw:
        first_line = raw.lstrip()[:200].lower()
        if first_line.startswith(TOOL_ERROR_PREFIXES):
            return True, raw

    return False, None


def _get_status_attr(obj: Any) -> str | None:
    # 安全读取对象的 status 字符串属性
    status = getattr(obj, "status", None)
    if status and isinstance(status, str):
        return status
    return None


def get_tool_status(out: Any) -> str | None:
    """Find ToolMessage.status through common LangGraph wrappers."""
    if out is None:
        return None

    # 非容器对象：先看自身 status，再穿透 update.messages
    if not isinstance(out, (dict, list, str)):
        status = _get_status_attr(out)
        if status:
            return status
        update = getattr(out, "update", None)
        if isinstance(update, dict):
            return get_tool_status(update.get("messages"))
        return None

    # 列表：跳过 dict/str，找第一个带 status 的消息对象
    if isinstance(out, list):
        for item in out:
            if isinstance(item, (dict, str)):
                continue
            status = _get_status_attr(item)
            if status:
                return status
        return None

    # dict：穿透 update.messages 继续查找
    if isinstance(out, dict):
        update = out.get("update")
        if isinstance(update, dict):
            return get_tool_status(update.get("messages"))

    return None


def collect_blocks(content: list, text_parts: list[str], media_blocks: list[dict]) -> bool:
    """Collect text and media blocks from MCP-style content lists."""
    # 就地把文本累积到 text_parts、媒体累积到 media_blocks，返回是否含媒体
    has_media = False

    for block in content:
        # 嵌套列表：先递归归一化再按结果类型合并
        if isinstance(block, list):
            nested = normalize_content(block)
            if isinstance(nested, str):
                text_parts.append(nested)
            elif isinstance(nested, dict):
                if "text" in nested:
                    text_parts.append(nested["text"])
                if "blocks" in nested:
                    media_blocks.extend(nested["blocks"])
                    has_media = True
            continue

        # 非 dict 块：直接字符串化后计入文本
        if not isinstance(block, dict):
            text_parts.append(str(block) if block is not None else "")
            continue

        block_type = block.get("type", "")
        if block_type == "text":
            # 文本块：压缩后计入文本
            text = block.get("text")
            text_parts.append(_compact_text(str(text)) if text is not None else "")
        elif block_type in MCP_MEDIA_TYPES:
            # 显式媒体块（image/file）：剔除 id 后计入媒体
            media_blocks.append(
                {key: value for key, value in block.items() if key not in MCP_SKIP_KEYS}
                if "id" in block
                else block
            )
            has_media = True
        elif "text" in block:
            # 无 type 但带 text 字段：当作文本
            text_parts.append(_compact_text(str(block["text"])))
        else:
            # 其余未知块：保守地当作媒体块保留
            media_blocks.append(
                {key: value for key, value in block.items() if key not in MCP_SKIP_KEYS}
                if "id" in block
                else block
            )
            has_media = True

    return has_media


def normalize_content(content: Any) -> Any:
    """Normalize MCP content blocks into text or structured media payloads."""
    # 字符串/字典原样返回
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content
    # 非列表则字符串化
    if not isinstance(content, list):
        return str(content)

    # 列表内容：拆分为文本片段与媒体块
    text_parts: list[str] = []
    media_blocks: list[dict] = []
    collect_blocks(content, text_parts, media_blocks)

    # 含媒体时返回 {text, blocks} 结构，供后续上传/展示
    if media_blocks:
        return {"text": "".join(text_parts), "blocks": media_blocks}

    # 纯文本时拼接返回；全空则退回原始 content
    text_result = "".join(text_parts)
    return text_result if text_result else content


def process_messages(messages: list) -> Any:
    """Extract and merge message content while preserving MCP media blocks."""
    # 合并一组消息的内容，同时保留其中的 MCP 媒体块
    text_parts: list[str] = []
    media_blocks: list[dict] = []
    has_media = False

    for message in messages:
        # 兼容 dict 消息与消息对象两种形态
        if isinstance(message, dict):
            content = message.get("content", "")
            artifact = message.get("artifact")
        else:
            content = getattr(message, "content", "")
            artifact = getattr(message, "artifact", None)

        # Prefer content (human-readable) over artifact (structured metadata)
        # 优先用可读的 content；字符串内容直接压缩计入
        if isinstance(content, str) and content:
            text_parts.append(_compact_text(content))
            continue
        # 列表内容走块收集
        if isinstance(content, list):
            if collect_blocks(content, text_parts, media_blocks):
                has_media = True
            continue
        # content 为空时退回 artifact（结构化元数据）序列化
        if artifact is not None:
            text_parts.append(_json_dumps_compacted(artifact))
            continue

        # Fallback: stringify whatever content is
        # 兜底：dict 内容 JSON 序列化，其余非空内容字符串化
        if isinstance(content, dict):
            text_parts.append(_json_dumps_compacted(content))
        elif content is not None:
            text_parts.append(_compact_text(str(content)))

    # 多条消息文本以换行连接；含媒体则返回结构化结果
    text_result = "\n".join(text_parts)
    if has_media:
        return {"text": text_result, "blocks": media_blocks}
    return text_result

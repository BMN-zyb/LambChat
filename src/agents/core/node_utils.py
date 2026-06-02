"""
Agent 节点共享工具函数

从 search_agent/nodes.py 和 fast_agent/nodes.py 中提取的公共逻辑。
"""

from __future__ import annotations

import base64
from tempfile import SpooledTemporaryFile
from urllib.parse import quote

from langchain_core.messages import HumanMessage

from src.infra.agent import AgentEventProcessor
from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger

logger = get_logger(__name__)
IMAGE_DATA_URL_INLINE_MAX_BYTES = 2 * 1024 * 1024
IMAGE_DATA_URL_SPOOL_MAX_MEMORY_BYTES = 256 * 1024
IMAGE_DATA_URL_ENCODE_CHUNK_BYTES = 192 * 1024


async def resolve_fallback_model(
    model_id: str | None,
    selected_model: str | None,
    *,
    log_prefix: str = "",
) -> str | None:
    """从 DB 解析 fallback_model ID 到实际的 model value。

    Args:
        model_id: 当前模型的 DB ID（优先）
        selected_model: 当前模型的 value 字符串（备选）
        log_prefix: 日志前缀，如 "[FastAgent]" 或 "[Agent]"

    Returns:
        fallback model 的 value 字符串，或 None（无 fallback / 查询失败）
    """
    from src.infra.agent.model_storage import get_model_storage

    storage = get_model_storage()
    db_model = None

    try:
        if model_id:
            db_model = await storage.get(model_id)
        elif selected_model:
            db_model = await storage.get_by_value(selected_model)
    except Exception as e:
        logger.warning("%s Failed to lookup model config: %s", log_prefix, e)
        return None

    if not db_model or not db_model.fallback_model:
        return None

    try:
        fallback_db = await storage.get(db_model.fallback_model)
    except Exception as e:
        logger.warning("%s Failed to lookup fallback model: %s", log_prefix, e)
        return None

    if fallback_db:
        logger.info(
            "%s Fallback model: %s (%s)",
            log_prefix,
            fallback_db.label,
            fallback_db.value,
        )
        return fallback_db.value

    return None


async def resolve_model_supports_vision(
    model_id: str | None,
    selected_model: str | None,
    *,
    log_prefix: str = "",
) -> bool:
    """Resolve whether the selected model is configured for image input."""
    if not model_id and not selected_model:
        return False

    from src.infra.agent.model_storage import get_model_storage

    storage = get_model_storage()
    db_model = None

    try:
        if model_id:
            db_model = await storage.get(model_id)
        elif selected_model:
            db_model = await storage.get_by_value(selected_model)
    except Exception as e:
        logger.warning("%s Failed to lookup model vision capability: %s", log_prefix, e)
        return False

    if not db_model or not getattr(db_model, "profile", None):
        return False

    return bool(getattr(db_model.profile, "supports_vision", False))


def _is_image_attachment(attachment: dict) -> bool:
    file_type = str(attachment.get("type", "")).lower()
    mime_type = str(attachment.get("mime_type") or attachment.get("mimeType") or "").lower()
    return file_type == "image" or mime_type.startswith("image/")


def _attachment_url_from_key(key: object, base_url: str) -> str:
    clean_base_url = base_url.rstrip("/")
    quoted_key = quote(str(key).lstrip("/"), safe="/")
    return f"{clean_base_url}/api/upload/file/{quoted_key}"


def _base64_encode_file(file) -> str:
    parts: list[str] = []
    while True:
        chunk = file.read(IMAGE_DATA_URL_ENCODE_CHUNK_BYTES)
        if not chunk:
            break
        parts.append(base64.b64encode(chunk).decode("ascii"))
    return "".join(parts)


async def _download_image_as_data_url(
    storage,
    key: object,
    mime_type: str,
    *,
    max_bytes: int,
) -> str | None:
    with SpooledTemporaryFile(
        max_size=IMAGE_DATA_URL_SPOOL_MAX_MEMORY_BYTES,
        mode="w+b",
    ) as spooled:
        downloaded_size = await storage.download_to_file(str(key), spooled)
        if isinstance(downloaded_size, int) and downloaded_size > max_bytes:
            return None
        encoded = await run_blocking_io(_base64_encode_file, spooled)
    return f"data:{mime_type};base64,{encoded}"


async def inline_image_attachments_as_data_urls(
    attachments: list[dict] | None,
    *,
    base_url: str = "",
    max_inline_bytes: int = IMAGE_DATA_URL_INLINE_MAX_BYTES,
) -> list[dict]:
    """Return image attachments with URLs preferred over in-memory data URLs."""
    if not attachments:
        return []

    inlined: list[dict] = []
    storage = None

    for attachment in attachments:
        if not _is_image_attachment(attachment):
            inlined.append(attachment)
            continue

        if attachment.get("url") or attachment.get("data_url"):
            inlined.append(attachment)
            continue

        key = attachment.get("key")
        if not key:
            inlined.append(attachment)
            continue

        if base_url:
            inlined.append(
                {
                    **attachment,
                    "url": _attachment_url_from_key(key, base_url),
                }
            )
            continue

        size = attachment.get("size")
        if isinstance(size, int) and size > max_inline_bytes:
            inlined.append(attachment)
            continue

        try:
            if storage is None:
                from src.infra.storage.s3.service import get_or_init_storage

                storage = await get_or_init_storage()
            mime_type = attachment.get("mime_type") or attachment.get("mimeType") or "image/jpeg"
            data_url = await _download_image_as_data_url(
                storage,
                key,
                mime_type,
                max_bytes=max_inline_bytes,
            )
            if data_url is None:
                inlined.append(attachment)
                continue
        except Exception as e:
            logger.warning("Failed to inline image attachment %s: %s", key, e)
            inlined.append(attachment)
            continue

        inlined.append(
            {
                **attachment,
                "data_url": data_url,
            }
        )

    return inlined


def _format_attachment_summary(text: str, attachments: list[dict]) -> str:
    enhanced_text = text
    if not attachments:
        return enhanced_text

    enhanced_text += "\n\n---\n**User Uploaded Attachments:**"

    for attachment in attachments:
        url = attachment.get("url", "")
        name = attachment.get("name", "未知文件")
        file_type = attachment.get("type", "document")
        mime_type = attachment.get("mime_type") or attachment.get("mimeType") or ""
        size = attachment.get("size", 0)

        if not url:
            continue

        size_str = ""
        if size:
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size / (1024 * 1024):.1f} MB"

        enhanced_text += f"\n\n**[{name}]**"
        enhanced_text += f"\n- 类型: {file_type}"
        if mime_type:
            enhanced_text += f" ({mime_type})"
        if size_str:
            enhanced_text += f"\n- 大小: {size_str}"
        enhanced_text += f"\n- 链接: {url}"

    return enhanced_text


def build_human_message(
    text: str,
    attachments: list[dict] | None,
    *,
    supports_vision: bool = False,
) -> HumanMessage:
    """
    构建 HumanMessage，将附件信息以文本形式附加到消息中

    Args:
        text: 用户输入的文本
        attachments: 附件列表，每个附件包含:
            - url: 文件访问链接
            - type: 文件类型 (image/video/audio/document)
            - name: 文件名
            - mime_type: MIME 类型 (可选)
            - size: 文件大小 (可选)

    Returns:
        HumanMessage: 包含文本和附件信息的消息
    """
    if not attachments:
        return HumanMessage(content=text)

    multimodal_images: list[dict] = []
    text_summary_attachments: list[dict] = []

    for attachment in attachments:
        url = attachment.get("url")
        data_url = attachment.get("data_url")
        image_url = url or data_url
        if supports_vision and _is_image_attachment(attachment) and image_url:
            multimodal_images.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                }
            )
        elif url:
            text_summary_attachments.append(attachment)

    enhanced_text = _format_attachment_summary(text, text_summary_attachments)
    if not multimodal_images:
        return HumanMessage(content=enhanced_text)

    return HumanMessage(
        content=[
            {"type": "text", "text": enhanced_text},
            *multimodal_images,
        ]
    )


async def emit_token_usage(
    event_processor: AgentEventProcessor,
    presenter,
    start_time: float,
    *,
    model_id: str | None = None,
    model: str | None = None,
) -> None:
    """发送 token 使用统计事件"""
    import time

    duration = time.time() - start_time
    try:
        await event_processor.emit_token_usage(
            duration=duration,
            model_id=model_id,
            model=model,
        )
    except Exception as e:
        logger.warning(f"Failed to emit token:usage event: {e}")

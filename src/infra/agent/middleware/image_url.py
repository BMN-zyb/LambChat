"""Middleware for converting model image_url blocks to base64 data URLs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

from src.agents.core.node_utils import (
    IMAGE_DATA_URL_INLINE_MAX_BYTES,
    _download_image_url_as_data_url,
)
from src.infra.logging import get_logger

logger = get_logger(__name__)


def _image_url_from_block(block: Any) -> str | None:
    # 从一个内容块中提取图片 URL，兼容两种格式；非图片/无 URL 返回 None
    if not isinstance(block, dict):
        return None

    # OpenAI 风格：{"type": "image_url", "image_url": {"url": ...} 或 字符串}
    if block.get("type") == "image_url":
        image_url = block.get("image_url")
        if isinstance(image_url, dict):
            url = image_url.get("url")
        else:
            url = image_url
        return url if isinstance(url, str) and url else None

    # Anthropic 风格：{"type": "image", "source": {"type": "url", "url": ...}}
    if block.get("type") == "image":
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "url":
            url = source.get("url")
            return url if isinstance(url, str) and url else None

    return None


def _with_data_url(block: dict, data_url: str) -> dict:
    # 用转换后的 data URL 生成新块（不改原块），按原块格式回填
    # Anthropic 风格：把 source 从 url 改为 base64，并剥离 data URL 头部
    if block.get("type") == "image":
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "url":
            return {
                **block,
                "source": {
                    "type": "base64",
                    "media_type": source.get("media_type") or "image/jpeg",
                    "data": data_url.split(",", 1)[1] if "," in data_url else data_url,
                },
            }

    # OpenAI 风格：把 url 直接替换为 data URL
    image_url = block.get("image_url")
    if isinstance(image_url, dict):
        return {**block, "image_url": {**image_url, "url": data_url}}
    return {**block, "image_url": {"url": data_url}}


def _mime_type_from_block(block: dict) -> str:
    # 尽量从块里推断图片 MIME 类型，缺省回退到 image/jpeg
    image_url = block.get("image_url")
    if isinstance(image_url, dict):
        detail = image_url.get("mime_type") or image_url.get("media_type")
        if isinstance(detail, str) and detail.startswith("image/"):
            return detail
    source = block.get("source")
    if isinstance(source, dict):
        media_type = source.get("media_type")
        if isinstance(media_type, str) and media_type.startswith("image/"):
            return media_type
    return "image/jpeg"


class ImageUrlToBase64Middleware(AgentMiddleware):
    """Convert every outbound image_url block to a base64 data URL."""

    def __init__(self, *, max_inline_bytes: int = IMAGE_DATA_URL_INLINE_MAX_BYTES) -> None:
        super().__init__()
        # 内联图片体积上限，超限的图片下载会被拒绝
        self.max_inline_bytes = max_inline_bytes

    async def _convert_content_blocks(self, content: Any) -> Any:
        # 转换单条消息的内容块列表；无变化时返回原对象（供上层判断是否改动）
        if not isinstance(content, list):
            return content

        converted: list[Any] = []
        changed = False
        for block in content:
            url = _image_url_from_block(block)
            # 无 URL、已是 data URL、或非 dict 块，一律原样保留
            if not url or url.startswith("data:") or not isinstance(block, dict):
                converted.append(block)
                continue

            # 下载远程图片并转 base64 data URL，失败则降级为保留原 URL
            try:
                data_url = await _download_image_url_as_data_url(
                    url,
                    _mime_type_from_block(block),
                    max_bytes=self.max_inline_bytes,
                )
            except Exception as e:
                logger.warning("Failed to convert image_url to base64: %s", e)
                data_url = None

            if data_url:
                converted.append(_with_data_url(block, data_url))
                changed = True
            else:
                converted.append(block)

        # 无任何转换则返回原 content，避免不必要的消息重建
        return converted if changed else content

    async def _convert_messages(self, messages: list[Any]) -> list[Any]:
        # 遍历消息，转换其中的图片块；无变化返回原列表
        converted_messages: list[Any] = []
        changed = False

        for message in messages:
            content = getattr(message, "content", None)
            converted_content = await self._convert_content_blocks(content)
            # is 判等：内容未变则复用原消息
            if converted_content is content:
                converted_messages.append(message)
                continue

            changed = True
            # 优先用 model_copy 生成不可变副本；否则退回 copy + 赋值
            if hasattr(message, "model_copy"):
                converted_messages.append(message.model_copy(update={"content": converted_content}))
            else:
                clone = message.copy()
                clone.content = converted_content
                converted_messages.append(clone)

        return converted_messages if changed else messages

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        # 模型调用前钩子：把请求消息里的图片 URL 转为内联 base64 再交给下一环
        messages = await self._convert_messages(request.messages)
        # 有改动才 override 请求，减少无谓拷贝
        if messages is not request.messages:
            request = request.override(messages=messages)
        return await handler(request)

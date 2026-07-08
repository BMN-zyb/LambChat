"""Vision model image analysis tool for LambChat agents."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import sys
from typing import Annotated, Any
from urllib.parse import unquote, urlsplit

from langchain_core.tools import BaseTool, InjectedToolArg

from src.agents.core.node_utils import (
    IMAGE_DATA_URL_INLINE_MAX_BYTES,
    build_human_message,
    inline_image_attachments_as_data_urls,
)
from src.infra.async_utils import run_blocking_io
from src.infra.llm.client import LLMClient
from src.infra.logging import get_logger
from src.infra.tool.backend_utils import get_backend_from_runtime, get_base_url_from_runtime
from src.kernel.config import settings
from src.kernel.schemas.model import ModelConfig

try:
    from langchain.tools import ToolRuntime  # type: ignore[assignment]
except ImportError:  # pragma: no cover
    # 兼容旧版 langchain：无 ToolRuntime 时注入占位模块，避免注解导入失败
    _mod = type(sys)("langchain.tools")
    _mod.ToolRuntime = Any  # type: ignore[attr-defined]
    sys.modules.setdefault("langchain.tools", _mod)
    from langchain.tools import ToolRuntime  # type: ignore[assignment]

from langchain.tools import tool  # noqa: E402

logger = get_logger(__name__)

# 默认分析提示词：未指定 prompt 时使用
DEFAULT_IMAGE_ANALYSIS_PROMPT = "Describe the image clearly and objectively."
# 内部调用标记：标注这是工具内部发起的 LLM 调用（区别于用户直接对话），便于追踪与计费归类
IMAGE_ANALYSIS_INTERNAL_RUN_CONFIG = {
    "metadata": {"lc_source": "image_analysis_tool", "internal_tool_call": True},
    "tags": ["internal_tool_call", "image_analysis_tool"],
}
# 上传文件 URL 标记：命中该标记的引用不再走后端文件下载路径
_UPLOAD_FILE_MARKER = "/api/upload/file/"


async def _json_dumps_result(data: dict[str, Any]) -> str:
    # JSON 序列化放到线程池，避免阻塞事件循环
    return await run_blocking_io(json.dumps, data, ensure_ascii=False)


async def _resolve_model_config(reference: str) -> ModelConfig | None:
    # 将模型引用解析为 ModelConfig：先按 id 查，再按 value 查，兼容两种引用写法
    value = reference.strip()
    if not value:
        return None

    from src.infra.agent.model_storage import get_model_storage

    storage = get_model_storage()
    model = await storage.get(value)
    if model:
        return model
    return await storage.get_by_value(value)


def _image_attachments_from_urls(image_urls: list[str]) -> list[dict[str, Any]]:
    # 将 URL 列表规整为统一的附件结构，跳过空串；默认 mime 设为 image/jpeg
    attachments: list[dict[str, Any]] = []
    for index, image_url in enumerate(image_urls):
        url = str(image_url).strip()
        if not url:
            continue
        attachments.append(
            {
                "id": f"image-{index + 1}",
                "name": f"image-{index + 1}",
                "type": "image",
                "mime_type": "image/jpeg",
                "url": url,
            }
        )
    return attachments


def _backend_path_from_image_reference(image_ref: str) -> str | None:
    # 判断图片引用是否为"需从后端下载的本地/项目文件路径"，是则返回该路径，否则返回 None
    ref = image_ref.strip()
    # data URL 或上传文件 URL 无需下载
    if not ref or ref.startswith("data:") or _UPLOAD_FILE_MARKER in ref:
        return None

    parsed = urlsplit(ref)
    scheme = (parsed.scheme or "").lower()
    # http/https 为远程 URL，交由后续内联逻辑处理，非后端文件
    if scheme in {"http", "https"}:
        return None
    # file:// 取其解码后的路径
    if scheme == "file":
        return unquote(parsed.path or "")
    # 其他带 scheme 的一律不当作后端文件
    if scheme:
        return None
    # 无 scheme：视为后端相对文件路径
    return ref


def _guess_image_mime_type(file_path: str, content: bytes) -> str | None:
    # 推断图片 MIME：优先扩展名，其次按文件头魔数识别，最后尝试识别 SVG
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type and mime_type.startswith("image/"):
        return mime_type

    # 常见图片格式的字节签名（魔数）
    signatures = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"RIFF", "image/webp"),
        (b"BM", "image/bmp"),
    )
    for prefix, detected_mime_type in signatures:
        if content.startswith(prefix):
            return detected_mime_type
    # SVG 是文本格式，单独按起始标签判断
    if content.lstrip().startswith(b"<svg"):
        return "image/svg+xml"
    return None


async def _download_file_from_backend(backend: Any, file_path: str) -> bytes | None:
    # 从运行时后端下载文件字节：优先异步接口 adownload_files，回退同步 download_files
    if hasattr(backend, "adownload_files"):
        try:
            responses = await backend.adownload_files([file_path])
            if responses:
                resp = responses[0]
                if resp.content:
                    return resp.content
                if resp.error:
                    logger.warning(
                        "[image_analyze] Download error for %s: %s", file_path, resp.error
                    )
        except Exception as e:
            logger.warning("[image_analyze] adownload_files failed for %s: %s", file_path, e)

    if hasattr(backend, "download_files"):
        # 同步接口放到线程池执行，避免阻塞事件循环
        try:
            responses = await run_blocking_io(backend.download_files, [file_path])
            if responses:
                resp = responses[0]
                if resp.content:
                    return resp.content
                if resp.error:
                    logger.warning(
                        "[image_analyze] Download error for %s: %s", file_path, resp.error
                    )
        except Exception as e:
            logger.warning("[image_analyze] download_files failed for %s: %s", file_path, e)

    return None


async def _inline_backend_image_paths(
    attachments: list[dict[str, Any]],
    runtime: ToolRuntime | None,
) -> list[dict[str, Any]]:
    # 将"后端文件路径"型附件下载并内联为 data URL，使视觉模型能够直接读取
    backend = get_backend_from_runtime(runtime)
    if backend is None:
        return attachments

    resolved: list[dict[str, Any]] = []
    for attachment in attachments:
        url = str(attachment.get("url") or "")
        backend_path = _backend_path_from_image_reference(url)
        # 非后端文件路径，原样保留
        if backend_path is None:
            resolved.append(attachment)
            continue

        content = await _download_file_from_backend(backend, backend_path)
        # 下载失败：保留原附件（后续可能仍能按 URL 处理）
        if content is None:
            resolved.append(attachment)
            continue
        # 超过内联大小上限则拒绝内联，避免请求体过大
        if len(content) > IMAGE_DATA_URL_INLINE_MAX_BYTES:
            logger.warning(
                "[image_analyze] Refusing oversized backend image: %s size=%s max=%s",
                backend_path,
                len(content),
                IMAGE_DATA_URL_INLINE_MAX_BYTES,
            )
            resolved.append(attachment)
            continue

        # 无法识别为图片则不内联
        mime_type = _guess_image_mime_type(backend_path, content)
        if not mime_type:
            logger.warning(
                "[image_analyze] Backend file is not a recognized image: %s", backend_path
            )
            resolved.append(attachment)
            continue

        # base64 编码放到线程池，拼成 data URL 替换原 url 字段
        encoded = await run_blocking_io(base64.b64encode, content)
        data_url = f"data:{mime_type};base64,{encoded.decode('ascii')}"
        resolved.append(
            {
                **attachment,
                "name": os.path.basename(backend_path.rstrip("/")) or attachment.get("name"),
                "mime_type": mime_type,
                "url": None,
                "data_url": data_url,
                "size": len(content),
            }
        )

    return resolved


def _content_to_text(content: Any) -> str:
    # 把 LLM 返回的多形态 content 归一化为纯文本：
    # 字符串直接返回；列表则拼接其中的字符串与 {"text": ...} 片段
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    return str(content)


async def _call_with_retries(llm: Any, messages: list[Any]) -> Any:
    # 带指数退避的重试调用：最多尝试 IMAGE_ANALYSIS_MAX_ATTEMPTS 次
    max_attempts = max(1, int(getattr(settings, "IMAGE_ANALYSIS_MAX_ATTEMPTS", 3) or 3))
    base_delay = float(getattr(settings, "IMAGE_ANALYSIS_RETRY_DELAY", 1.0) or 0)

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await llm.ainvoke(messages, config=IMAGE_ANALYSIS_INTERNAL_RUN_CONFIG)
        except Exception as exc:
            last_exc = exc
            # 已是最后一次尝试则跳出并抛出
            if attempt >= max_attempts:
                break
            # 指数退避：base_delay * 2^(attempt-1)
            delay = base_delay * (2 ** max(0, attempt - 1))
            logger.warning(
                "[image_analyze] model call failed with %s (attempt %d/%d), retrying in %.1fs",
                type(exc).__name__,
                attempt,
                max_attempts,
                delay,
            )
            if delay > 0:
                await asyncio.sleep(delay)

    # 循环耗尽必有 last_exc；重新抛出最后一次异常
    assert last_exc is not None
    raise last_exc


@tool
async def image_analyze(
    image_urls: Annotated[
        list[str],
        "Image URLs or project file URLs to inspect. Provide one or more images.",
    ],
    prompt: Annotated[
        str,
        "Question or instruction for the vision model, such as what to describe or compare.",
    ] = DEFAULT_IMAGE_ANALYSIS_PROMPT,
    runtime: Annotated[ToolRuntime | None, InjectedToolArg] = None,
) -> str:
    """Analyze one or more images with the configured vision-language model."""
    try:
        # 校验：必须配置视觉模型 ID
        model_reference = str(getattr(settings, "IMAGE_ANALYSIS_MODEL_ID", "") or "").strip()
        if not model_reference:
            return await _json_dumps_result({"error": "IMAGE_ANALYSIS_MODEL_ID is not configured"})

        # 校验：模型存在且声明支持视觉能力
        model_config = await _resolve_model_config(model_reference)
        if not model_config:
            return await _json_dumps_result(
                {"error": "Configured IMAGE_ANALYSIS_MODEL_ID not found"}
            )
        if not model_config.profile or not model_config.profile.supports_vision:
            return await _json_dumps_result(
                {"error": "Configured IMAGE_ANALYSIS_MODEL_ID does not support vision"}
            )

        # 规整入参 URL 为附件；至少要有一张图
        attachments = _image_attachments_from_urls(image_urls)
        if not attachments:
            return await _json_dumps_result({"error": "image_urls must include at least one image"})

        # 先内联后端文件路径的图片
        attachments = await _inline_backend_image_paths(attachments, runtime)
        # 若模型要求 base64，则强制把远程 URL 也内联为 data URL
        force_data_url = bool(model_config.profile.image_url_to_base64)
        attachments = await inline_image_attachments_as_data_urls(
            attachments,
            base_url=get_base_url_from_runtime(runtime),
            force_data_url=force_data_url,
        )
        # 构造带图的 human 消息
        message = build_human_message(
            prompt or DEFAULT_IMAGE_ANALYSIS_PROMPT, attachments, supports_vision=True
        )
        # 若内容退化为纯字符串，说明没有可用图片被内联，视为无有效输入
        if isinstance(message.content, str):
            return await _json_dumps_result({"error": "No readable image URLs were provided"})

        # 取模型客户端并带重试调用，最后把结果归一化为文本
        llm = await LLMClient.get_model(model_config=model_config)
        response = await _call_with_retries(llm, [message])
        analysis = _content_to_text(getattr(response, "content", response))
        return await _json_dumps_result(
            {
                "success": True,
                "analysis": analysis,
                "model_id": model_config.id or model_reference,
            }
        )
    except Exception as exc:
        # 任意异常统一转 JSON 错误串返回，保证 Agent 不中断
        logger.warning("[image_analyze] failed: %s", exc)
        return await _json_dumps_result({"error": f"Image analysis failed: {exc}"})


def get_image_analysis_tool() -> BaseTool:
    # 工厂函数：返回图像分析工具对象，供注册中心装配
    return image_analyze

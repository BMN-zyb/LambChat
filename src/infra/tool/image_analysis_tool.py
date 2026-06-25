"""Vision model image analysis tool for LambChat agents."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolArg

from src.agents.core.node_utils import (
    build_human_message,
    inline_image_attachments_as_data_urls,
)
from src.infra.async_utils import run_blocking_io
from src.infra.llm.client import LLMClient
from src.infra.logging import get_logger
from src.infra.tool.backend_utils import get_base_url_from_runtime
from src.kernel.config import settings
from src.kernel.schemas.model import ModelConfig

try:
    from langchain.tools import ToolRuntime  # type: ignore[assignment]
except ImportError:  # pragma: no cover
    _mod = type(sys)("langchain.tools")
    _mod.ToolRuntime = Any  # type: ignore[attr-defined]
    sys.modules.setdefault("langchain.tools", _mod)
    from langchain.tools import ToolRuntime  # type: ignore[assignment]

from langchain.tools import tool  # noqa: E402

logger = get_logger(__name__)

DEFAULT_IMAGE_ANALYSIS_PROMPT = "Describe the image clearly and objectively."


async def _json_dumps_result(data: dict[str, Any]) -> str:
    return await run_blocking_io(json.dumps, data, ensure_ascii=False)


async def _resolve_model_config(reference: str) -> ModelConfig | None:
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


def _content_to_text(content: Any) -> str:
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
    max_attempts = max(1, int(getattr(settings, "IMAGE_ANALYSIS_MAX_ATTEMPTS", 3) or 3))
    base_delay = float(getattr(settings, "IMAGE_ANALYSIS_RETRY_DELAY", 1.0) or 0)

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await llm.ainvoke(messages)
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
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
        model_reference = str(getattr(settings, "IMAGE_ANALYSIS_MODEL_ID", "") or "").strip()
        if not model_reference:
            return await _json_dumps_result({"error": "IMAGE_ANALYSIS_MODEL_ID is not configured"})

        model_config = await _resolve_model_config(model_reference)
        if not model_config:
            return await _json_dumps_result(
                {"error": "Configured IMAGE_ANALYSIS_MODEL_ID not found"}
            )
        if not model_config.profile or not model_config.profile.supports_vision:
            return await _json_dumps_result(
                {"error": "Configured IMAGE_ANALYSIS_MODEL_ID does not support vision"}
            )

        attachments = _image_attachments_from_urls(image_urls)
        if not attachments:
            return await _json_dumps_result({"error": "image_urls must include at least one image"})

        force_data_url = bool(model_config.profile.image_url_to_base64)
        attachments = await inline_image_attachments_as_data_urls(
            attachments,
            base_url=get_base_url_from_runtime(runtime),
            force_data_url=force_data_url,
        )
        message = build_human_message(
            prompt or DEFAULT_IMAGE_ANALYSIS_PROMPT, attachments, supports_vision=True
        )
        if isinstance(message.content, str):
            return await _json_dumps_result({"error": "No readable image URLs were provided"})

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
        logger.warning("[image_analyze] failed: %s", exc)
        return await _json_dumps_result({"error": f"Image analysis failed: {exc}"})


def get_image_analysis_tool() -> BaseTool:
    return image_analyze

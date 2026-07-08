"""Audio transcription tool backed by OpenAI-compatible audio/transcriptions."""

from __future__ import annotations

import inspect
import json
import sys
from tempfile import SpooledTemporaryFile
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlparse

import httpx
from langchain_core.tools import BaseTool, InjectedToolArg
from openai import AsyncOpenAI

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.tool.backend_utils import get_base_url_from_runtime
from src.kernel.config import settings

if TYPE_CHECKING:
    from langchain.tools import ToolRuntime
else:
    # 运行期兼容：老版本 langchain 无 ToolRuntime 时，注入一个占位模块，
    # 让 InjectedToolArg 注解不至于导入失败（类型退化为 Any）
    try:
        from langchain.tools import ToolRuntime  # type: ignore[assignment]
    except ImportError:  # pragma: no cover
        _mod = type(sys)("langchain.tools")  # type: ignore[assignment]
        _mod.ToolRuntime = Any  # type: ignore[assignment]
        sys.modules.setdefault("langchain.tools", _mod)
        from langchain.tools import ToolRuntime  # type: ignore[assignment]

from langchain.tools import tool  # noqa: E402

logger = get_logger(__name__)

# SpooledTemporaryFile 的内存阈值：小于该大小在内存中处理，超过才落盘，兼顾速度与内存
_SPOOL_MAX_MEMORY_BYTES = 2 * 1024 * 1024

# Bound remote audio downloads before forwarding them to the transcription API.
# 远程音频下载的默认大小上限，防止超大文件耗尽内存/带宽（可被配置覆盖）
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _json(data: dict[str, Any]) -> str:
    # 同步 JSON 序列化，保留非 ASCII 字符
    return json.dumps(data, ensure_ascii=False)


async def _json_dumps_result(data: dict[str, Any]) -> str:
    # 把 JSON 序列化放到线程池，避免大对象序列化阻塞事件循环
    return await run_blocking_io(json.dumps, data, ensure_ascii=False)


async def _maybe_await(value: Any) -> Any:
    # 兼容同步/异步返回值：是 awaitable 就 await，否则原样返回
    if inspect.isawaitable(value):
        return await value
    return value


def _resolve_url(url: str, runtime: ToolRuntime | None) -> str:
    # 把可能的相对路径解析为可下载的绝对 URL
    if url.startswith(("http://", "https://")):
        return url
    # 以 / 开头的相对路径需拼接运行时提供的后端 base_url
    if url.startswith("/"):
        base_url = get_base_url_from_runtime(runtime)
        if base_url:
            return f"{base_url}{url}"
    return url


def _guess_filename(url: str) -> str:
    # 从 URL 路径推断文件名，供上传给转写 API 时使用；无法推断则回退 "audio"
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1] if path else "audio"


def _known_download_size(headers: Any) -> int | None:
    # 从响应头解析 Content-Length（若有）用于提前拒绝超大文件；解析失败返回 None
    try:
        raw_size = headers.get("content-length")
    except Exception:
        return None
    if raw_size is None:
        return None
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _build_client() -> AsyncOpenAI | None:
    # 依据配置构造 OpenAI 兼容的异步客户端；未配置 API Key 时返回 None（工具将报错提示）
    api_key = getattr(settings, "AUDIO_TRANSCRIPTION_API_KEY", "") or ""
    if not api_key:
        return None

    # 可选自定义 base_url，支持指向 OpenAI 兼容的第三方转写服务
    base_url = getattr(settings, "AUDIO_TRANSCRIPTION_BASE_URL", "") or None
    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    return AsyncOpenAI(**client_kwargs)


async def _close_client(client: Any) -> None:
    # 尽力关闭客户端底层连接；无 aclose 或关闭失败均安全忽略
    close = getattr(client, "aclose", None)
    if close is None:
        return
    try:
        await _maybe_await(close())
    except Exception as exc:
        logger.debug("[audio_transcribe] failed to close OpenAI client: %s", exc)


@tool
async def audio_transcribe(
    url: Annotated[
        str, "URL of the audio file to transcribe. Supports absolute URLs and /api paths."
    ],
    model: Annotated[
        str | None,
        "Optional transcription model override, such as gpt-4o-mini-transcribe or FunAudioLLM/SenseVoiceSmall.",
    ] = None,
    language: Annotated[str | None, "Optional language hint, such as en or zh."] = None,
    prompt: Annotated[str | None, "Optional transcription prompt to improve recognition."] = None,
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """Download one audio file by URL and transcribe it into text."""

    # 解析为绝对 URL（相对路径依赖运行时的 base_url）
    resolved_url = _resolve_url(url, runtime)

    client = _build_client()
    if client is None:
        # 缺少 API Key：以 JSON 错误串返回，而非抛异常
        return await _json_dumps_result({"error": "AUDIO_TRANSCRIPTION_API_KEY is not configured"})

    # 模型优先级：调用参数 > 配置项 > 默认 gpt-4o-mini-transcribe
    resolved_model = (
        model or getattr(settings, "AUDIO_TRANSCRIPTION_MODEL", "") or "gpt-4o-mini-transcribe"
    )

    try:
        try:
            filename = _guess_filename(resolved_url)
            # 用 spooled 临时文件承接下载内容：先内存后落盘，避免大文件占满内存
            with SpooledTemporaryFile(max_size=_SPOOL_MAX_MEMORY_BYTES, mode="w+b") as file_obj:
                # 计算本次允许的最大下载字节数（配置可覆盖，至少 1）
                max_download_bytes = max(
                    int(
                        getattr(
                            settings,
                            "AUDIO_TRANSCRIPTION_MAX_DOWNLOAD_BYTES",
                            _MAX_DOWNLOAD_BYTES,
                        )
                        or 0
                    ),
                    1,
                )
                total_size = 0
                # 流式下载：跟随重定向、限时，边下边校验大小
                async with httpx.AsyncClient(follow_redirects=True, timeout=60) as http_client:
                    async with http_client.stream("GET", resolved_url) as response:
                        response.raise_for_status()
                        # 双重保护之一：如响应头已声明超限，直接拒绝，省去下载
                        known_size = _known_download_size(getattr(response, "headers", {}))
                        if known_size is not None and known_size > max_download_bytes:
                            return await _json_dumps_result(
                                {"error": f"Audio download exceeds {max_download_bytes} bytes"}
                            )
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            total_size += len(chunk)
                            # 双重保护之二：实际累计字节超限即中止（应对无 Content-Length 情况）
                            if total_size > max_download_bytes:
                                return await _json_dumps_result(
                                    {
                                        "error": (
                                            f"Audio download exceeds {max_download_bytes} bytes"
                                        )
                                    }
                                )
                            # 写文件是阻塞 I/O，放到线程池执行
                            await run_blocking_io(file_obj.write, chunk)
                # 回到文件开头，供上传读取
                await run_blocking_io(file_obj.seek, 0)

                # 组装转写请求；language/prompt 为可选增强项
                request: dict[str, Any] = {
                    "file": (filename, file_obj),
                    "model": resolved_model,
                }
                if language:
                    request["language"] = language
                if prompt:
                    request["prompt"] = prompt

                result = await client.audio.transcriptions.create(**request)
        except Exception as exc:
            # 下载或转写失败统一转成 JSON 错误串返回，不中断 Agent
            logger.warning("[audio_transcribe] transcription failed for %s: %s", resolved_url, exc)
            return await _json_dumps_result({"error": f"Audio transcription failed: {exc}"})

        # 兼容不同返回形态：对象带 .text 或直接返回字符串
        text = getattr(result, "text", None)
        if text is None and isinstance(result, str):
            text = result

        payload = {
            "success": True,
            "text": text or "",
            "url": resolved_url,
            "filename": filename,
            "model": resolved_model,
        }
        # 可选回传字段：识别语言与音频时长（部分模型才有）
        response_language = getattr(result, "language", None)
        if response_language:
            payload["language"] = response_language
        response_duration = getattr(result, "duration", None)
        if response_duration is not None:
            payload["duration"] = response_duration

        return await _json_dumps_result(payload)
    finally:
        # 无论成败都关闭客户端，避免连接泄漏
        await _close_client(client)


def get_audio_transcribe_tool() -> BaseTool:
    # 工厂函数：返回上面用 @tool 装饰生成的工具对象，供注册中心装配
    return audio_transcribe

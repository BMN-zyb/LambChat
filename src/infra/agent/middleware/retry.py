"""Retry and fallback middleware for deep agents."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import ModelRetryMiddleware
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from src.kernel.config import settings

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ExtendedModelResponse

logger = logging.getLogger(__name__)


def _is_retryable_error(exc: Exception) -> bool:
    """Check if an exception is a transient/retryable LLM error.

    Retries on: RateLimitError (429), 5xx server errors, timeouts,
    APIConnectionError (network/TLS/proxy failures), empty stream,
    and API proxy errors with custom error codes (e.g. code "1234").
    Does NOT retry on: 401/403 auth errors, 400 bad request, 404 not found.
    """
    # LangChain empty stream: LLM returned no chunks at all
    # LangChain 空流：模型一个 chunk 都没返回，视为可重试
    if isinstance(exc, ValueError) and "No generations found in stream" in str(exc):
        return True

    # httpx transient network errors (peer closed, incomplete chunked read, etc.)
    # httpx 瞬时网络错误（对端关闭、分块读取中断等）
    try:
        import httpx

        if isinstance(exc, httpx.RemoteProtocolError):
            return True
    except ImportError:
        pass

    # 动态探测 anthropic / openai 的异常类型（未安装则跳过对应分支）
    for module in ("anthropic", "openai"):
        try:
            mod = __import__(
                module,
                fromlist=[
                    "RateLimitError",
                    "APITimeoutError",
                    "APIConnectionError",
                    "APIStatusError",
                ],
            )
            # 限流(429)、超时、连接错误一律重试
            if isinstance(exc, mod.RateLimitError):
                return True
            if isinstance(exc, mod.APITimeoutError):
                return True
            if isinstance(exc, mod.APIConnectionError):
                return True
            if isinstance(exc, mod.APIStatusError):
                # Standard 5xx server errors
                # 标准 5xx 服务端错误可重试（4xx 客户端错误不重试）
                if 500 <= exc.status_code < 600:
                    return True
                # API proxy errors with custom error codes (e.g. Chinese proxies
                # returning code "1234" with "网络错误" for transient network issues)
                # 国内代理常把瞬时网络问题包成 200/自定义错误码，需按 body 内容识别
                body = getattr(exc, "body", None)
                if isinstance(body, dict):
                    error_obj = body.get("error", {})
                    if isinstance(error_obj, dict):
                        error_code = error_obj.get("code")
                        error_msg = str(error_obj.get("message", "")).lower()
                        # Known proxy error codes that indicate transient issues
                        # 已知代表瞬时问题的代理错误码
                        if error_code in ("1234",):
                            return True
                        # Network-related keywords in proxy error messages
                        # 代理错误消息里的网络类关键字
                        network_keywords = ("网络错误", "network error", "timeout", "overloaded")
                        if any(kw in error_msg for kw in network_keywords):
                            return True
        except (ImportError, AttributeError):
            continue
    return False


def _is_empty_content(aimessage: AIMessage) -> bool:
    """Check if an AIMessage has no meaningful content.

    Tool-call-only responses and responses with non-empty text are NOT empty.
    Reasoning-only responses are still empty because the user has no final answer yet.
    """
    # 带工具调用的回复不算空（模型在调工具，属正常）
    if getattr(aimessage, "tool_calls", None):
        return False

    content = getattr(aimessage, "content", None)
    # None / 空串视为空
    if content is None or content == "":
        return True
    # 纯字符串：去空白后为空则算空
    if isinstance(content, str):
        return not content.strip()
    # 块列表：没有任何非空 text 块则算空（只有 reasoning 也算空，用户还没拿到答案）
    if isinstance(content, list):
        return not any(
            block.get("type") == "text" and block.get("text", "").strip()
            for block in content
            if isinstance(block, dict)
        )
    return False


def _is_truncated_response(aimessage: AIMessage) -> bool:
    """Check if a response was truncated (incomplete) based on stop_reason or content cues.

    A response is considered truncated when:
    - stop_reason is not 'end_turn'/'tool_use'/'stop_sequence' (explicit truncation), or
    - stop_reason is absent but the text ends with an incomplete cue (colon, ellipsis)
      and there are no tool_calls (heuristic for connection-drop truncation).
    """
    # Explicit stop_reason check
    # 优先看显式的 stop_reason：非正常结束原因即判定截断
    metadata = getattr(aimessage, "response_metadata", None)
    if isinstance(metadata, dict):
        stop_reason = metadata.get("stop_reason")
        if stop_reason is not None:
            return stop_reason not in ("end_turn", "tool_use", "stop_sequence")

    # Heuristic: text ends with incomplete cue and no tool_calls
    # 无 stop_reason 时的启发式：有工具调用则不算截断
    if getattr(aimessage, "tool_calls", None):
        return False
    content = getattr(aimessage, "content", None)
    text = ""
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        # 取第一个 text 块的文本用于判断
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = (block.get("text", "") or "").strip()
                break
    if not text:
        return False
    # 文本以冒号/省略号等"未说完"信号结尾（且有一定长度）则疑似连接中断截断
    return text.endswith(("：", ":", "……", "...", "…")) and len(text) > 2


def _extract_messages(
    response: ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT] | Any,
) -> list[Any]:
    """Extract AIMessage list from various response types."""
    # 统一从多种响应类型中取出 AIMessage 列表
    if isinstance(response, AIMessage):
        return [response]
    if isinstance(response, ModelResponse):
        return response.result if response.result else []
    # ExtendedModelResponse：从其内部 model_response 取
    if hasattr(response, "model_response"):
        return response.model_response.result if response.model_response.result else []
    return []


def _response_is_invalid(response: Any) -> bool:
    """Check whether a model response should be treated as failed."""
    # 首条消息为空内容或被截断，即视为无效响应（触发重试/fallback）
    messages = _extract_messages(response)
    if not messages or not isinstance(messages[0], AIMessage):
        return False
    return _is_empty_content(messages[0]) or _is_truncated_response(messages[0])


class ModelFallbackMiddleware(AgentMiddleware):
    """Middleware that falls back to an alternate model when the primary model fails.

    Wraps the inner retry stack. When all retries on the primary model are exhausted
    (ModelRetryMiddleware gives up via ``on_failure="error"``) and the inner
    handler raises an error, this middleware creates a fallback LLM and
    replays the request once.
    """

    def __init__(self, *, fallback_model: str, thinking: dict | None = None) -> None:
        super().__init__()
        # 备用模型名与 thinking 配置；备用 LLM 惰性创建后缓存
        self._fallback_model = fallback_model
        self._thinking = thinking
        self._fallback_llm: BaseChatModel | None = None

    async def _get_fallback_llm(self) -> BaseChatModel:
        """Lazily create the fallback LLM instance."""
        # 首次需要 fallback 时才创建备用 LLM，避免无谓初始化
        if self._fallback_llm is None:
            from src.infra.llm.client import LLMClient

            self._fallback_llm = await LLMClient.get_model(
                model=self._fallback_model,
                thinking=self._thinking,
            )
            logger.info("[ModelFallback] Created fallback LLM: %s", self._fallback_model)
        return self._fallback_llm

    async def _invoke_fallback(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
        reason: str,
    ) -> ModelResponse[ResponseT]:
        # 用备用模型重放一次请求；备用也失败则记录并抛出
        logger.warning(
            "[ModelFallback] Primary model failed: %s — falling back to %s",
            reason,
            self._fallback_model,
        )

        fallback_llm = await self._get_fallback_llm()
        # override 只替换模型，其余请求参数保持不变
        new_request = request.override(model=fallback_llm)
        try:
            return await handler(new_request)
        except Exception as fallback_exc:
            logger.error(
                "[ModelFallback] Fallback model %s also failed: %s",
                self._fallback_model,
                fallback_exc,
            )
            raise

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        # 位于重试栈最外层：先正常调用（内部含重试），异常时触发 fallback
        try:
            response = await handler(request)
        except Exception as exc:
            return await self._invoke_fallback(request, handler, str(exc))

        # 即使未抛异常，若响应为空/被截断也切换到备用模型
        if _response_is_invalid(response):
            messages = _extract_messages(response)
            ai_message = messages[0]
            reason = "truncated content" if _is_truncated_response(ai_message) else "empty content"
            return await self._invoke_fallback(request, handler, reason)

        return response


class EmptyContentRetryMiddleware(AgentMiddleware):
    """Middleware that retries model calls returning empty content."""

    def __init__(self, *, max_retries: int = 1, retry_delay: float = 1.0) -> None:
        super().__init__()
        # 最大重试次数与每次重试间隔
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | AIMessage | ExtendedModelResponse[ResponseT]:
        # 位于重试栈最内层：仅针对"空/截断内容"重试（网络错误由外层 ModelRetry 处理）
        last_response = None
        for attempt in range(self.max_retries + 1):
            response = await handler(request)
            last_response = response

            # 拿不到 AIMessage 就没法判断，直接结束
            messages = _extract_messages(response)
            if not messages or not isinstance(messages[0], AIMessage):
                break

            # 内容有效则立即返回
            if not _is_empty_content(messages[0]) and not _is_truncated_response(messages[0]):
                return response

            reason = "truncated" if _is_truncated_response(messages[0]) else "empty"
            logger.warning(
                "%s content in model response (attempt %d/%d)",
                reason.capitalize(),
                attempt + 1,
                self.max_retries + 1,
            )
            # 还有重试机会则等待固定间隔后再试
            if attempt < self.max_retries:
                await asyncio.sleep(self.retry_delay)

        # 重试用尽仍无效，返回最后一次响应（交由上层 fallback 处理）
        return last_response  # type: ignore[return-value]


def create_retry_middleware(
    fallback_model: str | None = None,
    thinking: dict | None = None,
) -> list[AgentMiddleware]:
    """Create the retry middleware stack for deep agents.

    Returns [ModelFallbackMiddleware?, ModelRetryMiddleware, EmptyContentRetryMiddleware]:
    - Outer layer (optional): falls back to an alternate model when primary fails
    - Middle layer: retries on 429/5xx/timeout with exponential backoff
    - Inner layer: retries on empty content responses
    """
    # 中间件按列表顺序由外到内包裹，故顺序即分层：
    # 外层 fallback -> 中层网络重试 -> 内层空内容重试
    stack: list[AgentMiddleware] = []

    # 仅在配置了备用模型时才加最外层 fallback
    if fallback_model:
        stack.append(ModelFallbackMiddleware(fallback_model=fallback_model, thinking=thinking))

    stack.extend(
        [
            # 中层：对 429/5xx/超时等做指数退避重试（带抖动），失败后向外层抛出
            ModelRetryMiddleware(
                max_retries=settings.LLM_MAX_RETRIES,
                retry_on=_is_retryable_error,
                on_failure="error",
                backoff_factor=2.0,
                initial_delay=settings.LLM_RETRY_DELAY,
                max_delay=60.0,
                jitter=True,
            ),
            # 内层：对空/截断内容做固定间隔重试
            EmptyContentRetryMiddleware(
                max_retries=settings.LLM_MAX_RETRIES, retry_delay=settings.LLM_RETRY_DELAY
            ),
        ]
    )
    return stack

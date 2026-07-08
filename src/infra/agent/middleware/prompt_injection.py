"""System prompt injection middleware — memory, sandbox MCP, env vars, sections."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import SystemMessage

from src.infra.agent.middleware._helpers import (
    _append_system_text_block,
    _append_system_text_blocks,
    _normalize_prompt_text,
    _system_message_to_blocks,
)

logger = logging.getLogger(__name__)


class SectionPromptMiddleware(AgentMiddleware):
    """Append one or more deterministic prompt sections as separate system blocks.

    Each section becomes its own content block in the system message, enabling
    fine-grained KV cache breakpoints.  Sections are normalized (trailing
    whitespace stripped) at construction time and batch-appended in a single
    pass to avoid O(n²) block-list rebuilding.
    """

    def __init__(self, *, sections: list[str] | tuple[str, ...]) -> None:
        super().__init__()
        # 构造期就归一化并过滤空段：每段将作为独立系统块，利于精细的缓存断点
        self._sections = tuple(
            _normalize_prompt_text(section) for section in sections if section.strip()
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        # 无注入段则直接放行
        if not self._sections:
            return await handler(request)

        # Batch-append all sections in one pass (avoids repeated _system_message_to_blocks)
        # 一次性批量追加所有段，避免逐段重建块列表带来的 O(n²) 开销
        blocks = _system_message_to_blocks(request.system_message)
        blocks.extend({"type": "text", "text": section} for section in self._sections)
        request = request.override(system_message=SystemMessage(content=blocks))
        return await handler(request)


class MemoryIndexMiddleware(AgentMiddleware):
    """Injects the native memory index into the system prompt at request time.

    Uses ``NativeMemoryBackend.build_memory_index(user_id)`` which has its own
    5-minute per-user cache, so repeated calls are essentially free after the first.
    Only active when the native backend is selected and the index feature is enabled.
    """

    def __init__(self, *, user_id: str | None) -> None:
        super().__init__()
        self._user_id = user_id

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        # 无用户 id 无法构建记忆索引，直接放行
        if not self._user_id:
            return await handler(request)

        # 构建记忆索引（后端自带 5 分钟缓存），为空则不注入
        index_str = await _build_memory_index_for_user(self._user_id)
        if not index_str:
            return await handler(request)

        # 追加到系统消息尾部
        new_system_message = _append_system_text_block(request.system_message, index_str)
        request = request.override(system_message=new_system_message)
        return await handler(request)


async def _build_memory_index_for_user(user_id: str) -> str:
    """Build memory index string for a user. Returns empty string on any failure."""
    try:
        # 仅在记忆后端为 native 时才构建索引
        from src.infra.memory.tools import _get_backend

        backend = await _get_backend()
        if backend is None or backend.name != "native":
            return ""

        # 类型确认后调用 build_memory_index（其内部按用户缓存）
        from src.infra.memory.client.native import NativeMemoryBackend

        if not isinstance(backend, NativeMemoryBackend):
            return ""
        index = await backend.build_memory_index(user_id)
        return index if index else ""
    except Exception:
        # 任何失败都降级为空串，绝不阻断模型调用
        logger.warning("[Memory] Failed to build memory index for user %s", user_id, exc_info=True)
        return ""


class SandboxMCPMiddleware(AgentMiddleware):
    """Injects sandbox tool descriptions into the system prompt at request time.

    By injecting via middleware (instead of baking into the base system prompt string),
    the sandbox tools end up at the TAIL of the final system message — after
    deepagent's BASE_AGENT_PROMPT and all other middleware injections (memory, subagent,
    summarization, etc.).  This maximizes KV cache hit rates because changes to sandbox tools
    only invalidate the tail of the cache, not the stable prefix.

    ``build_sandbox_mcp_prompt`` has its own per-user 30-minute cache, so repeated
    ``awrap_model_call`` invocations within a session are essentially free.
    """

    def __init__(self, *, backend: Any, user_id: str) -> None:
        super().__init__()
        self._backend = backend
        self._user_id = user_id

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        # 构建沙箱 MCP 工具描述段（后端自带 30 分钟缓存）
        from src.infra.tool.sandbox_mcp_prompt import build_sandbox_mcp_prompt_sections

        prompt_sections = await build_sandbox_mcp_prompt_sections(self._backend, self._user_id)
        # 有内容则追加到系统消息尾部（放尾部以最大化缓存命中）
        if prompt_sections:
            new_system_message = _append_system_text_blocks(request.system_message, prompt_sections)
            request = request.override(system_message=new_system_message)
        return await handler(request)


class EnvVarPromptMiddleware(AgentMiddleware):
    """Inject configured environment variable keys into the system prompt.

    Only key names are included. Values are never read as plaintext here.
    """

    def __init__(self, *, user_id: str) -> None:
        super().__init__()
        self._user_id = user_id

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        # 仅注入环境变量的"键名"，绝不读取明文值（安全考量）
        from src.infra.tool.env_var_prompt import build_env_var_prompt_sections

        prompt_sections = await build_env_var_prompt_sections(self._user_id)
        if prompt_sections:
            new_system_message = _append_system_text_blocks(request.system_message, prompt_sections)
            request = request.override(system_message=new_system_message)
        return await handler(request)

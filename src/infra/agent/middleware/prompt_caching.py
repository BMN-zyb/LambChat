"""Prompt caching middleware — KV cache optimization for Anthropic models."""

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
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool

from src.infra.agent.middleware._helpers import _system_message_to_blocks
from src.kernel.config import settings

# Anthropic 最多支持 4 个缓存断点（cache_control 标记）
_MAX_ANTHROPIC_CACHE_BREAKPOINTS = 4
# 工具 extras 上的标记键：标记为"易变工具"的不参与提示缓存
_PROMPT_CACHE_VOLATILE_TOOL_EXTRA = "_lambchat_prompt_cache_volatile"


class PromptCachingMiddleware(AgentMiddleware):
    """Re-tags cache breakpoints AFTER all user middleware has injected dynamic content.

    Problem
    -------
    deepagents' built-in ``AnthropicPromptCachingMiddleware`` runs **before** user
    middleware (AppPrompt, MemoryIndex, SandboxMCP, ToolSearch).  It tags the *then*
    last system-message content block with ``cache_control``, but user middleware
    subsequently appends more blocks (skills, memory, MCP tools, deferred stubs).
    The original cache breakpoint ends up in the middle of the final system message,
    so all dynamic content is re-processed every turn.

    Solution
    --------
    This middleware runs **last** in the user middleware chain (innermost layer).
    It walks the final system message and tools, then:

    1. Removes stale ``cache_control`` tags left by earlier middleware.
    2. Allocates at most Anthropic's four cache breakpoints across tools and
       system blocks, reserving one for tools when possible and using the rest
       for the system-message tail.

    Result: cache tags stay valid while covering the stable prompt prefix
    (base prompt + workflow + persona + skills + memory guide) before volatile
    blocks such as memory indexes or deferred tool lists.
    """

    # cache_control 的取值：ephemeral（临时缓存），Anthropic 提示缓存标准写法
    _CACHE_CONTROL = {"type": "ephemeral"}

    def __init__(self) -> None:
        super().__init__()
        # 从配置读取可缓存的系统块/工具数量上限（至少为 1）
        self._max_cached_system_blocks = max(
            int(getattr(settings, "PROMPT_CACHE_MAX_SYSTEM_BLOCKS", 8) or 0), 1
        )
        self._max_cached_tools = max(int(getattr(settings, "PROMPT_CACHE_MAX_TOOLS", 8) or 0), 1)

    @staticmethod
    def _is_anthropic_model(model: Any) -> bool:
        """Return True when request.model is backed by langchain-anthropic."""
        # 沿包装链逐层解包，判断底层是否为 langchain_anthropic 模型
        # seen 记录已访问对象 id，防止环形引用导致死循环
        seen: set[int] = set()
        current = model
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            cls = type(current)
            if cls.__module__.startswith("langchain_anthropic"):
                return True

            # RunnableBinding and similar wrappers keep the underlying model on
            # ``bound``.  Some adapters use ``model`` for the wrapped runnable.
            # 包装器把真实模型放在 bound/_bound/model 上，依次解包
            next_model = getattr(current, "bound", None)
            if next_model is None:
                next_model = getattr(current, "_bound", None)
            if next_model is None:
                candidate = getattr(current, "model", None)
                # model 若是字符串（模型名）则不是可继续解包的对象
                next_model = candidate if not isinstance(candidate, str) else None
            current = next_model
        return False

    @staticmethod
    def _is_minimax_passive_cache_model(model: Any) -> bool:
        """Return True for MiniMax Anthropic-compatible models.

        MiniMax Prompt Cache is passive by default on its Anthropic-compatible
        endpoint. Avoid adding Anthropic active ``cache_control`` tags here so
        requests keep the documented passive-cache semantics.
        """
        # MiniMax 的 Anthropic 兼容端点默认被动缓存，主动打 cache_control 会破坏其语义
        # 因此需识别出 MiniMax 并跳过打标（见 awrap_model_call）
        seen: set[int] = set()
        current = model
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            # 把模型名/base_url 等字段拼成一个串做关键字匹配
            haystack = " ".join(
                str(getattr(current, attr, "") or "")
                for attr in (
                    "model",
                    "model_name",
                    "anthropic_api_url",
                    "base_url",
                    "_base_url",
                )
            ).lower()
            if "minimax" in haystack or "minimaxi" in haystack:
                return True

            # 同样逐层解包包装器
            next_model = getattr(current, "bound", None)
            if next_model is None:
                next_model = getattr(current, "_bound", None)
            if next_model is None:
                candidate = getattr(current, "model", None)
                next_model = candidate if not isinstance(candidate, str) else None
            current = next_model
        return False

    # ---- system message ---------------------------------------------------

    @staticmethod
    def _block_text(block: Any) -> str:
        # 提取块的文本内容（dict 取 text 字段，否则整体字符串化）
        if isinstance(block, dict):
            return str(block.get("text", ""))
        return str(block)

    @classmethod
    def _is_volatile_system_block(cls, block: Any) -> bool:
        """Return True for system blocks that are expected to change often."""
        # 通过前缀识别"易变"系统块（记忆索引、延迟 MCP 工具、运行时上下文）
        # 这些块每轮都可能变，不应纳入缓存前缀，否则会让缓存频繁失效
        text = cls._block_text(block).strip().lower()
        volatile_prefixes = (
            "<memory_index>",
            "## mcp tools (deferred)",
            "## user runtime context",
        )
        return any(text.startswith(prefix) for prefix in volatile_prefixes)

    @classmethod
    def _cacheable_system_block_count(cls, system_message: Any) -> int:
        """Count the stable prefix before the first volatile system block."""
        # 统计"第一个易变块之前"的稳定前缀长度：只有稳定前缀值得缓存
        blocks = _system_message_to_blocks(system_message)
        for i, block in enumerate(blocks):
            if cls._is_volatile_system_block(block):
                return i
        return len(blocks)

    @staticmethod
    def _cache_indices_for_stable_prefix(cacheable_count: int, max_cached_blocks: int) -> list[int]:
        """Pick cache breakpoints for stable blocks.

        Always include block 0 when possible so the global base prompt can be
        reused across different personas, skills, sessions, and runtime sections.
        Remaining breakpoints go to the tail of the stable prefix.
        """
        # 断点分配策略：块 0（全局基础 prompt）尽量固定命中，
        # 让不同 persona/技能/会话都能复用；剩余断点留给稳定前缀的尾部
        if cacheable_count <= 0 or max_cached_blocks <= 0:
            return []

        # 只剩 1 个断点或只有 1 个可缓存块时，只标块 0
        if max_cached_blocks == 1 or cacheable_count == 1:
            return [0]

        # 其余断点分配到稳定前缀末尾（tail_budget 个）
        tail_budget = min(max_cached_blocks - 1, cacheable_count - 1)
        tail_start = cacheable_count - tail_budget
        return [0, *range(tail_start, cacheable_count)]

    @staticmethod
    def _retag_system_message(
        system_message: Any, cache_control: dict, *, max_cached_blocks: int = 4
    ) -> Any:
        """Strip stale cache_control and tag the stable prefix before volatile blocks."""
        if system_message is None:
            return system_message

        blocks = _system_message_to_blocks(system_message)
        if not blocks:
            return system_message

        # Remove cache_control from every block
        # 先清掉所有块上早期中间件遗留的旧 cache_control 标记
        for i, block in enumerate(blocks):
            if isinstance(block, dict) and "cache_control" in block:
                blocks[i] = {k: v for k, v in block.items() if k != "cache_control"}

        if max_cached_blocks <= 0:
            return SystemMessage(content=blocks)

        # 计算稳定前缀长度
        cacheable_count = PromptCachingMiddleware._cacheable_system_block_count(
            SystemMessage(content=blocks)
        )
        if cacheable_count <= 0:
            return SystemMessage(content=blocks)

        # Tag global base plus the tail of the stable prefix. Later volatile
        # blocks still get sent, but they do not consume cache breakpoints.
        # 按分配策略给选中的块打上 cache_control；易变块仍会发送但不占断点
        for i in PromptCachingMiddleware._cache_indices_for_stable_prefix(
            cacheable_count, max_cached_blocks
        ):
            block = blocks[i]
            base = block if isinstance(block, dict) else {"type": "text", "text": str(block)}
            blocks[i] = {**base, "cache_control": cache_control}

        return SystemMessage(content=blocks)

    # ---- tools ------------------------------------------------------------

    @staticmethod
    def _is_cacheable_tool(tool: Any) -> bool:
        # 非 BaseTool 或被标记为易变的工具不可缓存
        if not isinstance(tool, BaseTool):
            return False
        extras = tool.extras or {}
        return not bool(extras.get(_PROMPT_CACHE_VOLATILE_TOOL_EXTRA))

    @classmethod
    def _cacheable_tool_count(cls, tools: list[Any] | None) -> int:
        # 统计可缓存工具数量
        return sum(1 for tool in tools or [] if cls._is_cacheable_tool(tool))

    @staticmethod
    def _retag_tools(
        tools: list[Any] | None, cache_control: dict, *, max_cached_tools: int = 4
    ) -> list[Any] | None:
        """Strip stale cache_control from tools and tag the final stable N tools."""
        if not tools:
            return tools

        # Find and remove existing cache_control from tools
        # 先清掉工具 extras 上遗留的旧 cache_control，同时记录可缓存工具下标
        cleaned = []
        tool_indices: list[int] = []
        for i, tool in enumerate(tools):
            if isinstance(tool, BaseTool):
                extras = tool.extras or {}
                if PromptCachingMiddleware._is_cacheable_tool(tool):
                    tool_indices.append(i)
                if "cache_control" in extras:
                    new_extras = {k: v for k, v in extras.items() if k != "cache_control"}
                    cleaned.append(tool.model_copy(update={"extras": new_extras}))
                    continue
            cleaned.append(tool)

        if max_cached_tools <= 0:
            return cleaned

        # Tag the last N tools
        # 只给靠后的 N 个可缓存工具打标（工具列表尾部更稳定，利于缓存命中）
        for idx in tool_indices[-max_cached_tools:]:
            tool = cleaned[idx]
            if isinstance(tool, BaseTool):
                new_extras = {**(tool.extras or {}), "cache_control": cache_control}
                cleaned[idx] = tool.model_copy(update={"extras": new_extras})

        return cleaned

    # ---- main entry -------------------------------------------------------

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        # 仅对 Anthropic 模型生效；非 Anthropic 直接放行
        if not self._is_anthropic_model(getattr(request, "model", None)):
            return await handler(request)
        # MiniMax 走被动缓存，跳过主动打标
        if self._is_minimax_passive_cache_model(getattr(request, "model", None)):
            return await handler(request)

        # 统计可缓存的系统块数与工具数
        overrides: dict[str, Any] = {}
        system_block_count = self._cacheable_system_block_count(request.system_message)
        tool_count = self._cacheable_tool_count(request.tools)

        # 断点预算分配：总共最多 4 个断点，尽量为工具保留 1 个
        reserved_tool_breakpoints = 1 if tool_count > 0 and self._max_cached_tools > 0 else 0
        # 系统块预算 = min(配置上限, 实际可缓存数, 4 - 预留给工具的断点)
        system_budget = min(
            self._max_cached_system_blocks,
            system_block_count,
            _MAX_ANTHROPIC_CACHE_BREAKPOINTS - reserved_tool_breakpoints,
        )
        # 工具预算 = min(配置上限, 实际可缓存数, 4 - 已用于系统块的断点)
        tool_budget = min(
            self._max_cached_tools,
            tool_count,
            _MAX_ANTHROPIC_CACHE_BREAKPOINTS - system_budget,
        )

        # 按预算重新给系统消息打缓存标记
        new_system = self._retag_system_message(
            request.system_message,
            self._CACHE_CONTROL,
            max_cached_blocks=system_budget,
        )
        if new_system is not request.system_message:
            overrides["system_message"] = new_system

        # 按预算重新给工具打缓存标记
        new_tools = self._retag_tools(
            request.tools,
            self._CACHE_CONTROL,
            max_cached_tools=tool_budget,
        )
        if new_tools is not request.tools:
            overrides["tools"] = new_tools

        # 有改动才 override 请求
        if overrides:
            request = request.override(**overrides)

        return await handler(request)

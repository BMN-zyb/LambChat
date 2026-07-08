"""Main-agent context handoff middleware for subagents."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from src.infra.async_utils import run_blocking_io

logger = logging.getLogger(__name__)

# token 估算：约 4 字符 ≈ 1 token；上下文默认 token 上限与保留最近条数
_CHARS_PER_TOKEN = 4
_DEFAULT_CONTEXT_TOKEN_LIMIT = 20000
_DEFAULT_KEEP_RECENT = 8
# 内存中日志的最大字符数（用于硬性截断，防止无限增长）
_DEFAULT_MAX_LOG_CHARS = _DEFAULT_CONTEXT_TOKEN_LIMIT * _CHARS_PER_TOKEN
_CONTEXT_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S %z"
# 脱敏占位符及匹配敏感信息的正则（键值赋值、JSON 字段、Authorization Bearer）
_REDACTED = "[REDACTED]"
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_SENSITIVE_JSON_RE = re.compile(
    r'(?i)("(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)"\s*:\s*")([^"]+)(")'
)
_AUTHORIZATION_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s,;]+)")


def subagent_handoff_dir_for_backend(backend: Any, dirname: str) -> str:
    """Resolve a subagent handoff directory using the same backend workspace rules."""
    # 依据后端（或其 default）的 work_dir 拼出交接目录；都取不到则退回根路径
    for candidate in (backend, getattr(backend, "default", None)):
        work_dir = getattr(candidate, "work_dir", None)
        if isinstance(work_dir, str) and work_dir.strip():
            return f"{work_dir.rstrip('/')}/{dirname.strip('/')}"
    return f"/{dirname.strip('/')}"


async def write_subagent_handoff_file(
    backend: Any,
    *,
    dirname: str,
    filename: str,
    content: str,
    log_context: str,
) -> str | None:
    """Write a subagent handoff file and return the backend-visible path."""
    # 通过后端写入交接文件，返回后端可见路径；写失败返回 None（不阻断主流程）
    path = f"{subagent_handoff_dir_for_backend(backend, dirname)}/{filename.strip('/')}"
    try:
        write_result = await backend.awrite(path, content)
        if getattr(write_result, "error", None):
            logger.warning("[%s] Write failed: %s", log_context, write_result.error)
            return None
        # 优先返回后端回报的真实路径
        return str(getattr(write_result, "path", None) or path)
    except Exception:
        logger.warning("[%s] Backend write failed", log_context, exc_info=True)
        return None


class CompressibleMarkdownLog:
    """Small reusable markdown log with one-shot older-entry compression."""

    def __init__(
        self,
        *,
        token_limit: int,
        keep_recent: int,
        max_log_chars: int,
        compressed_heading: str,
        truncated_label: str = "entries",
    ) -> None:
        # token_limit：超过则触发 LLM 压缩；keep_recent：压缩时保留的最近条数
        self._token_limit = token_limit
        self._keep_recent = keep_recent
        # max_log_chars：内存硬上限（至少 1），超过用截断标记裁掉最旧条目
        self._max_log_chars = max(int(max_log_chars), 1)
        self._compressed_heading = compressed_heading
        self._truncated_label = truncated_label
        self._entries: list[str] = []
        self._total_chars = 0
        # 压缩一次性生效标志，避免重复压缩
        self._compressed = False

    @property
    def entries(self) -> list[str]:
        return self._entries

    @property
    def total_chars(self) -> int:
        return self._total_chars

    @property
    def compressed(self) -> bool:
        return self._compressed

    def append(self, entry: str) -> None:
        # 追加一条并更新字符计数，随后按内存上限裁剪
        if not entry:
            return
        self._entries.append(entry)
        self._total_chars += len(entry)
        self._trim_to_memory_cap()

    def _trim_to_memory_cap(self) -> None:
        # 未超上限无需裁剪
        if self._total_chars <= self._max_log_chars:
            return

        omitted_count = 0
        marker = f"\n## [TRUNCATED] Earlier {self._truncated_label} omitted to cap memory."

        # 从最旧条目开始弹出，直到能容纳截断标记
        while self._entries and self._total_chars + len(marker) > self._max_log_chars:
            removed = self._entries.pop(0)
            self._total_chars -= len(removed)
            omitted_count += 1

        if omitted_count <= 0:
            return

        # 生成带被裁条数的截断标记
        marker = (
            f"\n## [TRUNCATED] {omitted_count} older {self._truncated_label} omitted to cap memory."
        )
        # 若首条已是截断标记则替换，否则插入到最前面
        if self._entries and self._entries[0].startswith("\n## [TRUNCATED]"):
            self._total_chars -= len(self._entries[0])
            self._entries[0] = marker
            self._total_chars += len(marker)
        else:
            self._entries.insert(0, marker)
            self._total_chars += len(marker)

        # 插入标记后若仍超限，继续弹出旧条目
        while self._entries and self._total_chars > self._max_log_chars:
            removed = self._entries.pop(0)
            self._total_chars -= len(removed)

    async def check_and_compress(
        self,
        compressor: Callable[[str], Awaitable[str]],
    ) -> None:
        # 只压缩一次
        if self._compressed:
            return

        # 未超 token 上限、或条目太少（不足以保留 keep_recent 后再压）则不压缩
        estimated_tokens = self._total_chars // _CHARS_PER_TOKEN
        if estimated_tokens <= self._token_limit:
            return
        if len(self._entries) <= self._keep_recent:
            return

        # 保留最近 keep_recent 条，其余旧条目交给 compressor 压缩为摘要
        split_idx = len(self._entries) - self._keep_recent
        old_entries = self._entries[:split_idx]
        recent_entries = self._entries[split_idx:]
        old_text = "\n".join(old_entries)

        summary = await compressor(old_text)
        # 压缩摘要 + 最近条目重组为新日志
        compressed_summary = f"\n## [COMPRESSED] {self._compressed_heading}\n{summary.strip()}"
        self._entries = [compressed_summary, *recent_entries]
        self._total_chars = sum(len(entry) for entry in self._entries)
        self._compressed = True

    def render(self, header: str) -> str:
        # 渲染为完整文本：头部 + 各条目
        return header + "\n".join(self._entries) + "\n"


def redact_sensitive_text(text: str) -> str:
    """Best-effort redaction for common inline secret formats."""
    # 尽力脱敏三类常见密钥格式：Bearer 头、JSON 字段、key=value 赋值
    text = _AUTHORIZATION_BEARER_RE.sub(rf"\1{_REDACTED}", text)
    text = _SENSITIVE_JSON_RE.sub(rf"\1{_REDACTED}\3", text)
    return _SENSITIVE_ASSIGNMENT_RE.sub(rf"\1\2{_REDACTED}", text)


def _message_role(message: Any) -> str:
    # 推断消息角色：优先用 type 字段，否则由类名去掉 Message 后缀
    explicit_type = getattr(message, "type", None)
    if isinstance(explicit_type, str) and explicit_type:
        return explicit_type
    name = type(message).__name__.removesuffix("Message")
    return name.lower() or "message"


async def _json_dumps_for_context(value: Any) -> str:
    import json

    # JSON 序列化放到线程池（可能较大），保留中文与缩进
    return await run_blocking_io(json.dumps, value, ensure_ascii=False, indent=2)


async def _message_content_to_text(content: Any) -> str:
    # 把各种形态的消息内容转为纯文本并脱敏
    if isinstance(content, str):
        return redact_sensitive_text(content)
    if isinstance(content, list):
        # 块列表：text 块取文本，其它已知类型给出占位，未知块 JSON 序列化
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type")
                if block_type == "text":
                    parts.append(str(block.get("text", "")))
                elif block_type:
                    parts.append(f"[{block_type} block]")
                else:
                    parts.append(await _json_dumps_for_context(block))
            else:
                parts.append(str(block))
        return redact_sensitive_text("\n".join(part for part in parts if part))
    if isinstance(content, (dict, tuple)):
        return redact_sensitive_text(await _json_dumps_for_context(content))
    if content is None:
        return ""
    return redact_sensitive_text(str(content))


async def format_messages_as_markdown(messages: list[Any]) -> str:
    # 把消息列表渲染为编号的 markdown 段落（角色 + 内容 + 工具调用）
    entries: list[str] = []
    for index, message in enumerate(messages, start=1):
        role = _message_role(message)
        content = await _message_content_to_text(getattr(message, "content", ""))
        # content 为空时尝试从 text 属性/方法兜底取文本
        if not content and hasattr(message, "text"):
            text_attr = getattr(message, "text")
            content = text_attr() if callable(text_attr) else str(text_attr or "")

        entry_parts = [f"\n## {index}. {role}"]
        if content:
            entry_parts.append(content.strip())

        # 附带工具调用名，便于子 agent 了解主 agent 调过哪些工具
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            names = [
                call.get("name", "?") if isinstance(call, dict) else str(call)
                for call in tool_calls
            ]
            entry_parts.append(f"Tool calls: {', '.join(names)}")

        entries.append("\n".join(entry_parts))

    return "\n".join(entries)


class MainAgentContextMiddleware(AgentMiddleware):
    """Writes parent message context before launching a subagent task."""

    def __init__(
        self,
        *,
        backend: Any,
        token_limit: int = _DEFAULT_CONTEXT_TOKEN_LIMIT,
        keep_recent: int = _DEFAULT_KEEP_RECENT,
        max_log_chars: int = _DEFAULT_MAX_LOG_CHARS,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._token_limit = token_limit
        self._keep_recent = keep_recent
        self._max_log_chars = max_log_chars
        # run_id 工厂：默认取随机 uuid 前 8 位，用于命名快照文件（可注入以便测试）
        self._run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex[:8])
        # 按 (runtime/messages) 缓存已写好的快照路径，同一批消息不重复写盘
        self._snapshot_cache: dict[tuple[Any, ...], str] = {}

    def _get_backend(self, runtime: Any) -> Any:
        # backend 可以是实例或"按 runtime 解析"的工厂函数
        if callable(self._backend):
            return self._backend(runtime)
        return self._backend

    @staticmethod
    def _cache_key(request: Any, messages: list[Any]) -> tuple[Any, ...]:
        # 快照缓存键：结合 runtime/messages 身份与消息 id 序列，消息不变则命中缓存
        runtime = getattr(request, "runtime", None)
        message_ids = tuple(getattr(message, "id", None) or id(message) for message in messages)
        return (id(runtime), id(messages), len(messages), message_ids)

    @staticmethod
    def _messages_from_request(request: Any) -> list[Any]:
        # 从 request.state 或 runtime.state 里取消息列表（两处兼容）
        state = getattr(request, "state", None)
        if isinstance(state, dict) and isinstance(state.get("messages"), list):
            return state["messages"]

        runtime = getattr(request, "runtime", None)
        runtime_state = getattr(runtime, "state", None)
        if isinstance(runtime_state, dict) and isinstance(runtime_state.get("messages"), list):
            return runtime_state["messages"]
        return []

    async def _compress_with_llm(self, text: str) -> str:
        # 用低温度 LLM 把旧上下文压成要点摘要（保留请求/决策/约束/路径/结果等）
        from langchain_core.messages import HumanMessage

        from src.infra.llm.client import LLMClient

        llm = await LLMClient.get_model(temperature=0.3)
        prompt = (
            "Compress the following main-agent conversation context for a subagent.\n"
            "Keep: user requests, main-agent decisions, constraints, file paths, tool outcomes, "
            "open questions, and the latest plan.\n"
            "Drop: duplicate wording, incidental chatter, and verbose reasoning.\n"
            "Format as concise markdown bullets.\n\n"
            f"{text}"
        )
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        return response.content if isinstance(response.content, str) else str(response.content)

    async def _write_context_file(self, request: Any) -> str | None:
        # 生成并写入主 agent 上下文快照文件，返回其路径
        messages = self._messages_from_request(request)
        if not messages:
            return None

        # 命中缓存则直接复用已写路径
        cache_key = self._cache_key(request, messages)
        cached = self._snapshot_cache.get(cache_key)
        if cached:
            return cached

        # 用可压缩日志承接消息，超限时压缩旧条目
        log = CompressibleMarkdownLog(
            token_limit=self._token_limit,
            keep_recent=self._keep_recent,
            max_log_chars=self._max_log_chars,
            compressed_heading="Earlier Main-Agent Context",
        )
        # 把渲染后的 markdown 按 "## " 分段逐条追加（保持每条为独立条目）
        rendered_messages = await format_messages_as_markdown(messages)
        for entry in rendered_messages.split("\n## "):
            if not entry.strip():
                continue
            log.append(entry if entry.startswith("\n## ") else "\n## " + entry)
        try:
            await log.check_and_compress(self._compress_with_llm)
        except Exception:
            # 压缩失败则退回裁剪后的原始上下文，不阻断
            logger.warning("[MainAgentContext] Compression failed, keeping trimmed raw context")

        # 拼接带快照 id 与时间戳的头部，写入交接目录
        run_id = self._run_id_factory()
        backend = self._get_backend(getattr(request, "runtime", None))
        header = (
            f"# Main Agent Conversation Context (snapshot: {run_id})\n"
            f"Captured at: {time.strftime(_CONTEXT_TIMESTAMP_FORMAT)}\n\n"
        )
        content = log.render(header)

        context_path = await write_subagent_handoff_file(
            backend,
            dirname="subagent_context",
            filename=f"main_agent_messages_{run_id}.md",
            content=content,
            log_context="MainAgentContext",
        )
        if not context_path:
            return None
        # 缓存路径供后续同批消息复用
        self._snapshot_cache[cache_key] = context_path
        return context_path

    @staticmethod
    def _description_with_context(description: str, context_path: str) -> str:
        # 在子 agent 任务描述末尾追加上下文快照说明（幂等：已含路径则不重复追加）
        if context_path in description:
            return description
        return (
            f"{description.rstrip()}\n\n"
            "## Main-Agent Context Snapshot\n"
            f"Read it when the assignment depends on prior user/main-agent context: {context_path}\n"
            "Treat this file as context only; the explicit task above remains your objective."
        )

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        # 只拦截 task 工具调用（即派发子 agent）；其它工具原样放行
        tool_call = getattr(request, "tool_call", {}) or {}
        if tool_call.get("name") != "task":
            return await handler(request)

        # 无有效 description 则不处理
        args = dict(tool_call.get("args") or {})
        description = args.get("description")
        if not isinstance(description, str) or not description.strip():
            return await handler(request)

        # 写上下文快照，失败则不改描述直接放行
        context_path = await self._write_context_file(request)
        if not context_path:
            return await handler(request)

        # 把快照路径注入到子 agent 的任务描述中
        args["description"] = self._description_with_context(description, context_path)
        updated_tool_call = {**tool_call, "args": args}
        # 优先用 override 生成新请求；否则就地赋值
        if hasattr(request, "override"):
            request = request.override(tool_call=updated_tool_call)
        else:
            request.tool_call = updated_tool_call

        return await handler(request)

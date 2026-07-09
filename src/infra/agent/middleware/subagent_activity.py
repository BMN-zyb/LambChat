"""Subagent activity logging middleware."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import AIMessage, ToolMessage

from src.infra.agent.middleware.main_agent_context import (
    CompressibleMarkdownLog,
    format_messages_as_markdown,
    write_subagent_handoff_file,
)
from src.infra.async_utils import run_blocking_io

logger = logging.getLogger(__name__)

# token 估算比例、活动日志的 token 上限与保留最近条数
_CHARS_PER_TOKEN = 4
_DEFAULT_ACTIVITY_TOKEN_LIMIT = 50000
_DEFAULT_KEEP_RECENT = 6
_DEFAULT_MAX_LOG_CHARS = _DEFAULT_ACTIVITY_TOKEN_LIMIT * _CHARS_PER_TOKEN
_ACTIVITY_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S %z"
# 工具结果片段的最大长度；超过此阈值的结果改为落盘并只保留片段
_MAX_RESULT_SNIPPET = 1000
_MAX_INLINE_PAYLOAD_CHARS = 2500


# 子 Agent 活动追踪中间件：把子 agent 的每次模型调用/工具调用记录成一份"活动日志"文件，
# 供主 agent 事后按需回读。日志用可压缩结构（CompressibleMarkdownLog）承接，超出 token 预算时用 LLM
# 把较早条目压成要点；超大工具结果单独落盘为 payload、正文只保留片段，避免活动日志本身膨胀。
# 到达最终回复时把整份日志落盘，并在回复末尾追加日志文件引用。
class SubagentActivityMiddleware(AgentMiddleware):
    """Record a subagent's model/tool activity to a backend-readable file."""

    def __init__(
        self,
        *,
        backend: Any,
        token_limit: int = _DEFAULT_ACTIVITY_TOKEN_LIMIT,
        keep_recent: int = _DEFAULT_KEEP_RECENT,
        max_log_chars: int = _DEFAULT_MAX_LOG_CHARS,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()
        self._backend = backend
        # 本次子 agent 运行的稳定 id（用于命名活动日志与 payload 文件）
        self._run_id = (run_id_factory or (lambda: uuid.uuid4().hex[:8]))()
        # payload 文件序号计数器
        self._payload_counter = 0
        # 活动日志已写入路径（写一次后缓存）
        self._written_path: str | None = None
        # 复用可压缩日志承接活动记录，超限时压缩旧条目
        self._log = CompressibleMarkdownLog(
            token_limit=token_limit,
            keep_recent=keep_recent,
            max_log_chars=max(int(max_log_chars), 1),
            compressed_heading="Summary of Earlier Activity",
            truncated_label="activity entries",
        )
        # 完整对话转写内容（结束时优先用它作为最终活动文件内容）
        self._transcript_content: str | None = None

    def _get_backend(self, runtime: Any) -> Any:
        # backend 可为实例或按 runtime 解析的工厂
        if callable(self._backend):
            return self._backend(runtime)
        return self._backend

    @staticmethod
    def _timestamp() -> str:
        return time.strftime(_ACTIVITY_TIMESTAMP_FORMAT)

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        # 超长文本保留首尾各一半，中间用省略号代替（保留上下文两端信息）
        if len(text) <= limit:
            return text
        half = max(limit // 2 - 3, 1)
        return text[:half] + "\n...\n" + text[-half:]

    @staticmethod
    async def _json_dumps(value: Any, *, indent: int | None = None) -> str:
        # JSON 序列化放线程池执行
        return await run_blocking_io(json.dumps, value, ensure_ascii=False, indent=indent)

    @classmethod
    async def _content_to_text(cls, content: Any) -> str:
        # 把消息内容规整为文本（字符串直用，块列表取 text/序列化，其余 JSON/str）
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(await cls._json_dumps(block, indent=2))
            return "\n".join(part for part in parts if part)
        if isinstance(content, (dict, tuple)):
            return await cls._json_dumps(content, indent=2)
        if content is None:
            return ""
        return str(content)

    async def _serialize_tool_result(self, result: Any) -> str:
        # 把工具结果序列化为文本（ToolMessage 取内容，容器类 JSON 化）
        if isinstance(result, ToolMessage):
            return await self._content_to_text(result.content)
        if isinstance(result, (dict, list, tuple)):
            return await self._json_dumps(result, indent=2)
        if result is None:
            return ""
        return str(result)

    def _format_args(self, args: dict[str, Any]) -> str:
        # 格式化工具参数；对超长的 content/old_string/new_string 折叠为
        # "<N chars>" 并附带截断片段，避免活动日志被大文本淹没
        if not args:
            return ""
        compact: dict[str, Any] = {}
        for key, value in args.items():
            if (
                key in {"content", "old_string", "new_string"}
                and isinstance(value, str)
                and len(value) > 240
            ):
                compact[key] = f"<{len(value)} chars>"
                compact[f"{key}_snippet"] = self._truncate(value, 240)
            else:
                compact[key] = value
        return ", ".join(f"{key}={value!r}" for key, value in compact.items())

    def _next_payload_filename(self, kind: str, label: str, extension: str = "txt") -> str:
        # 生成递增编号的 payload 文件名；label 清洗为安全字符
        self._payload_counter += 1
        safe_label = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or kind
        return (
            f"payloads/{self._run_id}/{self._payload_counter:04d}_{kind}_{safe_label}.{extension}"
        )

    async def _write_payload(
        self,
        runtime: Any,
        *,
        kind: str,
        label: str,
        content: str,
    ) -> str | None:
        # 把超大 payload 单独写入交接目录，返回其路径
        backend = self._get_backend(runtime)
        return await write_subagent_handoff_file(
            backend,
            dirname="subagent_activity",
            filename=self._next_payload_filename(kind, label),
            content=content,
            log_context="SubagentActivity",
        )

    def _append(self, entry: str) -> None:
        # 非空条目才追加到活动日志
        if entry:
            self._log.append(entry)

    @staticmethod
    def _messages_from_request(request: Any) -> list[Any]:
        # 从 request.state 或 runtime.state 取消息列表
        state = getattr(request, "state", None)
        if isinstance(state, dict) and isinstance(state.get("messages"), list):
            return state["messages"]

        runtime = getattr(request, "runtime", None)
        runtime_state = getattr(runtime, "state", None)
        if isinstance(runtime_state, dict) and isinstance(runtime_state.get("messages"), list):
            return runtime_state["messages"]
        return []

    @staticmethod
    def _messages_have_process_activity(messages: list[Any]) -> bool:
        # 判断消息里是否有"过程性活动"（工具消息或带工具调用的 AI 消息）
        # 只有存在过程活动，转写才有记录价值
        for message in messages:
            if isinstance(message, ToolMessage):
                return True
            if isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
                return True
        return False

    async def _capture_transcript_from_request(self, request: Any) -> None:
        # 子 agent 结束时抓取完整对话转写（作为最终活动文件内容）
        messages = self._messages_from_request(request)
        if not messages or not self._messages_have_process_activity(messages):
            return
        content = await format_messages_as_markdown(messages)
        if content.strip():
            self._transcript_content = content

    async def _build_tool_entry(
        self,
        runtime: Any,
        name: str,
        args: dict[str, Any],
        result_text: str,
    ) -> str:
        # 构造一条工具活动记录；结果过大则落盘为 payload 并只内联片段
        result_snippet = result_text
        payload_path: str | None = None
        if len(result_text) > _MAX_INLINE_PAYLOAD_CHARS:
            payload_path = await self._write_payload(
                runtime,
                kind="tool",
                label=name,
                content=result_text,
            )
            result_snippet = self._truncate(result_text, _MAX_RESULT_SNIPPET)

        entry = (
            f"\n## [{self._timestamp()}] Tool: {name}\n"
            f"Args: {self._format_args(args)}\n"
            f"Result: {result_snippet}"
        )
        # 附带完整 payload 路径，便于需要时回溯
        if payload_path:
            entry += f"\nFull payload: {payload_path}"
        return entry

    async def _build_model_entry(self, message: AIMessage) -> str:
        # 构造一条 LLM 活动记录（正文摘要 + 工具调用名）
        text = (await self._content_to_text(message.content)).strip()
        parts = [f"\n## [{self._timestamp()}] LLM"]
        if text:
            parts.append(f"> {self._truncate(text, 1200)}")
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            names = [
                call.get("name", "?") if isinstance(call, dict) else str(call)
                for call in tool_calls
            ]
            parts.append(f"Tool calls: {', '.join(names)}")
        # 只有纯 LLM 标题、无正文无工具调用时返回空串（不记录）
        return "\n".join(parts) if len(parts) > 1 else ""

    async def _compress_with_llm(self, text: str) -> str:
        # 用低温度 LLM 把活动日志压成要点（保留发现/路径/结果/决策/关键值）
        from langchain_core.messages import HumanMessage

        from src.infra.llm.client import LLMClient

        llm = await LLMClient.get_model(temperature=0.3)
        prompt = (
            "Compress the following subagent activity log into concise markdown bullets.\n"
            "Keep key findings, file paths, tool outcomes, decisions, and important values.\n\n"
            f"{text}"
        )
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        return response.content if isinstance(response.content, str) else str(response.content)

    async def _check_and_compress(self) -> None:
        # 触发日志压缩（失败降级为保留裁剪后的原始活动，不抛出）
        try:
            await self._log.check_and_compress(self._compress_with_llm)
        except Exception:
            logger.warning("[SubagentActivity] Compression failed, keeping trimmed raw activity")

    async def _persist_log(self, runtime: Any) -> str | None:
        # 把活动日志落盘为一个 md 文件，返回路径（只写一次）
        if self._written_path:
            return self._written_path
        # 优先用完整转写；否则用逐条累积的活动日志渲染
        content = self._transcript_content
        if not content and self._log.entries:
            content = self._log.render(f"# Subagent Activity Log (run: {self._run_id})\n")
        if not content:
            return None

        backend = self._get_backend(runtime)
        self._written_path = await write_subagent_handoff_file(
            backend,
            dirname="subagent_activity",
            filename=f"activity_{self._run_id}.md",
            # 保证内容带标题头
            content=content
            if content.startswith("#")
            else f"# Subagent Activity Log (run: {self._run_id})\n{content}",
            log_context="SubagentActivity",
        )
        return self._written_path

    @staticmethod
    def _copy_ai_message_with_content(message: AIMessage, content: str | list[Any]) -> AIMessage:
        # 复制 AIMessage 但替换 content（保留工具调用/id/元数据）
        return AIMessage(
            content=content,
            tool_calls=message.tool_calls,
            id=message.id,
            additional_kwargs=message.additional_kwargs,
            response_metadata=message.response_metadata,
        )

    @staticmethod
    def _append_reference(message: AIMessage, path: str) -> AIMessage:
        # 在最终回复末尾追加活动日志文件引用（兼容 str / 块列表两种 content）
        reference = f"\n\n[Activity log saved to: {path}]"
        if isinstance(message.content, list):
            content: str | list[Any] = [*message.content, {"type": "text", "text": reference}]
        else:
            content = f"{message.content or ''}{reference}"
        return SubagentActivityMiddleware._copy_ai_message_with_content(message, content)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        # 工具调用后钩子：记录一条工具活动，然后按需压缩
        result = await handler(request)
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_args = tool_call.get("args", {}) or {}
        result_text = await self._serialize_tool_result(result)
        self._append(
            await self._build_tool_entry(
                getattr(request, "runtime", None),
                str(tool_call.get("name", "")),
                dict(tool_args) if isinstance(tool_args, dict) else {},
                result_text,
            )
        )
        await self._check_and_compress()
        return result

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        # 模型调用后钩子
        response = await handler(request)

        # 从响应中取出首条 AI 消息
        messages: list[Any] = []
        if isinstance(response, AIMessage):
            messages = [response]
        elif hasattr(response, "result"):
            messages = getattr(response, "result") or []

        if not messages or not isinstance(messages[0], AIMessage):
            return response

        ai_message = messages[0]

        # 仍在调工具（非最终回复）：仅记录一条 LLM 活动并压缩，然后原样返回
        if getattr(ai_message, "tool_calls", None):
            self._append(await self._build_model_entry(ai_message))
            await self._check_and_compress()
            return response

        # 到达最终回复：抓取转写、落盘活动日志
        await self._capture_transcript_from_request(request)
        path = await self._persist_log(getattr(request, "runtime", None))
        if not path:
            return response

        # 在最终回复里追加日志文件引用，按响应类型重新包装返回
        new_ai = self._append_reference(ai_message, path)
        if hasattr(response, "result"):
            return type(response)(result=[new_ai])
        return new_ai  # type: ignore[return-value]

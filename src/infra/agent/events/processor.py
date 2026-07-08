"""
DeepAgent event processor.

This module keeps the public `AgentEventProcessor` entry point while delegating
the heavier event-specific work to focused helper modules.
"""

import logging
from io import StringIO
from typing import Any

from src.infra.agent.events.binary_uploads import upload_binary_blocks
from src.infra.agent.events.buffers import TextChunkBuffer
from src.infra.agent.events.debug_logger import debug_log_event
from src.infra.agent.events.stream import StreamEventMixin
from src.infra.agent.events.subagents import SubagentEventMixin
from src.infra.agent.events.tool_events import ToolEventMixin
from src.infra.agent.events.tool_outputs import (
    MCP_MEDIA_TYPES,
    MCP_SKIP_KEYS,
    collect_blocks,
    detect_tool_error,
    extract_tool_output,
    get_tool_status,
    normalize_content,
    process_messages,
)
from src.infra.agent.events.types import TOOL_TASK, StreamEvent
from src.infra.logging import get_logger
from src.infra.writer.present import Presenter

logger = get_logger(__name__)

# 只有这几类事件携带 agent/工具上下文，process_event 仅对它们做深度路由
_CONTEXT_EVENT_TYPES = frozenset(
    ("on_chat_model_stream", "on_tool_start", "on_tool_end", "on_tool_error")
)

# RubricMiddleware 内部评分子 agent 的链名，用于识别其 chain start/end
RUBRIC_GRADER = "rubric_grader"
# 这些来源属于内部工具流（如图像分析），不应作为可见输出转发给前端
INTERNAL_STREAM_SOURCES = frozenset(("image_analysis_tool",))
# 顶层助手输出文本累计上限，超过后不再往 output_buffer 复制（防内存膨胀）
OUTPUT_TEXT_COPY_MAX_CHARS = 8_000


# 通过多继承把三大类事件处理拆到独立 mixin：子 agent（Subagent）、
# 流式文本（Stream）、工具（Tool），本类只保留公共入口与路由逻辑
class AgentEventProcessor(SubagentEventMixin, StreamEventMixin, ToolEventMixin):
    """
    Process DeepAgent stream events and forward presenter-ready events.

    The processor is session-scoped. Call `flush()` before reading final output,
    and call `clear()` or `finalize()` when the session is no longer needed.
    Token counters are intentionally retained after `clear()` for existing
    callers that emit usage after stream cleanup.
    """

    # 用 __slots__ 固定实例属性：处理器按会话高频创建，减少内存与属性查找开销
    __slots__ = (
        "presenter",
        "checkpoint_to_agent",
        "thinking_ids",
        "_output_buffer",
        "total_input_tokens",
        "total_output_tokens",
        "total_tokens",
        "total_cache_creation_tokens",
        "total_cache_read_tokens",
        "_token_usage_emitted",
        "_output_buffer_chars",
        "_presenter_emit",
        "_base_url",
        "_chunk_buffer",
        "_summary_chunk_buffer",
        "_thinking_chunk_buffer",
        "_agent_context_cache",
        "_subagent_display_names",
        "_subagent_avatars",
        "_started_tool_call_ids",
        "_rubric_grader_active",
        "_rubric_grader_id",
    )

    # 文本分块缓冲的刷出阈值（字符数）：攒够 200 字符再作为一个 chunk 发出
    _CHUNK_FLUSH_SIZE = 200

    def __init__(
        self,
        presenter: Presenter,
        base_url: str = "",
        subagent_display_names: dict[str, str] | None = None,
        subagent_avatars: dict[str, str] | None = None,
    ):
        # presenter 负责把内部事件渲染成前端 SSE 载荷
        self.presenter = presenter
        # checkpoint_ns -> (agent_id, agent_name) 映射，用于识别事件属于哪个（子）agent
        self.checkpoint_to_agent: dict[str, tuple[str, str]] = {}
        # base_url 缺省时从配置读取，用于拼接二进制文件的代理下载地址
        if not base_url:
            from src.kernel.config import settings

            base_url = getattr(settings, "APP_BASE_URL", "").rstrip("/")
        self._base_url = base_url
        # 每个流键当前的 thinking 事件 id，用于把连续 thinking 片段归到同一条
        self.thinking_ids: dict[str | None, str | None] = {}
        # 顶层助手输出文本的累积缓冲及其字符计数
        self._output_buffer = StringIO()
        self._output_buffer_chars = 0
        # 各类 token 计数（输入/输出/总量/缓存创建/缓存命中）
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_tokens = 0
        self.total_cache_creation_tokens = 0
        self.total_cache_read_tokens = 0
        # 保证 token 用量事件只发一次
        self._token_usage_emitted = False
        # 缓存 presenter.emit 的绑定方法，减少属性查找
        self._presenter_emit = presenter.emit
        # 三个独立的文本缓冲：正文、摘要（summarization）、思考（thinking）
        self._chunk_buffer = TextChunkBuffer(self._CHUNK_FLUSH_SIZE)
        self._summary_chunk_buffer = TextChunkBuffer(self._CHUNK_FLUSH_SIZE)
        self._thinking_chunk_buffer = TextChunkBuffer(self._CHUNK_FLUSH_SIZE)
        # checkpoint_ns -> (agent_id, depth) 的解析缓存，避免重复解析命名空间
        self._agent_context_cache: dict[str, tuple[str | None, int]] = {}
        # 子 agent 的展示名与头像映射（前端展示用）
        self._subagent_display_names = subagent_display_names or {}
        self._subagent_avatars = subagent_avatars or {}
        # 已发出 start 事件的工具调用 id，避免重复发 start
        self._started_tool_call_ids: set[str] = set()
        # rubric grader 是否处于激活态，及其临时生成的 agent id
        self._rubric_grader_active: bool = False
        self._rubric_grader_id: str | None = None

    @property
    def output_text(self) -> str:
        """Return accumulated top-level assistant output text."""
        return self._output_buffer.getvalue()

    async def flush(self) -> None:
        """Flush pending stream chunks without clearing counters or output text."""
        # 把三个文本缓冲里未满阈值的残余内容立即刷出（正文/摘要/思考）
        await self._flush_chunk_buffer()
        await self._flush_summary_chunk_buffer()
        await self._flush_thinking_chunk_buffer()

    async def finalize(self) -> None:
        """Flush pending chunks and release session-scoped buffers."""
        # 会话结束：先刷出残余再释放缓冲（token 计数仍保留供后续读取）
        await self.flush()
        self.clear()

    async def emit_token_usage(
        self,
        *,
        duration: float = 0.0,
        model_id: str | None = None,
        model: str | None = None,
    ) -> bool:
        """Emit accumulated token usage once, preserving counters for late cleanup paths."""
        # 幂等保护：已发过则不再重复发
        if self._token_usage_emitted:
            return False

        # 没有任何 token 计数则无需上报
        if not (
            self.total_input_tokens > 0 or self.total_output_tokens > 0 or self.total_tokens > 0
        ):
            return False

        # total 优先用累计值，否则由输入+输出推算
        total_tokens = self.total_tokens or self.total_input_tokens + self.total_output_tokens
        event = self.presenter.present_token_usage(
            input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens,
            total_tokens=total_tokens,
            duration=duration,
            cache_creation_tokens=self.total_cache_creation_tokens,
            cache_read_tokens=self.total_cache_read_tokens,
            model_id=model_id,
            model=model,
        )
        await self._presenter_emit(event)
        self._token_usage_emitted = True
        return True

    def clear(self) -> None:
        """Release memory held by this session while preserving token counters."""
        # 释放输出缓冲并重置为新的空 StringIO
        self._output_buffer.close()
        self._output_buffer = StringIO()
        self._output_buffer_chars = 0
        # 清空各类会话级映射与缓冲；注意 token 计数刻意不清（供 clear 后上报）
        self.checkpoint_to_agent.clear()
        self.thinking_ids.clear()
        self._agent_context_cache.clear()
        self._chunk_buffer.clear()
        self._summary_chunk_buffer.clear()
        self._thinking_chunk_buffer.clear()
        self._started_tool_call_ids.clear()

    async def process_event(self, event: StreamEvent) -> None:
        """Process a single LangChain stream event."""
        # 先把原始事件写入调试日志（仅在开关开启时真正落盘）
        await debug_log_event(event, self._debug_log_context())
        # 事件类型（如 on_tool_start）与事件名（工具名/链名）
        evt_type = event.get("event")
        event_name = event.get("name", "")

        # ── Rubric grader chain detection ──
        # The RubricMiddleware runs a grader sub-agent inside the graph.
        # Detect its chain start/end so we can emit agent:call / agent:result
        # and suppress all internal events to avoid polluting the main stream.
        # ── 识别 rubric 评分子链 ──
        # 评分中间件会在图内跑一个 grader 子 agent；这里捕获它的 chain 起止，
        # 对外发 agent:call / agent:result，并在其运行期间屏蔽内部事件，避免污染主流
        if event_name == RUBRIC_GRADER:
            match evt_type:
                case "on_chain_start":
                    # 进入评分：置激活标记，用 run_id 前 8 位生成稳定的 agent id
                    self._rubric_grader_active = True
                    run_id = event.get("run_id", "")
                    self._rubric_grader_id = f"rubric_grader_{run_id[:8]}"
                    await self._presenter_emit(
                        self.presenter.present_agent_call(
                            agent_id=self._rubric_grader_id,
                            agent_name="Rubric Grader",
                            input_message="Evaluating against rubric criteria",
                            depth=1,
                        )
                    )
                    return
                case "on_chain_end":
                    # 评分结束：抽取结构化结果，非 "failed" 即视为成功
                    sr = self._extract_rubric_result(event)
                    success = sr.get("result", "completed") != "failed"
                    result_text = sr.get("result", "completed")
                    explanation = sr.get("explanation", "")
                    # 有解释文本时附加到结果后一并展示
                    if explanation:
                        result_text = f"{result_text}\n{explanation}"
                    await self._presenter_emit(
                        self.presenter.present_agent_result(
                            agent_id=self._rubric_grader_id or "rubric_grader",
                            result=result_text,
                            success=success,
                            depth=1,
                        )
                    )
                    # 复位激活标记
                    self._rubric_grader_active = False
                    self._rubric_grader_id = None
                    return

        tool_name = event_name

        # 内置 task 工具代表子 agent 派发，交给子 agent 专用处理
        if tool_name == TOOL_TASK:
            match evt_type:
                case "on_tool_start":
                    await self._handle_task_start(event)
                    return
                case "on_tool_end":
                    await self._handle_task_end(event)
                    return
                case "on_tool_error":
                    await self._handle_task_error(event)
                    return

        # 模型一轮结束：刷出残余文本并统计本轮 token 用量
        if evt_type == "on_chat_model_end":
            await self.flush()
            self._handle_token_usage(event)
            return

        # 其余非上下文事件（不带 agent/工具语义）直接忽略
        if evt_type not in _CONTEXT_EVENT_TYPES:
            return

        # Hide grader internals from the public stream. The chain start/end
        # events above already provide user-visible grading status; routing
        # the internal GraderResponse tool call makes the UI look stuck in a
        # tool loop while the middleware is just validating structured output.
        # 评分期间屏蔽其内部工具事件：上面的 chain 起止已给出可见状态，
        # 若把内部 GraderResponse 工具调用透出，UI 会误显示成卡在工具循环里
        if self._rubric_grader_active:
            return

        # 内部工具流（如图像分析）不作为可见输出，直接跳过
        metadata = event.get("metadata", {})
        if self._is_internal_stream_event(metadata):
            return

        # 由 checkpoint 命名空间解析出当前（子）agent 及其层级深度
        checkpoint_ns = None
        checkpoint_ns = self._get_checkpoint_ns(metadata)
        current_agent_id, current_depth = self._get_agent_context(checkpoint_ns)

        # 子 agent 事件的调试日志（仅 DEBUG 级别时才计算，避免开销）
        if current_depth and logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[Subagent] %s/%s: agent=%s, depth=%d, ns=%s",
                evt_type,
                tool_name or "N/A",
                current_agent_id,
                current_depth,
                checkpoint_ns[:60] if checkpoint_ns else "N/A",
            )

        # 按事件类型分发到对应处理器；工具类事件在处理前先 flush 保证文本时序
        match evt_type:
            case "on_chat_model_stream":
                # summarization 来源走摘要流，其余走正文聊天流
                if self._get_lc_source(metadata) == "summarization":
                    await self._handle_summary_stream(event, current_agent_id, current_depth)
                else:
                    await self._handle_chat_stream(event, current_agent_id, current_depth)
            case "on_tool_start":
                await self.flush()
                await self._handle_tool_start(event, tool_name, current_agent_id, current_depth)
            case "on_tool_end":
                await self.flush()
                await self._handle_tool_end(event, tool_name, current_agent_id, current_depth)
            case "on_tool_error":
                await self.flush()
                await self._handle_tool_error(event, tool_name, current_agent_id, current_depth)

    # 把 tool_outputs 模块的纯函数挂为静态方法，供各 mixin 复用（避免重复实现）
    _extract_tool_output = staticmethod(extract_tool_output)
    _detect_tool_error = staticmethod(detect_tool_error)
    _get_tool_status = staticmethod(get_tool_status)
    _collect_blocks = staticmethod(collect_blocks)
    _normalize_content = staticmethod(normalize_content)
    _process_messages = staticmethod(process_messages)
    _MCP_MEDIA_TYPES = MCP_MEDIA_TYPES
    _MCP_SKIP_KEYS = MCP_SKIP_KEYS

    def _debug_log_context(self) -> dict[str, Any]:
        """Return LambChat run identity for raw stream-event debug logs."""
        # 从 presenter/config 上抽取 trace/run/session 等标识，附加到调试日志
        config = getattr(self.presenter, "config", None)
        return {
            "trace_id": getattr(self.presenter, "trace_id", None),
            "run_id": getattr(self.presenter, "run_id", None),
            "session_id": getattr(config, "session_id", None),
            "agent_id": getattr(config, "agent_id", None),
            "agent_name": getattr(config, "agent_name", None),
        }

    @staticmethod
    def _is_internal_stream_event(metadata: dict[str, Any]) -> bool:
        # 通过 internal_tool_call 标记或来源白名单判断是否为内部流事件
        source = metadata.get("lc_source") or metadata.get("source")
        return bool(metadata.get("internal_tool_call") or source in INTERNAL_STREAM_SOURCES)

    async def _upload_binary_blocks(self, result: dict) -> None:
        # 转发到独立的二进制上传实现，绑定当前会话的 base_url
        await upload_binary_blocks(result, self._base_url)

    @staticmethod
    def _extract_rubric_result(event: StreamEvent) -> dict:
        """Extract the structured rubric evaluation from a grader chain-end event."""
        # 从 chain-end 事件里取 output.structured_response
        output: Any = event.get("data", {}).get("output", {})
        if not isinstance(output, dict):
            return {}
        sr = output.get("structured_response")
        if sr is None:
            return {}
        # 已是 dict 直接返回
        if isinstance(sr, dict):
            return sr
        # Pydantic model or similar object
        # 否则按 Pydantic 模型对象逐字段取值
        return {k: getattr(sr, k, "") for k in ("result", "explanation", "criteria")}


__all__ = ["AgentEventProcessor", "INTERNAL_STREAM_SOURCES", "StreamEvent"]

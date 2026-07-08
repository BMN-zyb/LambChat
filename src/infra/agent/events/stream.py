"""Chat, summary, and token usage stream handlers."""

from __future__ import annotations

from io import StringIO
from typing import Any

from src.infra.agent.events.buffers import BufferKey, TextChunkBuffer
from src.infra.agent.events.types import StreamEvent, get_value


def _first_int(*values: Any) -> int | None:
    # 返回参数中第一个是 int 的值，用于在多套 usage 字段命名中取到有效计数
    for value in values:
        if isinstance(value, int):
            return value
    return None


# 流式文本处理 mixin：被 AgentEventProcessor 继承，负责 chat/thinking/summary 三类文本流
# 以及 token 用量统计。这里仅声明依赖的属性类型，实际状态由处理器 __init__ 初始化
class StreamEventMixin:
    _chunk_buffer: TextChunkBuffer
    _summary_chunk_buffer: TextChunkBuffer
    _thinking_chunk_buffer: TextChunkBuffer
    _output_buffer: StringIO
    _presenter_emit: Any
    presenter: Any
    thinking_ids: dict[str | None, str | None]
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cache_creation_tokens: int
    total_cache_read_tokens: int
    _output_buffer_chars: int

    def _append_output_text(self, text: str) -> None:
        # 累积顶层助手输出文本，供会话结束后读取 output_text
        from src.infra.agent.events.processor import OUTPUT_TEXT_COPY_MAX_CHARS

        # 空文本或已达上限则不再累积（防止超长输出撑爆内存）
        if not text or self._output_buffer_chars >= OUTPUT_TEXT_COPY_MAX_CHARS:
            return

        # 只截取到上限剩余空间的部分写入
        remaining = OUTPUT_TEXT_COPY_MAX_CHARS - self._output_buffer_chars
        clipped = text[:remaining]
        self._output_buffer.write(clipped)
        self._output_buffer_chars += len(clipped)

    async def _flush_chunk_buffer(self) -> None:
        # 取出正文缓冲的全部残余并发出
        text, key = self._chunk_buffer.consume()
        await self._emit_text_flush(text, key)

    async def _flush_summary_chunk_buffer(self) -> None:
        # 取出摘要缓冲的全部残余并发出
        text, key = self._summary_chunk_buffer.consume()
        await self._emit_summary_flush(text, key)

    async def _flush_thinking_chunk_buffer(self) -> None:
        # 取出思考缓冲的全部残余并发出
        text, key = self._thinking_chunk_buffer.consume()
        await self._emit_thinking_flush(text, key)

    async def _emit_text_flush(self, text: str, key: BufferKey | None) -> None:
        # 空文本或无 key 不发出
        if not text or key is None:
            return

        # BufferKey 解包为 (深度, agent_id, 文本 id)，交给 presenter 渲染正文事件
        depth, agent_id, text_id = key
        await self._presenter_emit(
            self.presenter.present_text(
                text,
                text_id=text_id,
                depth=depth,
                agent_id=agent_id,
            )
        )

    async def _emit_summary_flush(self, text: str, key: BufferKey | None) -> None:
        if not text or key is None:
            return

        # 摘要（对话历史压缩）文本走 present_summary
        depth, agent_id, summary_id = key
        await self._presenter_emit(
            self.presenter.present_summary(
                text,
                summary_id=summary_id,
                depth=depth,
                agent_id=agent_id,
            )
        )

    async def _emit_thinking_flush(self, text: str, key: BufferKey | None) -> None:
        if not text or key is None:
            return

        # 思考/推理文本走 present_thinking
        depth, agent_id, thinking_id = key
        await self._presenter_emit(
            self.presenter.present_thinking(
                text,
                thinking_id=thinking_id,
                depth=depth,
                agent_id=agent_id,
            )
        )

    def _buffer_text_chunk(
        self,
        text: str,
        depth: int,
        agent_id: str | None,
        text_id: str | None,
    ) -> list[tuple[str, BufferKey | None]] | None:
        # 把正文片段写入缓冲，返回需要立即发出的刷出列表（可能为空 -> None）
        key: BufferKey = (depth, agent_id, text_id)
        ready_flushes = []
        # 流键切换（换了 agent/文本 id）时先刷出旧内容，避免不同来源文本粘连
        ready = self._chunk_buffer.consume_ready(key)
        if ready is not None:
            ready_flushes.append(ready)
        # append 返回 True 表示已达阈值，需要再刷出一次
        if self._chunk_buffer.append(text, key):
            ready_flushes.append(self._chunk_buffer.consume())
        return ready_flushes or None

    def _buffer_summary_chunk(
        self,
        text: str,
        depth: int,
        agent_id: str | None,
        summary_id: str | None,
    ) -> list[tuple[str, BufferKey | None]] | None:
        # 摘要片段缓冲，逻辑同 _buffer_text_chunk
        key: BufferKey = (depth, agent_id, summary_id)
        ready_flushes = []
        ready = self._summary_chunk_buffer.consume_ready(key)
        if ready is not None:
            ready_flushes.append(ready)
        if self._summary_chunk_buffer.append(text, key):
            ready_flushes.append(self._summary_chunk_buffer.consume())
        return ready_flushes or None

    def _buffer_thinking_chunk(
        self,
        text: str,
        depth: int,
        agent_id: str | None,
        thinking_id: str | None,
    ) -> list[tuple[str, BufferKey | None]] | None:
        # 思考片段缓冲，逻辑同 _buffer_text_chunk
        key: BufferKey = (depth, agent_id, thinking_id)
        ready_flushes = []
        ready = self._thinking_chunk_buffer.consume_ready(key)
        if ready is not None:
            ready_flushes.append(ready)
        if self._thinking_chunk_buffer.append(text, key):
            ready_flushes.append(self._thinking_chunk_buffer.consume())
        return ready_flushes or None

    def _handle_token_usage(self, event: StreamEvent) -> None:
        # 从模型结束事件里取输出消息对象
        response = event.get("data", {}).get("output")
        if not response:
            return

        # 兼容不同提供商的 usage 存放位置：优先 usage_metadata，
        # 再退回 response_metadata / metadata 下的 token_usage / usage
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            response_metadata = getattr(response, "response_metadata", None)
            if response_metadata:
                usage = response_metadata.get("token_usage") or response_metadata.get("usage")
        if usage is None:
            metadata = getattr(response, "metadata", None)
            if metadata:
                usage = metadata.get("token_usage") or metadata.get("usage")

        if usage is None:
            return

        # 输入 token：兼容 Anthropic(input_tokens)/OpenAI(prompt_tokens)/Gemini(prompt_token_count)
        input_tok = _first_int(
            get_value(usage, "input_tokens", None),
            get_value(usage, "prompt_tokens", None),
            get_value(usage, "prompt_token_count", None),
        )
        # 输出 token：同样兼容三家命名
        output_tok = _first_int(
            get_value(usage, "output_tokens", None),
            get_value(usage, "completion_tokens", None),
            get_value(usage, "candidates_token_count", None),
        )
        # 总 token
        total_tok = _first_int(
            get_value(usage, "total_tokens", None),
            get_value(usage, "total_token_count", None),
        )

        # 累加到会话级计数器（每轮模型结束都会调用）
        if isinstance(input_tok, int):
            self.total_input_tokens += input_tok
        if isinstance(output_tok, int):
            self.total_output_tokens += output_tok
        if isinstance(total_tok, int):
            self.total_tokens += total_tok

        # 缓存 token 明细：优先取 input_token_details，退回 prompt_tokens_details
        input_details = get_value(usage, "input_token_details", {})
        if not input_details:
            input_details = get_value(usage, "prompt_tokens_details", {})
        cache_creation = None
        cache_read = None
        if input_details:
            # 缓存写入（创建）token
            cache_creation = _first_int(
                get_value(input_details, "cache_creation", None),
                get_value(input_details, "cache_creation_input_tokens", None),
            )
            # 缓存命中（读取）token，兼容各家字段名
            cache_read = _first_int(
                get_value(input_details, "cache_read", None),
                get_value(input_details, "cached_tokens", None),
                get_value(input_details, "cached_content_token_count", None),
            )

        # 明细里没取到时，再从 usage 顶层兜底
        if cache_read is None:
            cache_read = _first_int(
                get_value(usage, "cached_content_token_count", None),
                get_value(usage, "cache_read_input_tokens", None),
            )
        if cache_creation is None:
            cache_creation = _first_int(get_value(usage, "cache_creation_input_tokens", None))

        # 累加缓存相关计数
        if cache_creation is not None:
            self.total_cache_creation_tokens += cache_creation
        if cache_read is not None:
            self.total_cache_read_tokens += cache_read

    async def _handle_summary_stream(
        self,
        event: StreamEvent,
        current_agent_id: str | None,
        current_depth: int,
    ) -> None:
        # 处理 summarization 来源的流式片段（历史压缩产生的摘要）
        data = event.get("data", {})
        chunk = data.get("chunk")
        if not chunk:
            return

        content = chunk.content
        summary_id = chunk.id

        # 情形一：content 直接是字符串
        if isinstance(content, str) and content:
            ready_flushes = self._buffer_summary_chunk(
                content,
                current_depth,
                current_agent_id,
                summary_id,
            )
            if ready_flushes:
                for ready in ready_flushes:
                    await self._emit_summary_flush(*ready)
            return

        # 情形二：content 是块列表，遍历取出其中的 text 块
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text", "")
                if text:
                    ready_flushes = self._buffer_summary_chunk(
                        text,
                        current_depth,
                        current_agent_id,
                        summary_id,
                    )
                    if ready_flushes:
                        for ready in ready_flushes:
                            await self._emit_summary_flush(*ready)

    async def _handle_chat_stream(
        self,
        event: StreamEvent,
        current_agent_id: str | None,
        current_depth: int,
    ) -> None:
        # 处理常规聊天流式片段，需区分正文文本与思考(thinking/reasoning)内容
        data = event.get("data", {})
        chunk = data.get("chunk")
        if not chunk:
            return

        content = chunk.content
        chunk_id = chunk.id

        # 情形一：非空字符串 = 正文文本。先把待发的思考内容刷出（正文标志思考结束）
        if isinstance(content, str) and content:
            await self._flush_thinking_chunk_buffer()
            # 仅顶层(depth==0)的正文累积到 output_text
            if current_depth == 0:
                self._append_output_text(content)
            ready_flushes = self._buffer_text_chunk(
                content,
                current_depth,
                current_agent_id,
                chunk_id,
            )
            if ready_flushes:
                for ready in ready_flushes:
                    await self._emit_text_flush(*ready)
            return

        # 情形二：空字符串但 additional_kwargs 带 reasoning_content（部分模型的思考流）
        if isinstance(content, str) and not content:
            rc = getattr(chunk, "additional_kwargs", {}).get("reasoning_content")
            if rc:
                ready_flushes = self._buffer_thinking_chunk(
                    rc,
                    current_depth,
                    current_agent_id,
                    chunk_id,
                )
                if ready_flushes:
                    for ready in ready_flushes:
                        await self._emit_thinking_flush(*ready)
            return

        # 情形三：content 是块列表，逐块按类型分流（thinking/reasoning vs text）
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                # 思考/推理块：写入思考缓冲
                if block_type in ("thinking", "reasoning"):
                    reasoning_text = block.get("thinking") or block.get("reasoning", "")
                    if reasoning_text:
                        ready_flushes = self._buffer_thinking_chunk(
                            reasoning_text,
                            current_depth,
                            current_agent_id,
                            chunk_id,
                        )
                        if ready_flushes:
                            for ready in ready_flushes:
                                await self._emit_thinking_flush(*ready)
                # 正文文本块：先刷出思考、重置该 agent 的 thinking_id，再走正文缓冲
                elif block_type == "text":
                    text = block.get("text", "")
                    if text:
                        await self._flush_thinking_chunk_buffer()
                        self.thinking_ids[current_agent_id] = None
                        if current_depth == 0:
                            self._append_output_text(text)
                        ready_flushes = self._buffer_text_chunk(
                            text,
                            current_depth,
                            current_agent_id,
                            chunk_id,
                        )
                        if ready_flushes:
                            for ready in ready_flushes:
                                await self._emit_text_flush(*ready)

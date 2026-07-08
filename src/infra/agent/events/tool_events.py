"""Generic tool start/end event handling."""

from __future__ import annotations

import json
import uuid
from typing import Any

import orjson

from src.infra.agent.events.binary_uploads import upload_binary_blocks
from src.infra.agent.events.tool_outputs import (
    detect_tool_error,
    extract_tool_output,
    normalize_content,
)
from src.infra.agent.events.types import StreamEvent
from src.infra.async_utils import run_blocking_io

# 工具结果展示的最大字符数，超出则截断（防止超大输出拖垮前端）
_TOOL_RESULT_DISPLAY_MAX_CHARS = 100_000
# 尝试 JSON 解析的最大字符数（与展示上限一致，避免解析超大字符串）
_TOOL_RESULT_JSON_PARSE_MAX_CHARS = _TOOL_RESULT_DISPLAY_MAX_CHARS


def _clip_tool_result_text(text: str) -> str:
    # 未超限直接返回
    if len(text) <= _TOOL_RESULT_DISPLAY_MAX_CHARS:
        return text
    # 截断并标注原始长度
    return (
        text[:_TOOL_RESULT_DISPLAY_MAX_CHARS].rstrip()
        + f"\n\n[truncated from {len(text)} chars for display]"
    )


def _parse_tool_result_json(raw: str) -> Any | None:
    # 优先用高性能的 orjson 解析，失败再退回标准库 json
    try:
        parsed = orjson.loads(raw)
    except orjson.JSONDecodeError:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    except TypeError:
        return None

    # dict 直接返回
    if isinstance(parsed, dict):
        return parsed
    # list 先归一化（可能是 MCP 内容块），归一化为 dict 则返回 dict，否则转字符串
    if isinstance(parsed, list):
        normalized = normalize_content(parsed)
        return normalized if isinstance(normalized, dict) else str(normalized)
    # 其余标量类型不作为结构化结果处理
    return None


# 通用工具事件处理 mixin：处理 task 之外的普通工具的 start/end/error 事件
class ToolEventMixin:
    _presenter_emit: Any
    presenter: Any
    _base_url: str
    _started_tool_call_ids: set[str]

    def _get_tool_call_id(self, event: StreamEvent) -> str:
        # 用 run_id 作为工具调用 id，缺失时生成随机 id
        return event.get("run_id") or f"tool_{uuid.uuid4().hex}"

    def _format_tool_error(self, tool_name: str, error: Any) -> str:
        # 把各种形态的错误统一格式化为 "[MCP Tool Error] ..." 文案
        if error is None:
            return f"[MCP Tool Error] {tool_name} failed: Unknown error"

        # 异常对象：取类型名 + 消息（无消息则用 repr）
        if isinstance(error, BaseException):
            error_type = type(error).__name__
            error_message = str(error) if str(error) else repr(error)
            return f"[MCP Tool Error] {tool_name} failed: [{error_type}] {error_message}"

        # dict 形态：尽量取出 type/message 等字段
        if isinstance(error, dict):
            error_type = error.get("type") or error.get("name") or "ToolError"
            error_message = error.get("message") or error.get("error") or str(error)
            return f"[MCP Tool Error] {tool_name} failed: [{error_type}] {error_message}"

        # 字符串/其它：若已带前缀则原样返回，避免重复包裹
        error_message = str(error) if str(error) else repr(error)
        if error_message.startswith("[MCP Tool Error]"):
            return error_message
        return f"[MCP Tool Error] {tool_name} failed: {error_message}"

    async def _handle_tool_start(
        self,
        event: StreamEvent,
        tool_name: str,
        current_agent_id: str | None,
        current_depth: int,
    ) -> None:
        # 工具开始：取输入与调用 id
        inp: dict[str, Any] = event.get("data", {}).get("input", {})
        tool_call_id = self._get_tool_call_id(event)

        # write_todos 是特殊工具：直接渲染为待办列表，不走通用工具卡片
        if tool_name == "write_todos":
            if isinstance(inp, dict):
                todos = inp.get("todos", [])
                if isinstance(todos, list) and todos:
                    await self._presenter_emit(
                        self.presenter.present_todo(
                            todos,
                            depth=current_depth,
                            agent_id=current_agent_id,
                        )
                    )
            return

        # 记录已发 start 的调用 id（供 error 分支判断是否需要补发 start）
        self._started_tool_call_ids.add(tool_call_id)
        await self._presenter_emit(
            self.presenter.present_tool_start(
                tool_name,
                inp,
                tool_call_id=tool_call_id,
                depth=current_depth,
                agent_id=current_agent_id,
            )
        )

    async def _handle_tool_end(
        self,
        event: StreamEvent,
        tool_name: str,
        current_agent_id: str | None,
        current_depth: int,
    ) -> None:
        # write_todos 已在 start 阶段处理，结束事件无需再发
        if tool_name == "write_todos":
            return

        data = event.get("data", {})
        out = data.get("output", "")
        tool_call_id = self._get_tool_call_id(event)

        # 抽取原始输出并检测是否为错误（均为可能耗时的处理，放到线程池）
        raw = await run_blocking_io(extract_tool_output, out)
        is_error, error_message = await run_blocking_io(detect_tool_error, out, raw)

        # 若原始输出像 JSON（以 { 或 [ 开头且不超长），尝试解析为结构化结果
        result: Any = raw
        if (
            isinstance(raw, str)
            and raw
            and raw[0] in ("{", "[")
            and len(raw) <= _TOOL_RESULT_JSON_PARSE_MAX_CHARS
        ):
            parsed_result = await run_blocking_io(_parse_tool_result_json, raw)
            if parsed_result is not None:
                result = parsed_result

        # 结果含 blocks（MCP 二进制内容块）时，上传并把 base64 替换为 URL
        if isinstance(result, dict) and "blocks" in result:
            await upload_binary_blocks(result, self._base_url)

        # 字符串结果按展示上限截断
        if isinstance(result, str):
            result = _clip_tool_result_text(result)

        # 发出工具结果事件（dict 保持结构，其余转字符串）
        await self._presenter_emit(
            self.presenter.present_tool_result(
                tool_name,
                result if isinstance(result, dict) else str(result),
                tool_call_id=tool_call_id,
                success=not is_error,
                error=error_message,
                depth=current_depth,
                agent_id=current_agent_id,
            )
        )
        # 调用结束，移除已记录的 start id
        self._started_tool_call_ids.discard(tool_call_id)

    async def _handle_tool_error(
        self,
        event: StreamEvent,
        tool_name: str,
        current_agent_id: str | None,
        current_depth: int,
    ) -> None:
        # 工具执行报错事件
        data = event.get("data", {})
        inp: dict[str, Any] = data.get("input", {})
        tool_call_id = self._get_tool_call_id(event)

        # 若该调用尚未发过 start（错误发生得很早），先补发一个 start，保证前端有卡片可挂结果
        if tool_call_id not in self._started_tool_call_ids:
            await self._presenter_emit(
                self.presenter.present_tool_start(
                    tool_name,
                    inp,
                    tool_call_id=tool_call_id,
                    depth=current_depth,
                    agent_id=current_agent_id,
                )
            )

        # 格式化错误并作为失败结果发出
        error_message = self._format_tool_error(tool_name, data.get("error"))
        await self._presenter_emit(
            self.presenter.present_tool_result(
                tool_name,
                error_message,
                tool_call_id=tool_call_id,
                success=False,
                error=error_message,
                depth=current_depth,
                agent_id=current_agent_id,
            )
        )
        self._started_tool_call_ids.discard(tool_call_id)

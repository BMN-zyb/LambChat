"""Subagent task event handling for AgentEventProcessor."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.infra.agent.events.tool_outputs import get_tool_status
from src.infra.agent.events.types import StreamEvent
from src.infra.logging import get_logger

logger = get_logger(__name__)

# 主 agent 上下文快照的分隔标记：投递给子 agent 的 description 里会附带一段
# 内部上下文，展示给用户前需从此标记处截断
_MAIN_AGENT_CONTEXT_MARKER = "\n## Main-Agent Context Snapshot"


def _strip_internal_main_context_snapshot(description: str) -> str:
    """Remove internal context handoff details from user-visible subagent cards."""
    # 无标记则原样返回
    if _MAIN_AGENT_CONTEXT_MARKER not in description:
        return description
    # 取标记前的可见部分，去掉尾部空白
    visible, _, _ = description.partition(_MAIN_AGENT_CONTEXT_MARKER)
    return visible.rstrip()


# 子 agent 事件处理 mixin：负责把 deepagents 的 task 工具调用识别为子 agent 层级，
# 并维护 checkpoint 命名空间 -> agent 的映射，用于给流式事件标注 depth/agent_id
class SubagentEventMixin:
    checkpoint_to_agent: dict[str, tuple[str, str]]
    _agent_context_cache: dict[str, tuple[str | None, int]]
    _subagent_display_names: dict[str, str]
    _subagent_avatars: dict[str, str]
    _presenter_emit: Any
    presenter: Any

    def _get_checkpoint_ns(self, metadata: dict[str, Any]) -> str:
        # 从事件 metadata 取 LangGraph 检查点命名空间（新旧字段名兼容）
        return metadata.get("langgraph_checkpoint_ns") or metadata.get("checkpoint_ns", "")

    def _get_lc_source(self, metadata: dict[str, Any]) -> str | None:
        # 取 LangChain 流来源标记（如 summarization），字段名兼容
        return metadata.get("lc_source") or metadata.get("source")

    def _get_agent_context(self, checkpoint_ns: str) -> tuple[str | None, int]:
        # 由命名空间推断 (agent_id, depth)。命名空间用 "|" 分层，无 "|" 视为顶层
        if not checkpoint_ns or "|" not in checkpoint_ns:
            return None, 0

        # 命中解析缓存直接返回，避免重复字符串处理
        cached = self._agent_context_cache.get(checkpoint_ns)
        if cached is not None:
            return cached

        # 取第一段作为定位键，去 checkpoint_to_agent 里找对应子 agent
        first_segment, _, _ = checkpoint_ns.partition("|")
        agent_info = self.checkpoint_to_agent.get(first_segment)
        if agent_info:
            # 找到即为子 agent，depth 记为 1
            agent_id = agent_info[0]
            result: tuple[str | None, int] = (agent_id, 1)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Found subagent: segment=%s, agent_id=%s",
                    first_segment[:30],
                    agent_id,
                )
            self._agent_context_cache[checkpoint_ns] = result
            return result

        # 未找到映射：agent_id 未知但仍视为深度 1，并缓存该结论
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Subagent not found: segment=%s, known=%s",
                first_segment[:30],
                list(self.checkpoint_to_agent.keys())[:3],
            )
        result = (None, 1)
        self._agent_context_cache[checkpoint_ns] = result
        return result

    async def _handle_task_start(self, event: StreamEvent) -> None:
        # task 工具启动 = 一个子 agent 开始运行
        data = event.get("data", {})
        inp: dict[str, Any] = data.get("input", {})

        # 从输入里取子 agent 类型与任务描述（做好非 dict 的兜底）
        subagent_type = inp.get("subagent_type", "unknown") if isinstance(inp, dict) else "unknown"
        description = inp.get("description", "") if isinstance(inp, dict) else ""
        # 剥离内部上下文快照，只展示用户可见的任务描述
        description = _strip_internal_main_context_snapshot(description)
        # 解析展示名与头像（无配置则回退到类型名 / None）
        subagent_display_name = self._subagent_display_names.get(subagent_type, subagent_type)
        subagent_avatar = self._subagent_avatars.get(subagent_type)
        run_id = event.get("run_id", uuid.uuid4().hex)

        # 用命名空间末段的 uuid 生成稳定的实例 id（同类型子 agent 可并发多个）
        metadata = event.get("metadata", {})
        checkpoint_ns = self._get_checkpoint_ns(metadata)
        checkpoint_uuid = checkpoint_ns.rpartition(":")[2] if checkpoint_ns else run_id
        instance_id = f"{subagent_type}_{checkpoint_uuid}"

        # 计算层级深度：首段已知(即父本身是子 agent) -> 2；否则按 "|" 数量+1；无 "|" -> 1
        if "|" in checkpoint_ns:
            first_seg, _, _ = checkpoint_ns.partition("|")
            current_depth = (
                2 if first_seg in self.checkpoint_to_agent else checkpoint_ns.count("|") + 1
            )
        else:
            current_depth = 1

        # 同一命名空间重复登记时给出提示（正常情况下不应发生）
        if checkpoint_ns in self.checkpoint_to_agent:
            logger.debug("Overwriting existing checkpoint_to_agent entry: %s", checkpoint_ns[:60])

        # 登记映射并清空解析缓存（映射变了，旧缓存作废）
        self.checkpoint_to_agent[checkpoint_ns] = (instance_id, subagent_type)
        self._agent_context_cache.clear()

        logger.info(
            "[Subagent] Task started: id=%s, ns=%s, depth=%d, total=%d",
            instance_id,
            checkpoint_ns,
            current_depth,
            len(self.checkpoint_to_agent),
        )

        # 向前端发出 agent:call 事件（子 agent 卡片）
        await self._presenter_emit(
            self.presenter.present_agent_call(
                agent_id=instance_id,
                agent_name=subagent_display_name,
                input_message=description,
                depth=current_depth,
                agent_avatar=subagent_avatar,
            )
        )

    def _resolve_agent_info(self, event: StreamEvent) -> tuple[str, int]:
        # 子 agent 结束时反查其 (instance_id, depth)，并从映射中弹出该命名空间
        checkpoint_ns = self._get_checkpoint_ns(event.get("metadata", {}))
        agent_info = self.checkpoint_to_agent.pop(checkpoint_ns, None)
        # 映射变更，解析缓存作废
        self._agent_context_cache.clear()
        if agent_info:
            return agent_info[0], checkpoint_ns.count("|") + 1 if checkpoint_ns else 1
        return "unknown", 1

    async def _handle_task_end(self, event: StreamEvent) -> None:
        # 子 agent 正常结束：提取结果文本并判断成功/失败
        data = event.get("data", {})
        out = data.get("output")
        result_text = str(out) if out is not None else ""

        # 优先取子 agent 返回消息里的原始内容（中间件可能改写过展示内容）
        out_update = getattr(out, "update", None) if out is not None else None
        if isinstance(out_update, dict):
            messages = out_update.get("messages", [])
            if messages:
                message = messages[0]
                # lambchat_original_content：保留改写前的原文用于展示
                original_content = getattr(message, "additional_kwargs", {}).get(
                    "lambchat_original_content"
                )
                if original_content is not None:
                    result_text = (
                        original_content
                        if isinstance(original_content, str)
                        else str(original_content)
                    )
                else:
                    result_text = getattr(message, "content", result_text)

        # 从输出中检测错误：先用工具状态，再看 dict 里的 error/status 字段
        error_message = None
        tool_status = get_tool_status(out)
        if tool_status == "error":
            error_message = str(out) if out else "Tool execution failed"
        elif isinstance(out, dict) and (out.get("error") or out.get("status") == "error"):
            error_message = out.get("error") or out.get("message") or str(out)

        # 反查该子 agent 的实例 id 与深度
        current_instance_id, current_depth = self._resolve_agent_info(event)

        logger.debug(
            "Subagent ended: id=%s, depth=%d, error=%s",
            current_instance_id,
            current_depth,
            error_message is not None,
        )

        # 发出 agent:result 事件，success 取决于是否检测到错误
        await self._presenter_emit(
            self.presenter.present_agent_result(
                agent_id=current_instance_id,
                result=result_text,
                success=error_message is None,
                depth=current_depth,
                error=error_message,
            )
        )

    async def _handle_task_error(self, event: StreamEvent) -> None:
        # 子 agent 异常结束：直接以错误信息构造失败结果
        error = event.get("data", {}).get("error")
        error_message = str(error) if error is not None else "Unknown error"
        current_instance_id, current_depth = self._resolve_agent_info(event)

        logger.warning(
            "Subagent error: id=%s, depth=%d, error=%s",
            current_instance_id,
            current_depth,
            error_message[:200],
        )

        await self._presenter_emit(
            self.presenter.present_agent_result(
                agent_id=current_instance_id,
                result="",
                success=False,
                depth=current_depth,
                error=error_message,
            )
        )

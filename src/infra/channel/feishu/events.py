"""Feishu task event processing."""

import json
from typing import Any

from src.infra.async_utils import run_blocking_io
from src.infra.channel.feishu.approval import (
    EVENT_APPROVAL_REQUIRED,
    _extract_approval_result_status,
    _get_existing_approval_status,
)
from src.infra.channel.feishu.collector import FeishuResponseCollector
from src.infra.channel.feishu.handler_helpers import (
    EVENT_MESSAGE_CHUNK,
    EVENT_TOOL_RESULT,
    EVENT_TOOL_START,
    _extract_tool_media_files,
)
from src.infra.logging import get_logger

logger = get_logger(__name__)


async def _process_events(
    collector: FeishuResponseCollector,
    session_id: str,
    run_id: str,
    show_tools: bool,
) -> None:
    """处理事件流并收集响应"""
    # 从会话的双写器读取 agent 运行事件流（经 Redis 转发），逐条驱动收集器：
    # 文本 chunk 走流式卡片、工具事件记录、审批事件走 HITL 卡片、文件事件上传发送。
    from src.infra.session.dual_writer import get_dual_writer

    dual_writer = get_dual_writer()
    # 缓存本轮已出现的待审批项，便于收到工具结果时回填审批上下文以更新卡片。
    pending_approvals: dict[str, dict[str, Any]] = {}

    try:
        async for event in dual_writer.read_from_redis(session_id, run_id):
            event_type = event.get("event_type", "")
            data = event.get("data", {})

            # 文本增量：追加到流式卡片（打字机效果）。
            if event_type == EVENT_MESSAGE_CHUNK:
                chunk = data.get("content", "")
                if chunk:
                    await collector.append_stream_chunk(chunk)

            # 工具开始：仅在需要展示工具时记录工具名。
            elif event_type == EVENT_TOOL_START and show_tools:
                tool_name = data.get("tool", "")
                if tool_name:
                    collector.add_tool(tool_name)

            # 需要人工审批：去重后暂存审批数据，先在卡片上显示"等待确认"，再发出审批卡片。
            elif event_type == EVENT_APPROVAL_REQUIRED:
                approval_id = str(data.get("id") or "")
                logger.info(
                    "[HITL] approval_id=%s Received approval_required event",
                    approval_id,
                )
                if approval_id:
                    # 同一审批已发过卡片则跳过（可能收到重复事件）。
                    if collector.has_sent_approval_card(approval_id):
                        logger.info(
                            "[HITL] approval_id=%s Skip duplicate approval_required event",
                            approval_id,
                        )
                        continue
                    pending_approvals[approval_id] = data
                await collector.set_waiting_for_approval(data)
                sent = await collector.send_approval_card(data)
                if sent:
                    logger.info("[HITL] approval_id=%s Sent approval card", approval_id)
                else:
                    logger.warning(
                        "[HITL] approval_id=%s Failed to send approval card", approval_id
                    )

            # 工具结果：可能携带审批终态，也可能是 reveal_file/媒体文件需要发送。
            elif event_type == EVENT_TOOL_RESULT:
                tool_name = data.get("tool", "")
                logger.debug(f"[Feishu] tool:result event: tool={tool_name}")
                result = data.get("result", "")
                result_approval_id, approval_status = _extract_approval_result_status(result)
                if result_approval_id:
                    # A tool result is only emitted after the approval was
                    # resolved (the tool already awaited the user response), so
                    # "pending" here means the tool forgot to carry its outcome.
                    # Never revert an already-handled card to pending — refresh
                    # the authoritative status from the approval record instead.
                    # 工具结果一定在审批被处理之后才产生，因此这里若还是 pending，
                    # 说明结果里漏带了终态；此时改为从审批记录读取权威状态，
                    # 绝不把已处理的卡片回退成 pending。
                    if approval_status == "pending":
                        approval_status = await _get_existing_approval_status(result_approval_id)
                    logger.info(
                        "[HITL] approval_id=%s Tool result received, finalizing card status=%s",
                        result_approval_id,
                        approval_status,
                    )
                    # 清除"等待确认"提示，并把审批卡片刷新为终态（通过/拒绝等）。
                    await collector.clear_waiting_for_approval()
                    approval = pending_approvals.get(result_approval_id) or {
                        "id": result_approval_id,
                        "message": "审批请求",
                        "type": "confirm",
                    }
                    await collector.update_approval_card(
                        result_approval_id,
                        approval,
                        status=approval_status,
                    )

                # reveal_file 工具：结果里带 key/name 的文件，加入待发送并立即上传发送。
                if tool_name == "reveal_file":
                    logger.info(f"[Feishu] reveal_file result type={type(result).__name__}")
                    if isinstance(result, str) and result:
                        try:
                            file_info = await run_blocking_io(json.loads, result)
                            if (
                                isinstance(file_info, dict)
                                and "key" in file_info
                                and "name" in file_info
                            ):
                                collector.add_file_to_reveal(file_info)
                                await collector.upload_and_send_files()
                                logger.info(
                                    f"[Feishu] Added file to reveal: {file_info.get('name')}"
                                )
                        except json.JSONDecodeError as e:
                            logger.warning(f"[Feishu] Failed to parse reveal_file result: {e}")
                    elif isinstance(result, dict):
                        if "key" in result and "name" in result:
                            collector.add_file_to_reveal(result)
                            await collector.upload_and_send_files()
                            logger.info(
                                f"[Feishu] Added file to reveal (dict): {result.get('name')}"
                            )

                # 其它工具产出的媒体文件（图片/附件等）也一并抽取并发送。
                for file_info in _extract_tool_media_files(result):
                    collector.add_file_to_reveal(file_info)
                    await collector.upload_and_send_files()
                    logger.info(
                        "[Feishu] Added tool media file to reveal: %s",
                        file_info.get("name"),
                    )

            # 运行结束/完成/出错：跳出事件循环。
            elif event_type in ("done", "complete", "error"):
                break

        logger.info(f"[Feishu] Event processing completed for session={session_id}")

    except Exception as e:
        logger.error(f"[Feishu] Event processing error: {e}", exc_info=True)

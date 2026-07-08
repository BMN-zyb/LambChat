"""Feishu human approval card handling."""

import json
from typing import Any

from src.infra.channel.feishu.manager import FeishuChannelManager
from src.infra.logging import get_logger

logger = get_logger(__name__)

# 审批相关事件名/卡片按钮动作标识；审批动作去重锁的 TTL 与进程内本地锁集合。
EVENT_APPROVAL_REQUIRED = "approval_required"
FEISHU_APPROVAL_ACTION = "lambchat.approval"
_FEISHU_APPROVAL_ACTION_LOCK_TTL_SECONDS = 30
_feishu_approval_action_locks: set[str] = set()


def _approval_status_text(status: str) -> str:
    # 审批状态英文枚举到中文展示文案的映射（未知状态原样返回）。
    return {
        "pending": "等待确认",
        "processing": "正在处理",
        "approved": "已确认",
        "rejected": "已拒绝",
        "error": "处理失败",
    }.get(status, status)


def _approval_message_preview(message: str, *, max_chars: int = 1600) -> str:
    # 审批正文预览：超长则截断并提示去详情页查看，避免卡片过大。
    message = message.strip()
    if len(message) <= max_chars:
        return message
    return f"{message[:max_chars].rstrip()}\n\n...（内容较长，请打开详情页查看完整信息）"


def _coerce_json_dict(value: Any) -> dict[str, Any] | None:
    # 把可能是 dict 或 JSON 字符串的值统一转成 dict；无法解析则返回 None。
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _extract_approval_result_status(result: Any) -> tuple[str | None, str]:
    # 从工具结果中解析 (approval_id, 状态)：兼容 status 字段与 approved 布尔字段，
    # 无法识别时状态回落为 pending。
    data = _coerce_json_dict(result)
    if not data:
        return None, "pending"
    approval_id = data.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return None, "pending"
    status = str(data.get("status") or "")
    if status == "approved" or data.get("approved") is True:
        return approval_id, "approved"
    if status == "rejected" or data.get("approved") is False:
        return approval_id, "rejected"
    if status == "timeout":
        return approval_id, "error"
    return approval_id, "pending"


def _approval_status_from_record(approval: Any, response: Any = None) -> str:
    # 依据审批记录/响应推断权威状态：记录已是终态直接用，否则看响应的 approved 布尔。
    status = str(getattr(approval, "status", "") or "")
    if status in {"approved", "rejected"}:
        return status
    approved = getattr(response, "approved", None)
    if approved is True:
        return "approved"
    if approved is False:
        return "rejected"
    return "error"


async def _get_existing_approval_status(approval_id: str) -> str:
    # 从审批存储读取记录与响应，返回其权威状态（用于卡片状态校准）。
    from src.infra.storage.mongodb import get_approval_storage

    storage = get_approval_storage()
    approval = await storage.get(approval_id)
    response = await storage.get_response(approval_id)
    return _approval_status_from_record(approval, response)


async def _respond_to_human_approval(approval_id: str, *, approved: bool) -> None:
    # 复用 HTTP 层的审批响应逻辑，把"通过/拒绝"结果写回，唤醒被暂停的 agent 流程。
    from src.api.routes.human import respond_to_approval

    await respond_to_approval(
        approval_id,
        approved=approved,
        response="{}",
    )


async def _claim_feishu_approval_action(approval_id: str) -> bool:
    # 抢占审批动作处理权，防止同一审批被重复处理（多次点击/多实例）。
    # 先查进程内本地锁快速拦截，再用 Redis SET NX 做跨实例去重。
    if approval_id in _feishu_approval_action_locks:
        return False

    try:
        from src.infra.storage.redis import get_redis_client

        redis_client = get_redis_client()
        claimed = await redis_client.set(
            f"feishu:approval_action:{approval_id}",
            "1",
            nx=True,
            ex=_FEISHU_APPROVAL_ACTION_LOCK_TTL_SECONDS,
        )
        if not claimed:
            return False
    except Exception as e:
        # Redis 不可用时降级为仅本地去重。
        logger.debug("[Feishu] Redis approval action dedupe unavailable: %s", e)

    _feishu_approval_action_locks.add(approval_id)
    return True


def _release_feishu_approval_action(approval_id: str) -> None:
    # 释放进程内本地锁（Redis 锁靠 TTL 自动过期）。
    _feishu_approval_action_locks.discard(approval_id)


async def _build_approval_card_content(
    approval: dict[str, Any],
    *,
    session_url: str | None,
    status: str,
) -> str:
    # 构建审批卡片 JSON：标题+正文+状态行；仅在 pending 且为 confirm 类型时展示"确认/拒绝"按钮，
    # 有 session_url 时附"打开详情页"按钮。按钮 value 携带 action 标识与 approval_id 供回调识别。
    approval_id = str(approval.get("id") or "")
    approval_type = str(approval.get("type") or "form")
    message = _approval_message_preview(str(approval.get("message") or "需要用户确认"))
    pending = status == "pending"

    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**需要用户确认**\n\n{message}",
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": f"状态：**{_approval_status_text(status)}**",
        },
    ]

    actions: list[dict[str, Any]] = []
    if pending and approval_type == "confirm":
        actions.extend(
            [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "确认"},
                    "type": "primary",
                    "value": {
                        "action": FEISHU_APPROVAL_ACTION,
                        "approval_id": approval_id,
                        "approved": True,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "拒绝"},
                    "type": "danger",
                    "value": {
                        "action": FEISHU_APPROVAL_ACTION,
                        "approval_id": approval_id,
                        "approved": False,
                    },
                },
            ]
        )

    if session_url:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开详情页"},
                "type": "default",
                "url": session_url,
            }
        )

    if actions:
        elements.append({"tag": "action", "actions": actions})

    card = {"config": {"wide_screen_mode": True}, "elements": elements}
    return json.dumps(card, ensure_ascii=False)


def build_feishu_approval_processing_card_data(approval_id: str) -> dict[str, Any]:
    """Build card JSON returned synchronously to disable clicked approval buttons."""
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "markdown",
                "content": "**需要用户确认**\n\n已收到确认操作，正在处理审批请求。",
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": f"状态：**{_approval_status_text('processing')}**",
            },
        ],
    }
    if approval_id:
        card["elements"].append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"审批 ID：{approval_id}",
                    }
                ],
            }
        )
    return card


async def handle_feishu_approval_action(
    *,
    value: Any,
    message_id: str | None = None,
    user_id: str | None = None,
    instance_id: str | None = None,
    manager: FeishuChannelManager | None = None,
) -> bool:
    """Handle a Feishu card button click for LambChat approvals."""
    # 校验回调是否为本系统审批动作并取出 approval_id / approved。
    action_value = _coerce_json_dict(value)
    if not action_value or action_value.get("action") != FEISHU_APPROVAL_ACTION:
        return False

    approval_id = str(action_value.get("approval_id") or "")
    if not approval_id:
        return False
    approved = bool(action_value.get("approved"))
    logger.info(
        "[HITL] approval_id=%s Card action received approved=%s",
        approval_id,
        approved,
    )
    # 抢占处理权：抢不到说明该审批正被处理（重复点击/并发），此时只刷新卡片状态后返回。
    claimed = await _claim_feishu_approval_action(approval_id)
    if not claimed:
        status = await _get_existing_approval_status(approval_id)
        logger.info(
            "[HITL] approval_id=%s Action already in progress, refreshing card status=%s",
            approval_id,
            status,
        )
        if manager and user_id and message_id and status != "error":
            await _patch_feishu_approval_card(
                manager=manager,
                user_id=user_id,
                instance_id=instance_id,
                message_id=message_id,
                approval_id=approval_id,
                status=status,
            )
        return True

    try:
        from fastapi import HTTPException

        # 提交审批结果；成功则状态取决于用户选择。
        await _respond_to_human_approval(approval_id, approved=approved)
        status = "approved" if approved else "rejected"
        logger.info(
            "[HITL] approval_id=%s Approval action accepted status=%s",
            approval_id,
            status,
        )
    except HTTPException as e:
        # 400 通常表示该审批已被处理过：改为读取权威状态，而非报错。
        if e.status_code == 400:
            status = await _get_existing_approval_status(approval_id)
            logger.info(
                "[HITL] approval_id=%s Approval action already handled status=%s",
                approval_id,
                status,
            )
        else:
            status = "error"
            logger.warning(
                "[HITL] approval_id=%s Failed to respond to approval from card action "
                "status_code=%s detail=%s",
                approval_id,
                e.status_code,
                e.detail,
            )
    except Exception as e:
        status = "error"
        logger.warning(
            "[HITL] approval_id=%s Failed to respond to approval from card action error=%s",
            approval_id,
            e,
        )

    # 把点击过的卡片更新为最终状态（通过/拒绝/失败）。
    if manager and user_id and message_id:
        await _patch_feishu_approval_card(
            manager=manager,
            user_id=user_id,
            instance_id=instance_id,
            message_id=message_id,
            approval_id=approval_id,
            status=status,
        )

    # 处理完成后释放本地锁。
    _release_feishu_approval_action(approval_id)

    return True


async def _patch_feishu_approval_card(
    *,
    manager: FeishuChannelManager,
    user_id: str,
    instance_id: str | None,
    message_id: str,
    approval_id: str,
    status: str,
) -> None:
    # 把指定消息 ID 的审批卡片就地更新为给定状态（失败仅记 debug，不影响主流程）。
    logger.info(
        "[HITL] approval_id=%s Patching clicked card to status=%s message_id=%s",
        approval_id,
        status,
        message_id,
    )
    try:
        client = manager._find_channel(user_id, instance_id)
        if client:
            content = await _build_approval_card_content(
                {
                    "id": approval_id,
                    "message": "审批请求已处理，任务将继续执行。",
                    "type": "confirm",
                },
                session_url=None,
                status=status,
            )
            await client.patch_card_message(message_id, content)
    except Exception as e:
        logger.debug(
            "[HITL] approval_id=%s Failed to patch clicked approval card: %s",
            approval_id,
            e,
        )

"""Feishu reaction, text, card, and message patch operations."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 本模块是 FeishuChannel 的"消息收发 / 更新"能力 Mixin（FeishuMessageSenderMixin），
# 覆盖：表情回应(reaction) 的增删、纯文本消息发送、交互式卡片消息发送，以及对已发
# 消息的整体更新(update) / 局部更新(patch)。
# 统一模式：每个操作先有一个 `*_sync` 同步实现（直接调用阻塞式 lark SDK），再由
# `async` 方法通过 run_blocking_io 丢到线程池执行，避免阻塞事件循环。
# 发送卡片时支持"回复引用"：优先用回复接口，遇到可回退错误码（原消息不可回复等）
# 时自动退化为直接发送，保证消息仍能送达。
# 关键依赖：lark_oapi.api.im.v1、run_blocking_io、_resolve_receive_id、
# _REPLY_FALLBACK_ERROR_CODES。
# ============================================================================

import json
from typing import Any

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger

logger = get_logger(__name__)


async def _json_dumps_text_body(content: str) -> str:
    # 把纯文本包成飞书文本消息体 {"text": ...}；序列化放线程池且保留中文。
    return await run_blocking_io(json.dumps, {"text": content}, ensure_ascii=False)


class FeishuMessageSenderMixin:
    """Mixin providing message send/update and reaction operations."""

    # 本 mixin 的模式：每个操作有一个 *_sync 助手（直接调用同步的 lark SDK），
    # 再由 async 包装方法通过 run_blocking_io 放到线程池执行，避免阻塞事件循环。
    _client: Any
    _resolve_receive_id: Any
    _REPLY_FALLBACK_ERROR_CODES: set[int]

    # 同步添加表情回应：用 emoji_type 构造 Emoji 请求体调用 reaction.create，
    # 成功返回 reaction_id（后续可据此删除该回应），失败返回 None。
    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> str | None:
        """Sync helper for adding reaction."""
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
                return None
            data = response.data
            return data.reaction_id if data else None
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")
            return None

    # 异步封装：默认表情为 THUMBSUP；无客户端返回 None，否则丢线程池执行。
    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> str | None:
        """Add a reaction emoji to a message."""
        if not self._client:
            return None

        return await run_blocking_io(self._add_reaction_sync, message_id, emoji_type)

    # 同步删除表情回应：按 message_id + reaction_id 调用 reaction.delete。
    def _delete_reaction_sync(self, message_id: str, reaction_id: str) -> bool:
        """Sync helper for deleting reaction."""
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        try:
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            response = self._client.im.v1.message_reaction.delete(request)
            if not response.success():
                logger.warning(
                    f"Failed to delete reaction: code={response.code}, msg={response.msg}"
                )
                return False
            return True
        except Exception as e:
            logger.warning(f"Error deleting reaction: {e}")
            return False

    # 异步封装：无客户端返回 False，否则把删除操作丢线程池执行。
    async def _delete_reaction(self, message_id: str, reaction_id: str) -> bool:
        """Delete a reaction emoji from a message."""
        if not self._client:
            return False

        return await run_blocking_io(self._delete_reaction_sync, message_id, reaction_id)

    # 同步发送一条消息（不返回消息 ID）：按 receive_id_type/receive_id 与 msg_type
    # 构造 CreateMessage 请求，成功返回 True，失败/异常返回 False。
    def _send_message_sync(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> bool:
        """Send a message synchronously."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    f"Failed to send Feishu {msg_type} message: code={response.code}, msg={response.msg}"
                )
                return False
            return True
        except Exception as e:
            logger.error(f"Error sending Feishu {msg_type} message: {e}")
            return False

    # 发送纯文本消息：解析 chat_id 得到接收方类型/ID，把文本包成 {"text": ...} 消息体，
    # 再丢线程池同步发送。**kwargs 保留以兼容 BaseChannel 的统一发送接口签名。
    async def send_message(self, chat_id: str, content: str, **kwargs: Any) -> bool:
        """Send a text message to a chat."""
        if not self._client:
            return False

        receive_id_type, receive_id = self._resolve_receive_id(chat_id)
        text_body = await _json_dumps_text_body(content)

        return await run_blocking_io(
            self._send_message_sync, receive_id_type, receive_id, "text", text_body
        )

    # 同步发送并返回消息 ID：与 _send_message_sync 相同，但额外从响应取出 message_id
    # （供后续更新/回写该条消息使用）。
    def _send_message_with_id_sync(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> tuple[bool, str | None]:
        """Send a message synchronously and return (success, message_id)."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    f"Failed to send Feishu {msg_type} message: code={response.code}, msg={response.msg}"
                )
                return False, None
            # Return message_id (response.data is an attribute, not a method)
            data = response.data
            message_id = data.message_id if data else None
            return True, message_id
        except Exception as e:
            logger.error(f"Error sending Feishu {msg_type} message: {e}")
            return False, None

    # 发送文本并返回 (是否成功, message_id)：用于需要后续更新该条文本的场景。
    async def send_message_with_id(self, chat_id: str, content: str) -> tuple[bool, str | None]:
        """Send a text message and return (success, message_id)."""
        if not self._client:
            return False, None

        receive_id_type, receive_id = self._resolve_receive_id(chat_id)
        text_body = await _json_dumps_text_body(content)

        return await run_blocking_io(
            self._send_message_with_id_sync, receive_id_type, receive_id, "text", text_body
        )

    # 同步发送交互式卡片并返回 (是否成功, message_id)：
    # 有 reply_to_id 时走回复接口形成引用，回复失败且错误码可回退时退化为直接发送；
    # 无 reply_to_id 时直接发送。via 变量仅用于日志标注最终实际走的路径。
    def _send_card_message_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        card_content: str,
        reply_to_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Send a card message synchronously and return (success, message_id).

        Args:
            receive_id_type: Type of receive_id (chat_id, open_id, etc.)
            receive_id: The target ID
            card_content: JSON string of the card content
            reply_to_id: Optional message ID to reply to (for quote/reply)
        """
        try:
            via = "create"
            # Use ReplyMessageRequest API for replies
            # 有 reply_to_id 时优先走"回复"接口，形成引用回复；
            # 若回复失败且错误码可回退，则退化为普通"发送"接口。
            if reply_to_id:
                from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

                request = (
                    ReplyMessageRequest.builder()
                    .message_id(reply_to_id)
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .msg_type("interactive")
                        .content(card_content)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.message.reply(request)

                if not response.success():
                    logger.warning(
                        "Reply Feishu card failed: code=%s msg=%s receive_id_type=%s receive_id=%s",
                        response.code,
                        response.msg,
                        receive_id_type,
                        receive_id,
                    )
                    if response.code in self._REPLY_FALLBACK_ERROR_CODES:
                        logger.info(
                            "Falling back to create Feishu card after reply failure: "
                            "code=%s receive_id_type=%s receive_id=%s",
                            response.code,
                            receive_id_type,
                            receive_id,
                        )
                        from lark_oapi.api.im.v1 import (
                            CreateMessageRequest,
                            CreateMessageRequestBody,
                        )

                        request = (
                            CreateMessageRequest.builder()
                            .receive_id_type(receive_id_type)
                            .request_body(
                                CreateMessageRequestBody.builder()
                                .receive_id(receive_id)
                                .msg_type("interactive")
                                .content(card_content)
                                .build()
                            )
                            .build()
                        )
                        response = self._client.im.v1.message.create(request)
                        via = "reply-fallback"
                else:
                    via = "reply"
            else:
                # Use CreateMessageRequest API for new messages
                from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

                request = (
                    CreateMessageRequest.builder()
                    .receive_id_type(receive_id_type)
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(receive_id)
                        .msg_type("interactive")
                        .content(card_content)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.message.create(request)
                via = "create"

            if not response.success():
                logger.error(
                    "Failed to send Feishu card message: code=%s, msg=%s, "
                    "receive_id_type=%s, receive_id=%s",
                    response.code,
                    response.msg,
                    receive_id_type,
                    receive_id,
                )
                return False, None
            data = response.data
            message_id = data.message_id if data else None
            logger.info("[Feishu] Card delivered message_id=%s via=%s", message_id, via)
            return True, message_id
        except Exception as e:
            logger.error(f"Error sending Feishu card message: {e}")
            return False, None

    # 异步封装：无客户端返回 (False, None)，否则把同步卡片发送丢到线程池执行。
    async def _send_card_message_internal(
        self,
        receive_id_type: str,
        receive_id: str,
        card_content: str,
        reply_to_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Send a card message and return (success, message_id).

        Args:
            receive_id_type: Type of receive_id
            receive_id: The target ID
            card_content: JSON string of the card content
            reply_to_id: Optional message ID to reply to
        """
        if not self._client:
            return False, None

        return await run_blocking_io(
            self._send_card_message_sync,
            receive_id_type,
            receive_id,
            card_content,
            reply_to_id,
        )

    # 对外便捷方法：解析 chat_id 后发送卡片，只关心成功与否（丢弃 message_id）。
    async def send_card_message(
        self, chat_id: str, card_content: str, reply_to_id: str | None = None
    ) -> bool:
        """Send a card message to a chat.

        Args:
            chat_id: Chat ID or open_id
            card_content: JSON string of the card content
            reply_to_id: Optional message ID to reply to (for quote/reply)
        """
        if not self._client:
            return False

        receive_id_type, receive_id = self._resolve_receive_id(chat_id)
        success, _ = await self._send_card_message_internal(
            receive_id_type, receive_id, card_content, reply_to_id
        )
        return success

    # 同步局部更新一条消息内容：仅对卡片消息有效（patch 接口）。对非卡片消息调用会
    # 预期性失败，因此失败只记 debug、不当作错误。
    def _patch_message_sync(self, message_id: str, content: str) -> bool:
        """Patch/update a message synchronously. Only works for card messages."""
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        try:
            request = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(PatchMessageRequestBody.builder().content(content).build())
                .build()
            )
            response = self._client.im.v1.message.patch(request)
            if not response.success():
                logger.debug(
                    f"Failed to patch Feishu message (may not be a card): code={response.code}"
                )
                return False
            return True
        except Exception as e:
            logger.debug(f"Error patching Feishu message: {e}")
            return False

    # 同步更新一条文本消息内容（update 接口）：把新内容重新包成 {"text": ...} 后更新。
    def _update_text_message_sync(self, message_id: str, content: str) -> bool:
        """Update a text message using the update API."""
        from lark_oapi.api.im.v1 import UpdateMessageRequest, UpdateMessageRequestBody

        try:
            text_body = json.dumps({"text": content}, ensure_ascii=False)
            request = (
                UpdateMessageRequest.builder()
                .message_id(message_id)
                .request_body(UpdateMessageRequestBody.builder().content(text_body).build())
                .build()
            )
            response = self._client.im.v1.message.update(request)
            if not response.success():
                logger.debug(f"Failed to update Feishu text message: code={response.code}")
                return False
            return True
        except Exception as e:
            logger.debug(f"Error updating Feishu text message: {e}")
            return False

    async def patch_message(self, message_id: str, content: str) -> bool:
        """Update an existing message's content. Tries update API first, then patch."""
        if not self._client:
            return False

        # Try update API first (for text messages)
        # 先尝试 update 接口（适用于文本消息）。
        success = await run_blocking_io(self._update_text_message_sync, message_id, content)
        if success:
            return True

        # Fall back to patch API (for card messages only)
        # 文本更新失败则回退到 patch 接口（仅适用于卡片消息）。
        text_body = await _json_dumps_text_body(content)
        return await run_blocking_io(self._patch_message_sync, message_id, text_body)

    # 更新一条已发出的交互式卡片消息（直接走 patch 接口，不尝试文本 update）。
    async def patch_card_message(self, message_id: str, card_content: str) -> bool:
        """Patch/update an existing interactive card message."""
        if not self._client:
            return False
        return await run_blocking_io(self._patch_message_sync, message_id, card_content)

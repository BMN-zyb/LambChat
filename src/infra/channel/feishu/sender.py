"""Compatibility entrypoint for Feishu sender mixins."""

from collections import OrderedDict
from typing import Any

from src.infra.channel.feishu import sender_base as sender_base
from src.infra.channel.feishu.sender_base import FeishuBaseSenderMixin
from src.infra.channel.feishu.sender_files import FeishuFileSenderMixin
from src.infra.channel.feishu.sender_messages import FeishuMessageSenderMixin

httpx = sender_base.httpx


class FeishuSenderMixin(
    FeishuFileSenderMixin,
    FeishuMessageSenderMixin,
    FeishuBaseSenderMixin,
):
    """Compose Feishu send/upload/download/card operations for FeishuChannel.

    Requires the host class to provide:
        - self._client: The lark SDK client instance
        - self.config.user_id: For logging purposes
    """

    _client: Any
    _chat_mode_cache: OrderedDict

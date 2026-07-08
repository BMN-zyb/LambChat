"""Compatibility entrypoint for Feishu sender mixins."""

from collections import OrderedDict
from typing import Any

from src.infra.channel.feishu import sender_base as sender_base
from src.infra.channel.feishu.sender_base import FeishuBaseSenderMixin
from src.infra.channel.feishu.sender_files import FeishuFileSenderMixin
from src.infra.channel.feishu.sender_messages import FeishuMessageSenderMixin

# 复用 sender_base 中已导入的 httpx，保持历史导入路径 `sender.httpx` 可用。
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

    # 组合三个 mixin（文件/消息/基础）为一个统一发送能力集合，供 FeishuChannel 继承。
    # 下面两个类型注解声明宿主类须提供的属性（SDK 客户端与会话模式缓存）。
    _client: Any
    _chat_mode_cache: OrderedDict

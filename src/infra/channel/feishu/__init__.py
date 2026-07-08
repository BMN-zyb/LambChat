"""
Feishu/Lark channel module.

This module provides Feishu (Lark) integration with WebSocket long connection support.
Each user can have their own Feishu bot configuration.
"""

# 飞书渠道核心：FeishuChannel（基于 lark-oapi 的 WebSocket 长连接实现），
# FEISHU_AVAILABLE 标记 lark-oapi 依赖是否可用。
from src.infra.channel.feishu.channel import FEISHU_AVAILABLE, FeishuChannel
# 事件处理层：响应收集器、消息处理回调工厂、agent 执行入口与 handler 装配。
from src.infra.channel.feishu.handler import (
    FeishuResponseCollector,
    create_feishu_message_handler,
    execute_feishu_agent,
    setup_feishu_handler,
)
# 管理层：每用户飞书渠道管理器及其单例访问器/批量启停函数。
from src.infra.channel.feishu.manager import (
    FeishuChannelManager,
    get_feishu_channel_manager,
    start_feishu_channels,
    stop_feishu_channels,
)
# Markdown 适配器：把内部 Markdown 转成飞书卡片/富文本可渲染的格式。
from src.infra.channel.feishu.markdown import FeishuMarkdownAdapter
# 连接状态枚举：描述 WebSocket 长连接的生命周期状态。
from src.infra.channel.feishu.state import ConnectionState
# 飞书专用存储。
from src.infra.channel.feishu.storage import FeishuStorage

# 本子包对外导出的公共符号清单。
__all__ = [
    # Channel
    "FEISHU_AVAILABLE",
    "FeishuChannel",
    "ConnectionState",
    # Manager
    "FeishuChannelManager",
    "get_feishu_channel_manager",
    "start_feishu_channels",
    "stop_feishu_channels",
    # Handler
    "FeishuResponseCollector",
    "create_feishu_message_handler",
    "execute_feishu_agent",
    "setup_feishu_handler",
    # Markdown
    "FeishuMarkdownAdapter",
    # Storage
    "FeishuStorage",
]

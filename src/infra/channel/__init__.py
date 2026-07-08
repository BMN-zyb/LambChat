"""Channel infrastructure module.

Provides abstract base classes, registry, and implementations for
various chat platform integrations (Feishu, WeChat, DingTalk, etc.).
"""

# 渠道抽象基类：BaseChannel 是"单个渠道实例"的统一接口，
# UserChannelManager 是"每用户可多实例"的渠道管理器抽象基类。
from src.infra.channel.base import BaseChannel, UserChannelManager
# ChannelStorage：渠道配置/连接状态的持久化存储封装。
from src.infra.channel.channel_storage import ChannelStorage
# 飞书渠道的完整实现集合：
# - FeishuResponseCollector：收集 agent 产出并回发到飞书的收集器；
# - FeishuStorage：飞书专用存储；
# - create_feishu_message_handler：构造处理飞书来消息的回调；
# - execute_feishu_agent：在飞书上下文中执行 agent 的入口；
# - setup_feishu_handler：装配飞书事件处理器。
from src.infra.channel.feishu import (
    FeishuResponseCollector,
    FeishuStorage,
    create_feishu_message_handler,
    execute_feishu_agent,
    setup_feishu_handler,
)
# manager：渠道协调器（统一启停所有渠道类型）及其单例访问器与便捷启停函数。
from src.infra.channel.manager import (
    ChannelCoordinator,
    get_channel_coordinator,
    start_channels,
    stop_channels,
)
# registry：渠道类型注册表，负责自动发现并登记各渠道实现，供协调器统一调度。
from src.infra.channel.registry import ChannelRegistry, get_registry

# 该子包对外导出的公共符号清单：只有列在此处的名字才是稳定的对外 API。
__all__ = [
    # Base classes
    "BaseChannel",
    "UserChannelManager",
    # Registry
    "ChannelRegistry",
    "get_registry",
    # Coordinator
    "ChannelCoordinator",
    "get_channel_coordinator",
    "start_channels",
    "stop_channels",
    # Storage
    "ChannelStorage",
    "FeishuStorage",
    # Feishu Handler
    "FeishuResponseCollector",
    "create_feishu_message_handler",
    "execute_feishu_agent",
    "setup_feishu_handler",
]

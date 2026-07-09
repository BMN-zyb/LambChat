"""Base channel interface for chat platforms.

Provides abstract base class for implementing various chat platform channels
(Feishu, WeChat, DingTalk, Slack, etc.) with a unified interface.
"""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 多渠道接入的抽象基座。定义两个抽象基类，供各聊天平台（飞书 / 微信 / 钉钉 /
# Slack 等）实现后接入 LambChat 的统一消息系统：
#   - BaseChannel：单个渠道"连接实例"的统一接口——启动 / 停止 / 发送消息，并把
#     收到的消息经 _handle_message 规整后转发给上层注册的 message_handler；同时
#     通过一组类属性 + 类方法向前端暴露"渠道类型元数据"（能力、配置 schema、
#     配置表单等）。
#   - UserChannelManager：某一渠道类型下"多用户 / 多实例"的管理者，负责持有并按
#     user_id[:instance_id] 键查找渠道实例，并以类级单例在进程内共享。
# 关键依赖：ChannelType / ChannelCapability / ChannelMetadata 等 schema。
# ============================================================================

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from src.infra.logging import get_logger
from src.kernel.schemas.channel import ChannelCapability, ChannelType

# 模块级日志器：以模块名作为 logger 名，便于按渠道基础设施分类过滤日志。
logger = get_logger(__name__)


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.

    Each channel (Feishu, WeChat, DingTalk, etc.) should implement this interface
    to integrate with the LambChat message system.

    Attributes:
        channel_type: The channel type enum value.
        display_name: Human-readable name for UI display.
        description: Brief description of the channel.
        icon: Lucide icon name for UI.
    """

    # 以下四个类属性是"渠道类型元数据"，由各具体渠道子类覆盖，
    # 供前端展示与注册表识别使用（类型枚举、展示名、描述、图标）。
    channel_type: ChannelType
    display_name: str = "Base Channel"
    description: str = "Base channel implementation"
    icon: str = "message-circle"

    def __init__(self, config: Any, message_handler: Optional[Callable] = None):
        """
        Initialize the channel.

        Args:
            config: Channel-specific configuration (e.g., FeishuConfig).
            message_handler: Async callback for incoming messages.
        """
        self.config = config
        self.message_handler = message_handler
        # _running 标记渠道当前是否处于运行（已建立连接/正在监听）状态。
        self._running = False

    # 抽象方法：返回该渠道类型支持的能力列表（如收发文本/图片/文件、流式回复等），
    # 供前端展示与功能开关判断。子类必须实现。
    @classmethod
    @abstractmethod
    def get_capabilities(cls) -> list[ChannelCapability]:
        """Get the capabilities of this channel type."""
        pass

    # 抽象方法：返回渠道配置的 JSON Schema（描述配置项结构与校验规则）。子类必须实现。
    @classmethod
    @abstractmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """Get JSON schema for channel configuration."""
        pass

    # 抽象方法：返回该渠道的接入引导步骤（逐条文案），供前端展示配置向导。子类必须实现。
    @classmethod
    @abstractmethod
    def get_setup_guide(cls) -> list[str]:
        """Get setup guide steps for this channel."""
        pass

    # 可覆盖方法：返回供前端动态渲染配置表单的字段定义列表（字段名/标题/类型/是否
    # 必填/是否敏感等）。默认返回空列表，具体渠道按需覆盖。
    @classmethod
    def get_config_fields(cls) -> list[dict[str, Any]]:
        """Get configuration fields for UI rendering.

        Returns a list of field definitions that the frontend can use
        to dynamically render the configuration form.

        Each field should have:
        - name: Field name (key in config)
        - title: Human-readable label
        - type: Field type (text, password, toggle, select)
        - required: Whether the field is required
        - sensitive: Whether the field contains sensitive data
        - placeholder: Optional placeholder text
        - default: Optional default value
        - options: For select type, list of {value, label} objects
        """
        return []

    # 汇总该渠道类型的完整元数据（合并上述各方法的结果）供前端消费。此方法通用，
    # 一般无需子类覆盖。
    @classmethod
    def get_metadata(cls) -> dict[str, Any]:
        """Get full metadata for this channel type."""
        # 延迟导入避免与 schemas 之间形成循环依赖。
        from src.kernel.schemas.channel import ChannelMetadata

        # 汇总各抽象/可覆盖方法的结果，组装成前端可直接消费的元数据字典。
        return ChannelMetadata(
            channel_type=cls.channel_type,
            display_name=cls.display_name,
            description=cls.description,
            icon=cls.icon,
            capabilities=cls.get_capabilities(),
            config_schema=cls.get_config_schema(),
            setup_guide=cls.get_setup_guide(),
            config_fields=cls.get_config_fields(),
        ).model_dump()

    # 抽象方法：启动渠道并开始监听消息，成功返回 True。子类实现具体的连接/长连接逻辑。
    @abstractmethod
    async def start(self) -> bool:
        """
        Start the channel and begin listening for messages.

        Returns:
            True if started successfully, False otherwise.
        """
        pass

    # 抽象方法：停止渠道并清理资源（断开连接、取消后台任务等）。子类必须实现。
    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        pass

    # 抽象方法：通过本渠道向指定会话发送一条消息，成功返回 True；**kwargs 供各渠道
    # 传递平台专属选项。子类必须实现。
    @abstractmethod
    async def send_message(self, chat_id: str, content: str, **kwargs) -> bool:
        """
        Send a message through this channel.

        Args:
            chat_id: The target chat/conversation ID.
            content: The message content.
            **kwargs: Channel-specific options.

        Returns:
            True if sent successfully, False otherwise.
        """
        pass

    # 只读属性：当前渠道是否处于运行态。
    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self._running

    # 只读属性：本渠道归属的用户 ID（从 config 读取，缺失时回退为 "unknown"）。
    @property
    def user_id(self) -> str:
        """Get the user ID this channel belongs to."""
        return getattr(self.config, "user_id", "unknown")

    # 处理平台侧收到的入站消息：这是各渠道收到消息后的统一入口——先补全 metadata
    # （注入 instance_id 等），再以关键字参数转发给注册的 message_handler；异常只记
    # 日志、不外抛，以保证长连接稳定不中断。
    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Handle an incoming message from the chat platform.

        This method forwards the message to the registered message handler.

        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/conversation identifier.
            content: Message text content.
            metadata: Optional channel-specific metadata.
        """
        # 未注册消息处理器时无法投递，仅告警并直接返回，避免抛异常中断连接。
        if not self.message_handler:
            logger.warning(f"No message handler registered for {self.channel_type} channel")
            return

        try:
            enriched_metadata = metadata or {}
            # Include instance_id so handlers can look up per-channel config
            # 注入 instance_id，使下游处理器能按"每渠道实例"粒度查回对应配置
            # （同一用户可能配置了多个同类型渠道实例）。
            instance_id = getattr(self.config, "instance_id", None)
            if instance_id and "instance_id" not in enriched_metadata:
                enriched_metadata["instance_id"] = instance_id

            # 以关键字参数转发给统一的消息处理回调，交由上层 agent 流程处理。
            await self.message_handler(
                user_id=self.user_id,
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                metadata=enriched_metadata,
            )
        except Exception as e:
            # 处理消息出错只记录日志、不向平台侧抛出，保证长连接稳定不中断。
            logger.error(f"Error handling message on {self.channel_type}: {e}")


class UserChannelManager(ABC):
    """
    Abstract base class for managing user-specific channel instances.

    Each channel type should implement a manager that handles multiple
    user configurations and their corresponding channel instances.
    """

    channel_type: ChannelType
    config_class: type
    # 类级单例注册表：以"具体管理器类"为键缓存其唯一实例，
    # 使每种渠道类型在进程内共享同一个管理器（见 get_instance）。
    _instances: dict[type, "UserChannelManager"] = {}

    def __init__(self, message_handler: Optional[Callable] = None):
        """
        Initialize the channel manager.

        Args:
            message_handler: Async callback for incoming messages.
        """
        self.message_handler = message_handler
        # 以"用户键"映射到渠道实例；键形如 user_id 或 user_id:instance_id。
        self._channels: dict[str, BaseChannel] = {}
        self._running = False

    @classmethod
    def get_instance(cls) -> "UserChannelManager":
        """Get the singleton instance for this channel manager type."""
        # 按需惰性创建并缓存单例，保证同一管理器类全局唯一。
        if cls not in cls._instances:
            cls._instances[cls] = cls()
        return cls._instances[cls]

    @classmethod
    async def close_all_instances(cls) -> None:
        """Stop and release manager singletons created through get_instance()."""
        # 先取快照再清空缓存，避免在逐个 stop 期间受注册表变动影响。
        instances = list(cls._instances.items())
        cls._instances.clear()
        for manager_cls, manager in instances:
            try:
                await manager.stop()
            except Exception as e:
                # 单个管理器停止失败不应阻断其余管理器的清理。
                logger.warning("Error stopping %s singleton: %s", manager_cls.__name__, e)

    # 抽象方法：启动该管理器下所有用户、所有启用中的渠道连接。子类实现（可含分布式分配）。
    @abstractmethod
    async def start(self) -> None:
        """Start all enabled channels for all users."""
        pass

    # 抽象方法：停止该管理器下的所有渠道连接。子类必须实现。
    @abstractmethod
    async def stop(self) -> None:
        """Stop all channels."""
        pass

    # 抽象方法：重新加载某用户（可指定 instance_id）的渠道配置并据此重启连接。子类必须实现。
    @abstractmethod
    async def reload_user(self, user_id: str, instance_id: Optional[str] = None) -> bool:
        """Reload a user's channel configuration."""
        pass

    def get_channel(self, user_id: str, instance_id: Optional[str] = None) -> Optional[BaseChannel]:
        """Get a user's channel instance."""
        # 三级回退查找策略：
        # 1) 若指定 instance_id，优先精确匹配 "user_id:instance_id"；
        if instance_id:
            channel = self._channels.get(f"{user_id}:{instance_id}")
            if channel:
                return channel

        # 2) 退化为按裸 user_id 命中（兼容单实例场景的旧键）；
        channel = self._channels.get(user_id)
        if channel:
            return channel

        # 3) 仍未命中则扫描所有以 "user_id:" 为前缀的键，返回该用户的任一实例。
        prefix = f"{user_id}:"
        for key, channel in self._channels.items():
            if key.startswith(prefix):
                return channel

        # 全部落空时返回 None（此处 get 恒为 None，作为兜底返回）。
        return self._channels.get(user_id)

    # 判断某用户（可指定实例）的渠道是否已连接：要求实例存在且处于运行态。
    def is_connected(self, user_id: str, instance_id: Optional[str] = None) -> bool:
        """Check if a user's channel is connected."""
        # 有 instance_id 用组合键、否则用裸 user_id；两者都要求实例存在且正在运行。
        channel_key = f"{user_id}:{instance_id}" if instance_id else user_id
        channel = self._channels.get(channel_key)
        return channel is not None and channel.is_running

    # 返回当前处于运行态的渠道键列表（键可能是 user_id 或 user_id:instance_id）。
    def get_connected_users(self) -> list[str]:
        """Get list of users with connected channels."""
        # 注意：此处返回的是 _channels 的键（可能是 user_id 或 user_id:instance_id），
        # 仅统计当前处于运行态的渠道。
        return [user_id for user_id, channel in self._channels.items() if channel.is_running]

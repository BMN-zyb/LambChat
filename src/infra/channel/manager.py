"""Channel manager for coordinating all chat channels.

Provides a unified manager that coordinates multiple channel types
and their user-specific instances.
"""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 渠道协调器（ChannelCoordinator）——LambChat 渠道管理的总入口。它按渠道类型持有
# 各自的 UserChannelManager（每种类型一个），负责统一启动 / 停止、按用户重载配置、
# 转发发送消息、汇总连接状态，以及向前端暴露"可用渠道"元数据。
# 具体的连接与分布式调度交由各 UserChannelManager 实现，这里只做编排与路由：
# 启停时逐个处理、单个失败不影响其它；发送/查询按"类型 → 管理器 → 实例"两级定位。
# 通过 get_channel_coordinator() 提供进程级全局单例。
# 关键依赖：get_registry（自动发现管理器类）、UserChannelManager、ChannelType。
# ============================================================================

from __future__ import annotations

from typing import Any, Callable, Optional

from src.infra.channel.base import UserChannelManager
from src.infra.channel.registry import get_registry
from src.infra.logging import get_logger
from src.kernel.schemas.channel import ChannelType

logger = get_logger(__name__)


class ChannelCoordinator:
    """
    Coordinates all channel managers across different platform types.

    This is the main entry point for channel management in LambChat.
    It manages multiple UserChannelManager instances (one per channel type).
    """

    def __init__(self, message_handler: Optional[Callable] = None):
        """
        Initialize the channel coordinator.

        Args:
            message_handler: Async callback for incoming messages from all channels.
        """
        self.message_handler = message_handler
        # 按渠道类型持有各自的 UserChannelManager（每种类型一个）。
        self._managers: dict[ChannelType, UserChannelManager] = {}
        self._running = False

    async def start(self) -> None:
        """Start all enabled channel managers."""
        # 幂等保护：已在运行则直接返回，避免重复启动。
        if self._running:
            return

        self._running = True
        # 从注册表拿到所有已自动发现的渠道管理器类，逐一实例化并启动。
        registry = get_registry()

        for channel_type, manager_cls in registry.get_all_managers().items():
            try:
                # 注册表以字符串登记，这里转回枚举；非法字符串会抛 ValueError。
                channel_type_enum = ChannelType(channel_type)
                manager = manager_cls(message_handler=self.message_handler)
                await manager.start()
                self._managers[channel_type_enum] = manager
                logger.info(f"Started {channel_type} channel manager")
            except ValueError:
                # 注册表里出现了未知/未定义的渠道类型：跳过而非中断整体启动。
                logger.debug(f"Unknown channel type: {channel_type}")
            except Exception as e:
                # 单个渠道管理器启动失败不影响其它渠道，仅记录错误。
                logger.error(f"Failed to start {channel_type} channel manager: {e}")

    async def stop(self) -> None:
        """Stop all channel managers."""
        self._running = False

        # 逐个停止各渠道管理器；单个失败不阻断其余，最后统一清空。
        for channel_type, manager in self._managers.items():
            try:
                await manager.stop()
                logger.info(f"Stopped {channel_type.value} channel manager")
            except Exception as e:
                logger.error(f"Error stopping {channel_type.value} channel manager: {e}")

        self._managers.clear()

    # 重载某用户在指定渠道类型下的配置：找不到对应管理器返回 False，否则委托其 reload_user。
    async def reload_user(self, user_id: str, channel_type: ChannelType) -> bool:
        """
        Reload a user's channel configuration.

        Args:
            user_id: The user ID.
            channel_type: The channel type to reload.

        Returns:
            True if reloaded successfully, False otherwise.
        """
        manager = self._managers.get(channel_type)
        if not manager:
            logger.warning(f"No manager for channel type: {channel_type}")
            return False

        return await manager.reload_user(user_id)

    async def send_message(
        self,
        user_id: str,
        channel_type: ChannelType,
        chat_id: str,
        content: str,
        instance_id: str | None = None,
    ) -> bool:
        """
        Send a message through a user's channel.

        Args:
            user_id: The user ID.
            channel_type: The channel type.
            chat_id: The target chat ID.
            content: The message content.
            instance_id: Optional channel instance ID.

        Returns:
            True if sent successfully, False otherwise.
        """
        # 发送前两级校验：先按类型找到管理器，再按 user_id/instance_id 找到具体渠道实例。
        manager = self._managers.get(channel_type)
        if not manager:
            logger.warning(f"No manager for channel type: {channel_type}")
            return False

        channel = manager.get_channel(user_id, instance_id)
        if not channel:
            logger.warning(f"No {channel_type} channel for user {user_id}, instance {instance_id}")
            return False

        return await channel.send_message(chat_id, content)

    # 查询某用户在指定渠道类型下是否已连接：无对应管理器视为未连接。
    def is_connected(self, user_id: str, channel_type: ChannelType) -> bool:
        """Check if a user's channel is connected."""
        manager = self._managers.get(channel_type)
        if not manager:
            return False
        return manager.is_connected(user_id)

    # 汇总所有渠道类型的状态：每种类型给出已连接用户列表与本地实例总数。
    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        status = {}
        for channel_type, manager in self._managers.items():
            status[channel_type.value] = {
                "connected_users": manager.get_connected_users(),
                "total_users": len(manager._channels),
            }
        return status

    # 返回所有可用渠道类型的元数据（取自注册表，供前端展示可选渠道列表）。
    def get_available_channels(self) -> list[dict]:
        """Get metadata for all available channel types."""
        registry = get_registry()
        return registry.get_channel_metadata()


# Global instance
# 进程级全局协调器单例（懒创建），使整个应用共享同一套渠道管理状态。
_coordinator: Optional[ChannelCoordinator] = None


# 取（或懒创建）全局渠道协调器单例。
def get_channel_coordinator() -> ChannelCoordinator:
    """Get the global channel coordinator instance."""
    global _coordinator
    if _coordinator is None:
        _coordinator = ChannelCoordinator()
    return _coordinator


async def start_channels(message_handler: Optional[Callable] = None) -> None:
    """Start the channel coordinator with all enabled channels."""
    # 便捷入口：取全局协调器、注入消息处理回调并启动所有渠道。
    coordinator = get_channel_coordinator()
    coordinator.message_handler = message_handler
    await coordinator.start()


async def stop_channels() -> None:
    """Stop the channel coordinator."""
    # 停止并释放全局协调器（置 None 以便下次可重新创建）。
    global _coordinator
    if _coordinator:
        await _coordinator.stop()
        _coordinator = None

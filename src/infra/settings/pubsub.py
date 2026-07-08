# src/infra/settings/pubsub.py
"""
Settings Pub/Sub - Redis Pub/Sub for distributed settings synchronization.

When one instance updates a setting, it publishes a message to Redis.
All other instances subscribe and refresh their local in-memory settings.

Includes:
- Auto-reconnect on connection errors (with backoff)
- Instance ID filtering to skip self-published messages
"""

import json
import uuid
from typing import Any, Dict, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.pubsub_hub import get_pubsub_hub

from ..task.constants import SETTINGS_CHANNEL

logger = get_logger(__name__)

# 本模块依赖 pubsub_hub 提供的通用 Redis Pub/Sub 能力（自动重连/退避等已在 hub 内实现），
# 这里只负责"配置变更"这一个 Redis 频道（SETTINGS_CHANNEL）的订阅、消息处理与发布触发。


class SettingsPubSub:
    """
    Redis Pub/Sub listener for settings changes.

    Lightweight version of TaskPubSub — no lock/tasks needed,
    just listens for setting change notifications and refreshes local state.
    """

    def __init__(self):
        self._subscription_token: Optional[str] = None
        self._running = False
        # Unique ID for this instance — used to skip self-published messages
        # 每个进程实例随机生成一个短 ID：本实例发布的变更消息会带上这个 ID，
        # 收到消息时若发现是自己发布的就直接跳过，避免"自己广播、自己又处理一遍"的无意义触发
        self._instance_id: str = uuid.uuid4().hex[:8]

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def start_listener(self) -> None:
        """Start listening for settings change notifications.

        Should be called during application startup, after initialize_settings().
        """
        # 已经启动则跳过，避免重复订阅同一个频道
        if self._running:
            return

        # 订阅全局 pubsub hub 上的配置变更频道，收到消息时回调 _handle_message
        hub = get_pubsub_hub()
        self._subscription_token = hub.subscribe(
            SETTINGS_CHANNEL,
            self._handle_message,
        )
        # 启动 hub 的后台监听协程；多个模块可能共享同一个 hub 实例，start() 应为幂等操作
        await hub.start()
        self._running = True
        logger.info(
            f"Settings pub/sub listening on channel: {SETTINGS_CHANNEL} (instance={self._instance_id})"
        )

    async def _handle_message(self, message: Dict[str, Any]) -> None:
        """Handle an incoming settings change message."""
        try:
            # JSON 解析丢到线程池执行，避免（理论上较大的）消息解析阻塞事件循环
            data = await run_blocking_io(json.loads, message["data"])
            key = data.get("key")
            # Skip messages published by this instance
            # 跳过本实例自己发布的消息：本实例在 set()/reset() 时已经同步刷新过内存态，无需再处理一次
            if data.get("instance_id") == self._instance_id:
                return
            if not key:
                return

            logger.info(f"[SettingsPubSub] Received setting change: {key}")

            # Refresh local in-memory settings
            # 延迟导入以避免模块级循环依赖
            from src.kernel.config import refresh_settings

            await refresh_settings(key)
            logger.info(f"[SettingsPubSub] Refreshed local setting: {key}")

        except json.JSONDecodeError:
            # 消息格式异常或处理过程中的任何错误都只记录日志，不能让回调抛出异常影响 hub 的整体调度
            logger.warning(f"[SettingsPubSub] Invalid message format: {message['data']}")
        except Exception as e:
            logger.error(f"[SettingsPubSub] Error handling message: {e}")

    async def stop_listener(self) -> None:
        """Stop the settings pub/sub listener.

        Should be called during application shutdown.
        """
        self._running = False

        if self._subscription_token:
            # 取消订阅；stop_if_idle 由 hub 自行判断是否还有其他订阅者在用，没有才真正停止监听
            hub = get_pubsub_hub()
            hub.unsubscribe(self._subscription_token)
            self._subscription_token = None
            await hub.stop_if_idle()

        logger.info("Settings pub/sub listener stopped")

    @property
    def is_running(self) -> bool:
        return self._running


# Singleton instance
# 进程级单例：整个进程只需要一个配置变更监听器
_settings_pubsub: Optional[SettingsPubSub] = None


def get_settings_pubsub() -> SettingsPubSub:
    """Get the global SettingsPubSub instance."""
    global _settings_pubsub
    if _settings_pubsub is None:
        _settings_pubsub = SettingsPubSub()
    return _settings_pubsub

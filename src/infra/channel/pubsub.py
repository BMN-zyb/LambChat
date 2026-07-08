"""Distributed channel configuration synchronization."""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.channel.registry import get_registry
from src.infra.logging import get_logger
from src.infra.pubsub_hub import get_pubsub_hub
from src.infra.storage.redis import get_redis_client
from src.kernel.schemas.channel import ChannelType

logger = get_logger(__name__)

# 用于跨实例广播"渠道配置已变更"事件的 Redis pub/sub 频道名。
CHANNEL_CONFIG_CHANNEL = "channel:config:changed"


class ChannelConfigPubSub:
    """Listen for cross-instance channel configuration changes."""

    def __init__(self) -> None:
        # 订阅句柄（取消订阅时用）与运行标记。
        self._subscription_token: Optional[str] = None
        self._running = False
        # 本进程唯一 ID：用于识别并忽略"自己发出的"广播事件，避免自我回环处理。
        self._instance_id = uuid.uuid4().hex[:8]

    # 对外暴露本实例 ID（发布事件时会带上，供其它实例区分来源）。
    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def start_listener(self) -> None:
        """启动监听：向 pub/sub 中枢订阅配置变更频道并开始收消息（幂等）。"""
        # 幂等保护：已在监听则不重复订阅。
        if self._running:
            return

        # 向进程内 pub/sub 中枢注册回调，再启动中枢的后台收听。
        hub = get_pubsub_hub()
        self._subscription_token = hub.subscribe(CHANNEL_CONFIG_CHANNEL, self._handle_message)
        await hub.start()
        self._running = True
        logger.info(
            "ChannelConfig pub/sub listening on channel: %s (instance=%s)",
            CHANNEL_CONFIG_CHANNEL,
            self._instance_id,
        )

    async def stop_listener(self) -> None:
        """停止监听：取消订阅并在中枢空闲时释放其后台资源。"""
        self._running = False
        if self._subscription_token:
            hub = get_pubsub_hub()
            hub.unsubscribe(self._subscription_token)
            self._subscription_token = None
            # 若中枢已无其它订阅者则顺带停掉，避免空转。
            await hub.stop_if_idle()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """处理收到的配置变更事件：解析载荷并在本实例热重载对应用户的渠道。"""
        try:
            # JSON 解析放到线程池，避免大载荷阻塞事件循环。
            data = await run_blocking_io(json.loads, message["data"])
            # 关键：忽略本实例自己发出的事件，防止自我回环重复重载。
            if data.get("instance_id") == self._instance_id:
                return

            user_id = data.get("user_id")
            channel_type_value = data.get("channel_type")
            instance_id = data.get("channel_instance_id")
            # 缺少关键字段的事件无法定位目标渠道，直接丢弃。
            if not user_id or not channel_type_value:
                return

            try:
                channel_type = ChannelType(channel_type_value)
            except ValueError:
                # 收到本进程不认识的渠道类型（版本/插件不一致），告警后忽略。
                logger.warning("Unknown channel type from pub/sub event: %s", channel_type_value)
                return

            # 找到该渠道类型的管理器单例，触发该用户配置的热重载（无需重启进程）。
            manager_class = get_registry().get_manager_class(channel_type)
            if not manager_class:
                return

            manager = manager_class.get_instance()
            await manager.reload_user(user_id, instance_id)
            logger.info(
                "Applied distributed channel config change: user=%s channel=%s instance=%s",
                user_id,
                channel_type_value,
                instance_id,
            )
        except Exception as e:
            # 处理单条事件失败不应中断监听循环。
            logger.error("Failed to handle distributed channel config change: %s", e)

    # 只读的运行状态标记。
    @property
    def is_running(self) -> bool:
        return self._running


# 进程级单例，保证同一进程内只有一个配置同步监听器。
_channel_config_pubsub: ChannelConfigPubSub | None = None


def get_channel_config_pubsub() -> ChannelConfigPubSub:
    """获取（或惰性创建）全局 ChannelConfigPubSub 单例。"""
    global _channel_config_pubsub
    if _channel_config_pubsub is None:
        _channel_config_pubsub = ChannelConfigPubSub()
    return _channel_config_pubsub


async def close_channel_config_pubsub() -> None:
    """Stop and release the global ChannelConfigPubSub instance if it exists."""
    # 先摘除全局引用再停止，避免停止期间又被别处取用。
    global _channel_config_pubsub
    pubsub = _channel_config_pubsub
    _channel_config_pubsub = None
    if pubsub is not None:
        await pubsub.stop_listener()


async def publish_channel_config_changed(
    *,
    user_id: str,
    channel_type: str,
    channel_instance_id: str | None,
    action: str,
) -> None:
    """向所有实例广播一次渠道配置变更事件（本地失败仅告警，不抛出）。"""
    try:
        redis_client = get_redis_client()
        pubsub = get_channel_config_pubsub()
        # 载荷带上本实例 ID，使接收方能过滤掉自己发的事件（见 _handle_message）。
        payload = await run_blocking_io(
            json.dumps,
            {
                "instance_id": pubsub.instance_id,
                "user_id": user_id,
                "channel_type": channel_type,
                "channel_instance_id": channel_instance_id,
                "action": action,
            },
        )
        # 通过 Redis 发布到约定频道，各实例的监听器据此热重载配置。
        await redis_client.publish(CHANNEL_CONFIG_CHANNEL, payload)
    except Exception as e:
        # 发布失败不影响主流程（本地配置已改），仅告警。
        logger.warning("Failed to publish channel config change: %s", e)

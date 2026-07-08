"""Distributed invalidation for process-local tool prompt caches."""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.pubsub_hub import get_pubsub_hub
from src.infra.storage.redis import get_redis_client
from src.infra.tool.env_var_prompt import invalidate_env_var_prompt_cache
from src.infra.tool.sandbox_mcp_prompt import invalidate_sandbox_mcp_prompt_cache

logger = get_logger(__name__)

# Redis 发布/订阅频道名：各实例通过此频道广播"进程内工具提示词缓存失效"事件
TOOL_CACHE_INVALIDATION_CHANNEL = "tool:cache:invalidate"


class ToolCachePubSub:
    """Synchronize prompt cache invalidation across instances."""

    def __init__(self) -> None:
        # 当前订阅句柄（用于取消订阅）；未订阅时为 None
        self._subscription_token: Optional[str] = None
        # 监听器是否已启动的运行标记，避免重复订阅
        self._running = False
        # 本实例唯一 ID：用于识别并忽略自己发出的失效消息，避免自我重复失效
        self._instance_id = uuid.uuid4().hex[:8]

    @property
    def instance_id(self) -> str:
        # 暴露本实例 ID（发布消息时写入 payload）
        return self._instance_id

    async def start_listener(self) -> None:
        # 启动监听：幂等，若已在运行则直接返回
        if self._running:
            return

        # 通过全局 pub/sub hub 订阅失效频道，收到消息交由 _handle_message 处理
        hub = get_pubsub_hub()
        self._subscription_token = hub.subscribe(
            TOOL_CACHE_INVALIDATION_CHANNEL, self._handle_message
        )
        await hub.start()
        self._running = True
        logger.info(
            "ToolCache pub/sub listening on channel: %s (instance=%s)",
            TOOL_CACHE_INVALIDATION_CHANNEL,
            self._instance_id,
        )

    async def stop_listener(self) -> None:
        # 停止监听：先置运行标记为 False，再取消订阅并在 hub 空闲时释放
        self._running = False
        if self._subscription_token:
            hub = get_pubsub_hub()
            hub.unsubscribe(self._subscription_token)
            self._subscription_token = None
            await hub.stop_if_idle()

    async def _handle_message(self, message: dict[str, Any]) -> None:
        # 处理收到的失效广播消息
        try:
            # JSON 解析放到线程池执行，避免阻塞事件循环
            data = await run_blocking_io(json.loads, message["data"])
            # 忽略自己发出的消息（本地缓存已在发布端就地失效，无需重复处理）
            if data.get("instance_id") == self._instance_id:
                return

            cache = data.get("cache")
            user_id = data.get("user_id")
            # 缺少必要字段则丢弃
            if not cache or not user_id:
                return

            # 根据 cache 键分发到对应的进程内缓存失效函数
            if cache == "env_var_prompt":
                invalidate_env_var_prompt_cache(user_id)
            elif cache == "sandbox_mcp_prompt":
                invalidate_sandbox_mcp_prompt_cache(user_id)
            else:
                # 未知缓存键：记录并忽略，保持向前兼容
                logger.debug("Ignoring unknown tool cache invalidation key: %s", cache)
                return

            logger.debug("Applied distributed tool cache invalidation: %s user=%s", cache, user_id)
        except Exception as e:
            # 失效处理失败不应中断监听循环，仅记录错误
            logger.error("Failed to handle distributed tool cache invalidation: %s", e)

    @property
    def is_running(self) -> bool:
        # 返回监听器当前是否处于运行状态
        return self._running


# 模块级单例：整个进程共享一个 ToolCachePubSub 实例
_tool_cache_pubsub: ToolCachePubSub | None = None


def get_tool_cache_pubsub() -> ToolCachePubSub:
    # 惰性创建并返回全局单例
    global _tool_cache_pubsub
    if _tool_cache_pubsub is None:
        _tool_cache_pubsub = ToolCachePubSub()
    return _tool_cache_pubsub


async def close_tool_cache_pubsub() -> None:
    """Stop and release the tool cache pub/sub singleton without creating it."""
    # 关闭并释放单例；注意：不会因关闭而意外创建新实例（先取出再置空）
    global _tool_cache_pubsub
    pubsub = _tool_cache_pubsub
    _tool_cache_pubsub = None
    if pubsub is not None:
        await pubsub.stop_listener()


async def publish_tool_cache_invalidation(cache: str, *, user_id: str | None = None) -> None:
    # 向所有实例广播一条缓存失效消息，携带本实例 ID 以便接收端过滤自身
    try:
        redis_client = get_redis_client()
        pubsub = get_tool_cache_pubsub()
        # 序列化同样放到线程池，避免阻塞事件循环
        payload = await run_blocking_io(
            json.dumps,
            {
                "instance_id": pubsub.instance_id,
                "cache": cache,
                "user_id": user_id,
            },
        )
        await redis_client.publish(TOOL_CACHE_INVALIDATION_CHANNEL, payload)
    except Exception as e:
        # 广播失败仅告警：本地缓存失效通常已单独完成，跨实例同步失败不致命
        logger.warning("Failed to publish tool cache invalidation: %s", e)

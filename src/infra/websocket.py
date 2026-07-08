"""
WebSocket Manager - WebSocket 连接管理器

管理 WebSocket 连接，用于实时推送任务完成通知。
支持 Redis Pub/Sub 实现分布式部署。
"""

import asyncio
import json
import uuid
from typing import Dict, Optional, Set

from fastapi import WebSocket

from src.infra.async_utils.blocking import run_blocking_io
from src.infra.logging import get_logger
from src.infra.pubsub_hub import get_pubsub_hub
from src.infra.storage.redis import create_redis_client

logger = get_logger(__name__)

# Redis key/channel design for distributed WebSocket delivery
# 分布式 WebSocket 投递的 Redis 键/频道设计：
# - WS_ROUTE_PREFIX: 路由键前缀，记录「某用户在某实例上有几条连接」，形如 ws:route:{user}:{instance}。
# - WS_ROUTE_SET_PREFIX: 路由集合前缀，记录「某用户当前挂在哪些实例上」的实例 id 集合。
# - WS_DELIVERY_CHANNEL_PREFIX: 每个实例一个投递频道 ws:deliver:{instance}，跨实例定向推送靠它。
# - WS_ROUTE_TTL_SECONDS: 路由键/集合的存活时间，需要靠刷新任务续期，实例宕机后会自动过期清理。
# - WS_ROUTE_REFRESH_INTERVAL: 路由续期间隔(应小于 TTL)。
# - WS_ROUTE_SCAN_LIMIT: 扫描/遍历实例数的上限，避免异常场景下无限放大。
# - WS_MESSAGE_MAX_BYTES: 单条消息最大字节数，超限丢弃。
WS_ROUTE_PREFIX = "ws:route"
WS_ROUTE_SET_PREFIX = "ws:routes"
WS_DELIVERY_CHANNEL_PREFIX = "ws:deliver"
WS_ROUTE_TTL_SECONDS = 60
WS_ROUTE_REFRESH_INTERVAL = 20
WS_ROUTE_SCAN_LIMIT = 100
WS_MESSAGE_MAX_BYTES = 256 * 1024


async def _json_dumps_message(message: dict) -> str:
    # 把消息序列化为 JSON(保留非 ASCII 字符)；放到线程池执行，避免大对象序列化阻塞事件循环。
    return await run_blocking_io(json.dumps, message, ensure_ascii=False)


async def _json_dumps_delivery(payload: dict) -> str:
    # 序列化跨实例投递的载荷(经 Redis 传输，无需保留可读中文，用默认 ASCII 转义即可)。
    return await run_blocking_io(json.dumps, payload)


async def _json_loads_delivery(raw_value: str) -> dict:
    # 反序列化跨实例投递载荷，同样放到线程池执行。
    return await run_blocking_io(json.loads, raw_value)


def _json_size_bytes(value: str) -> int:
    # 计算字符串按 UTF-8 编码后的字节数(用于消息大小限制判断)。
    return len(value.encode("utf-8"))


async def _json_dumps_message_limited(message: dict, *, label: str) -> str | None:
    # 序列化消息并做大小限制：超过上限则告警并返回 None(表示丢弃)，否则返回 JSON 串。
    serialized = await _json_dumps_message(message)
    size = _json_size_bytes(serialized)
    if size > WS_MESSAGE_MAX_BYTES:
        logger.warning(
            "Dropping oversized WebSocket %s message: %s bytes > %s",
            label,
            size,
            WS_MESSAGE_MAX_BYTES,
        )
        return None
    return serialized


async def _json_dumps_delivery_limited(payload: dict) -> str | None:
    # 序列化投递载荷并做大小限制，超限返回 None。
    serialized = await _json_dumps_delivery(payload)
    size = _json_size_bytes(serialized)
    if size > WS_MESSAGE_MAX_BYTES:
        logger.warning(
            "Dropping oversized WebSocket delivery payload: %s bytes > %s",
            size,
            WS_MESSAGE_MAX_BYTES,
        )
        return None
    return serialized


async def _scan_redis_keys(
    redis_client,
    pattern: str,
    *,
    count: int = 100,
    limit: int = WS_ROUTE_SCAN_LIMIT,
) -> list[str]:
    """Collect matching Redis keys with SCAN to avoid blocking Redis."""
    # 用 SCAN 游标分批遍历匹配的键，避免 KEYS 阻塞 Redis；累计到 limit 条即提前返回并告警。
    cursor: int | str = 0
    keys: list[str] = []
    while True:
        cursor, batch = await redis_client.scan(cursor=cursor, match=pattern, count=count)
        for key in batch:
            keys.append(str(key))
            if len(keys) >= limit:
                logger.warning("WebSocket route scan limit reached: %s", limit)
                return keys
        # 游标回到 0 表示遍历完成。
        if int(cursor) == 0:
            return keys


class ConnectionManager:
    """
    WebSocket 连接管理器

    管理所有活跃的 WebSocket 连接，按用户 ID 分组。
    支持 Redis Pub/Sub 实现分布式部署时的跨实例广播。
    """

    def __init__(self):
        # user_id -> Set[WebSocket]
        # 本地(本实例)连接表：用户 -> 该用户在本实例上的所有 WebSocket 连接集合。
        self._connections: Dict[str, Set[WebSocket]] = {}
        # 保护本地连接表读写的锁。
        self._lock = asyncio.Lock()
        # 本实例在 pub/sub 中枢上的订阅句柄(订阅自己的投递频道)。
        self._subscription_token: Optional[str] = None
        # 每个用户一个「路由续期」后台任务，定期给 Redis 路由键续 TTL。
        self._route_refresh_tasks: dict[str, asyncio.Task] = {}
        # 监听器运行标志。
        self._running = False
        # 本实例的唯一 id：用于构造实例专属投递频道与路由键，是分布式定向投递的关键。
        self._instance_id = uuid.uuid4().hex
        # 本实例专用的隔离 Redis 客户端(惰性创建)。
        self._redis = None

    async def connect(self, websocket: WebSocket, user_id: str, accept: bool = True) -> None:
        """用户连接 WebSocket

        Args:
            websocket: WebSocket连接
            user_id: 用户ID
            accept: 是否需要接受连接（如果已经accept过，设为False）
        """
        if accept:
            await websocket.accept()
        async with self._lock:
            if user_id not in self._connections:
                self._connections[user_id] = set()
            self._connections[user_id].add(websocket)
            connection_count = len(self._connections[user_id])
            # 该用户在本实例上的「第一条」连接：启动路由续期任务，把本实例登记为该用户的落点。
            if connection_count == 1:
                self._ensure_route_refresh_task(user_id)
        # 把「该用户在本实例的连接数」同步到 Redis 路由表，供其他实例定向投递时查询。
        await self._sync_route_registration(user_id, connection_count)
        logger.info(f"WebSocket connected: user_id={user_id}, total={connection_count}")

    async def disconnect(self, websocket: WebSocket, user_id: str) -> None:
        """用户断开 WebSocket"""
        async with self._lock:
            connection_count = 0
            if user_id in self._connections:
                self._connections[user_id].discard(websocket)
                connection_count = len(self._connections[user_id])
                # 该用户在本实例已无连接：删除桶并停掉其路由续期任务。
                if connection_count == 0:
                    del self._connections[user_id]
                    await self._stop_route_refresh_task(user_id)
        # 同步最新连接数到 Redis 路由表(0 表示从本实例注销该用户路由)。
        await self._sync_route_registration(user_id, connection_count)
        logger.info(f"WebSocket disconnected: user_id={user_id}")

    async def broadcast(self, message: dict) -> int:
        """
        向所有用户广播消息

        Args:
            message: 消息内容

        Returns:
            成功发送的连接数
        """
        all_connections = []
        async with self._lock:
            for user_id, conns in self._connections.items():
                all_connections.extend([(user_id, ws) for ws in conns])

        sent_count = 0
        disconnected = set()
        # 仅在有连接时才做序列化(空连接直接置空串)；序列化超限返回 None 则整体放弃广播。
        json_str = (
            await _json_dumps_message_limited(message, label="broadcast") if all_connections else ""
        )
        if json_str is None:
            return 0

        for user_id, ws in all_connections:
            try:
                await ws.send_text(json_str)
                sent_count += 1
            except Exception as e:
                logger.warning(f"Failed to broadcast to WebSocket: {e}")
                # 发送失败的连接先记下，稍后统一清理。
                disconnected.add((user_id, ws))

        # 清理断开的连接
        # 清理失败连接，并对因此变空的用户从 Redis 路由表注销(连接数置 0)。
        if disconnected:
            users_to_unregister = await self._cleanup_disconnected_connections(disconnected)
            for user_id in users_to_unregister:
                await self._sync_route_registration(user_id, 0)

        return sent_count

    def get_connection_count(self, user_id: str | None = None) -> int:
        """获取连接数量"""
        # 传 user_id 则返回该用户在本实例的连接数；否则返回本实例所有连接总数。
        if user_id:
            return len(self._connections.get(user_id, set()))
        return sum(len(conns) for conns in self._connections.values())

    async def start_pubsub_listener(self) -> None:
        """
        启动 Redis pub/sub 监听器，用于接收跨实例广播

        应在应用启动时调用
        """
        if self._running:
            return

        # 通过共享 pub/sub 中枢订阅「本实例专属」的投递频道，只接收发给本实例的定向消息。
        hub = get_pubsub_hub()
        self._subscription_token = hub.subscribe(
            self._delivery_channel(self._instance_id),
            self._handle_pubsub_message,
        )
        await hub.start()
        self._running = True
        logger.info(
            "WebSocket: Started listening on Redis channel: %s",
            self._delivery_channel(self._instance_id),
        )

    async def stop_pubsub_listener(self) -> None:
        """
        停止 Redis pub/sub 监听器

        应在应用关闭时调用
        """
        self._running = False
        # 关闭前的收尾：停掉所有路由续期任务、从 Redis 注销本实例的全部路由、关闭 Redis 客户端。
        await self._cancel_all_route_refresh_tasks()
        await self._remove_all_route_registrations()
        await self._close_redis()

        # 退订本实例频道；若中枢已无其他订阅则顺带停止中枢。
        if self._subscription_token:
            hub = get_pubsub_hub()
            hub.unsubscribe(self._subscription_token)
            self._subscription_token = None
            await hub.stop_if_idle()

        logger.info("WebSocket pub/sub listener stopped")

    async def _handle_pubsub_message(self, message: dict) -> None:
        # pub/sub 中枢的回调：解析 Redis 传来的投递载荷并转交本地发送；解析失败/异常只记录不外抛。
        try:
            data = await _json_loads_delivery(message["data"])
            await self._handle_broadcast_message(data)
        except json.JSONDecodeError:
            logger.warning(f"Invalid WebSocket broadcast message: {message['data']}")
        except Exception as e:
            logger.error(f"Error processing WebSocket broadcast: {e}")

    async def _handle_broadcast_message(self, data: dict) -> int:
        """Handle a WebSocket broadcast payload received from Redis."""
        # 从投递载荷中取出目标用户与消息体，缺失任一则忽略；随后仅在本实例向该用户发送。
        user_id = data.get("user_id")
        msg_content = data.get("message")
        if not user_id or not msg_content:
            return 0

        return await self._send_to_user_local(user_id, msg_content)

    async def _send_to_user_local(self, user_id: str, message: dict) -> int:
        """
        仅在本地实例向指定用户发送消息（不广播到 Redis）

        Args:
            user_id: 用户 ID
            message: 消息内容

        Returns:
            成功发送的连接数
        """
        if not message:
            return 0

        json_str = await _json_dumps_message_limited(message, label="local")
        if json_str is None:
            return 0
        sent_count = 0

        # 拷贝一份连接集合再逐个发送，避免发送期间持锁 / 集合被并发修改。
        async with self._lock:
            connections = self._connections.get(user_id, set()).copy()

        disconnected = set()
        for ws in connections:
            try:
                await ws.send_text(json_str)
                sent_count += 1
            except Exception as e:
                logger.warning(f"Failed to send to WebSocket: {e}")
                disconnected.add(ws)

        # 清理断开的连接
        if disconnected:
            users_to_unregister = await self._cleanup_disconnected_connections(
                {(user_id, ws) for ws in disconnected}
            )
            for target_user_id in users_to_unregister:
                await self._sync_route_registration(target_user_id, 0)

        return sent_count

    async def _cleanup_disconnected_connections(
        self,
        disconnected: set[tuple[str, WebSocket]],
    ) -> set[str]:
        """Remove disconnected sockets and fully release empty user buckets."""
        # 从本地连接表移除已断开的 socket；若某用户桶被清空则删除桶、停其续期任务，
        # 并返回这些用户以便调用方从 Redis 路由表注销。
        users_to_unregister: set[str] = set()
        async with self._lock:
            for user_id, ws in disconnected:
                connections = self._connections.get(user_id)
                if connections is None:
                    continue
                connections.discard(ws)
                if not connections:
                    del self._connections[user_id]
                    await self._stop_route_refresh_task(user_id)
                    users_to_unregister.add(user_id)
        return users_to_unregister

    async def send_to_user_with_broadcast(self, user_id: str, message: dict) -> int:
        """
        向指定用户发送消息（支持分布式定向投递）

        Args:
            user_id: 用户 ID
            message: 消息内容

        Returns:
            发布到的实例通道数量
        """
        try:
            redis_client = self._get_redis()
            # 构造投递载荷(含目标用户、消息体、来源实例 id)，并做大小限制。
            payload = await _json_dumps_delivery_limited(
                {
                    "user_id": user_id,
                    "message": message,
                    "source_instance_id": self._instance_id,
                }
            )
            if payload is None:
                return 0
            published = 0
            # 查该用户当前挂在哪些实例上(路由集合)，逐个实例做定向投递。
            instance_ids = await redis_client.smembers(self._route_set_key(user_id))
            for raw_instance_id in sorted(instance_ids)[:WS_ROUTE_SCAN_LIMIT]:
                # Redis 返回值可能是 bytes，统一转成 str。
                instance_id = (
                    raw_instance_id.decode("utf-8")
                    if isinstance(raw_instance_id, bytes)
                    else str(raw_instance_id)
                )
                route_key = self._route_key_for_instance(user_id, instance_id)
                # 路由键已过期(实例可能已下线)：把它从集合里剔除，跳过该实例。
                if await redis_client.get(route_key) is None:
                    await redis_client.srem(self._route_set_key(user_id), instance_id)
                    continue
                # 向该实例的投递频道发布消息，累加订阅者数量。
                subscriber_count = await redis_client.publish(
                    self._delivery_channel(instance_id),
                    payload,
                )
                published += int(subscriber_count or 0)

            # Fallback for edge cases where local connections exist but Redis route has
            # not been registered yet (for example after a transient Redis error).
            # 兜底：Redis 路由尚未登记但本地其实有连接(如 Redis 短暂故障后)，直接走本地发送。
            if published == 0:
                return await self._send_to_user_local(user_id, message)
            return published
        except Exception as e:
            # 分布式路由任何异常都降级为本地发送，保证同实例用户仍能收到消息。
            logger.warning(f"Failed to route WebSocket message: {e}")
            return await self._send_to_user_local(user_id, message)

    @staticmethod
    def _delivery_channel(instance_id: str) -> str:
        # 某实例的投递频道名：ws:deliver:{instance_id}。
        return f"{WS_DELIVERY_CHANNEL_PREFIX}:{instance_id}"

    def _route_key(self, user_id: str) -> str:
        # 本实例上某用户的路由键：ws:route:{user_id}:{本实例 id}。
        return f"{WS_ROUTE_PREFIX}:{user_id}:{self._instance_id}"

    @staticmethod
    def _route_key_for_instance(user_id: str, instance_id: str) -> str:
        # 指定实例上某用户的路由键(用于跨实例查询对方是否仍在线)。
        return f"{WS_ROUTE_PREFIX}:{user_id}:{instance_id}"

    @staticmethod
    def _route_set_key(user_id: str) -> str:
        # 某用户的路由集合键：ws:routes:{user_id}，成员是该用户当前所在的实例 id 集合。
        return f"{WS_ROUTE_SET_PREFIX}:{user_id}"

    async def _sync_route_registration(self, user_id: str, connection_count: int) -> None:
        # 把「本实例上某用户的连接情况」同步到 Redis 路由表。异常仅告警，不影响本地连接。
        try:
            redis_client = self._get_redis()
            route_key = self._route_key(user_id)
            route_set_key = self._route_set_key(user_id)
            if connection_count > 0:
                # 有连接：写路由键(带 TTL)并把本实例加入该用户的实例集合，同时给集合续 TTL。
                await redis_client.set(route_key, str(connection_count), ex=WS_ROUTE_TTL_SECONDS)
                await redis_client.sadd(route_set_key, self._instance_id)
                await redis_client.expire(route_set_key, WS_ROUTE_TTL_SECONDS)
            else:
                # 无连接：删除路由键并把本实例移出集合(注销该用户在本实例的路由)。
                await redis_client.delete(route_key)
                await redis_client.srem(route_set_key, self._instance_id)
        except Exception as e:
            logger.warning("Failed to sync WebSocket route for user %s: %s", user_id, e)

    def _get_redis(self):
        # 惰性创建本实例专用的隔离连接池 Redis 客户端。
        if self._redis is None:
            self._redis = create_redis_client(isolated_pool=True)
        return self._redis

    async def _close_redis(self) -> None:
        # 关闭本实例的 Redis 客户端(关闭失败仅告警)。
        if self._redis is None:
            return
        try:
            await self._redis.aclose()
        except Exception as e:
            logger.warning("Failed to close WebSocket Redis client: %s", e)
        finally:
            self._redis = None

    def _ensure_route_refresh_task(self, user_id: str) -> None:
        # 为某用户确保存在一个后台续期任务(已存在则不重复创建)。
        if user_id in self._route_refresh_tasks:
            return

        async def _refresh_loop() -> None:
            # 周期性给该用户的 Redis 路由键续 TTL；一旦本实例已无其连接则退出循环。
            # 这样即便实例崩溃、无法续期，路由也会在 TTL 到期后自动被清理，避免消息投到死实例。
            try:
                while True:
                    await asyncio.sleep(WS_ROUTE_REFRESH_INTERVAL)
                    async with self._lock:
                        connection_count = len(self._connections.get(user_id, set()))
                    if connection_count <= 0:
                        return
                    await self._sync_route_registration(user_id, connection_count)
            except asyncio.CancelledError:
                pass
            finally:
                self._route_refresh_tasks.pop(user_id, None)

        self._route_refresh_tasks[user_id] = asyncio.create_task(_refresh_loop())

    async def _stop_route_refresh_task(self, user_id: str) -> None:
        # 取消并等待某用户的续期任务结束。
        task = self._route_refresh_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _cancel_all_route_refresh_tasks(self) -> None:
        # 停止所有用户的续期任务(关闭监听器时调用)。
        for user_id in list(self._route_refresh_tasks.keys()):
            await self._stop_route_refresh_task(user_id)

    async def _remove_all_route_registrations(self) -> None:
        # 从 Redis 注销本实例上所有用户的路由(关闭时清理，避免遗留死路由)。
        user_ids = list(self._connections.keys())
        if not user_ids:
            return
        for user_id in user_ids:
            await self._sync_route_registration(user_id, 0)


# Singleton instance
# 进程级单例：整个进程共用同一个连接管理器。
_manager: ConnectionManager | None = None


def get_connection_manager() -> ConnectionManager:
    """获取 ConnectionManager 单例"""
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager

"""
Shared Redis pub/sub hub.

Keeps a single Redis pub/sub connection per process and fan-outs messages to
channel-specific async handlers. This reduces idle connections and background
listener tasks for distributed features that only need lightweight broadcasts.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError

from src.infra.logging import get_logger

logger = get_logger(__name__)

# pub/sub 处理器签名：接收一条消息(dict)，可以是同步函数也可以是返回 awaitable 的协程函数。
PubSubHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
# 断线重连的最大退避秒数（指数退避的上限）。
_MAX_RECONNECT_DELAY = 30
# 并发执行的处理器任务数上限（用信号量限流，防止某个频道消息风暴打爆事件循环）。
_DEFAULT_MAX_HANDLER_TASKS = 128
# 单条 pub/sub 消息允许的最大字节数，超过则直接丢弃（防御异常大消息）。
_DEFAULT_MAX_MESSAGE_BYTES = 256 * 1024


def create_redis_client(*, isolated_pool: bool = False, socket_timeout: Any = None) -> Any:
    """Create Redis client lazily to avoid import cycles at module import time."""
    from src.infra.storage.redis import create_redis_client as _create_redis_client

    return _create_redis_client(
        isolated_pool=isolated_pool,
        socket_timeout=socket_timeout,
    )


def _message_data_size(data: Any) -> int:
    # 估算一条消息 data 字段的字节大小，用于超限丢弃判断。
    # bytes 直接取长度；str 按 UTF-8 编码后取长度；其他类型尝试 len()，不支持则记为 0。
    if isinstance(data, bytes):
        return len(data)
    if isinstance(data, str):
        return len(data.encode("utf-8"))
    try:
        return len(data)
    except TypeError:
        return 0


class RedisPubSubHub:
    """Multiplex Redis pub/sub channels over a single shared listener."""

    def __init__(
        self,
        *,
        max_handler_tasks: int = _DEFAULT_MAX_HANDLER_TASKS,
        max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        # 订阅表：channel -> {token -> handler}。同一频道可有多个订阅者，各自用唯一 token 标识。
        self._subscriptions: dict[str, dict[str, PubSubHandler]] = defaultdict(dict)
        # 保护 self._pubsub 读写的锁（监听循环与关闭逻辑可能并发访问）。
        self._lock = asyncio.Lock()
        # 后台监听协程；None 表示未启动。
        self._listener_task: asyncio.Task | None = None
        # 当前使用的 Redis pubsub 对象；重连/重订阅时会被替换。
        self._pubsub: Any | None = None
        # 运行标志：start 置 True，stop 置 False，监听循环据此决定是否继续。
        self._running = False
        # 就绪事件：start() 会等待它被 set，确保「已开始订阅」后再返回。
        self._ready_event: asyncio.Event | None = None
        # 记录「预期内的主动断开」的 pubsub 对象 id，用于区分主动重订阅 vs 真正的异常断线。
        self._expected_disconnects: set[int] = set()
        # 正在运行的处理器任务集合（配合信号量做背压与优雅关闭）。
        self._handler_tasks: set[asyncio.Task[None]] = set()
        # 重订阅「唤醒」任务：订阅表变化时用它去打断当前 listen() 以便重新订阅。
        self._resubscribe_task: asyncio.Task[None] | None = None
        # 并发处理器信号量：限制同时在跑的 handler 数量。
        self._handler_semaphore = asyncio.Semaphore(max(1, max_handler_tasks))
        # 单条消息最大字节数（下界保护为 1）。
        self._max_message_bytes = max(1, int(max_message_bytes))

    def subscribe(self, channel: str, handler: PubSubHandler) -> str:
        """Register a handler for a Redis channel."""
        # 生成唯一 token 作为本次订阅的句柄（unsubscribe 时按 token 精确移除）。
        token = uuid.uuid4().hex
        self._subscriptions[channel][token] = handler
        # 若监听器已在运行，需要唤醒它重新订阅，以便把新频道纳入 SUBSCRIBE。
        if self._running:
            self._schedule_resubscribe()
        return token

    def unsubscribe(self, token: str) -> None:
        """Remove a previously registered handler."""
        # 遍历所有频道找到该 token 并删除；若某频道删空则记录下来一并清理。
        empty_channels: list[str] = []
        for channel, handlers in self._subscriptions.items():
            if token in handlers:
                del handlers[token]
                if not handlers:
                    empty_channels.append(channel)
                break

        for channel in empty_channels:
            del self._subscriptions[channel]

        # 订阅表变化后同样需要唤醒监听器重新订阅（退订某频道）。
        if self._running:
            self._schedule_resubscribe()

    async def start(self) -> None:
        """Start the shared listener if it is not running already."""
        # 幂等：已在运行则直接返回。
        if self._running:
            return

        self._running = True
        self._ready_event = asyncio.Event()
        # 启动后台监听循环，并等待其发出「已就绪」信号后再返回，避免调用方错过早期消息。
        self._listener_task = asyncio.create_task(self._listener_loop())
        await self._ready_event.wait()

    async def stop(self) -> None:
        """Stop the shared listener and close the current Redis pub/sub."""
        # 先置 running=False 让监听循环尽快退出，再主动关闭当前 pubsub 打断阻塞的 listen()。
        self._running = False
        pubsub = self._pubsub
        if pubsub is not None:
            await self._close_pubsub(pubsub)

        # 取消并等待监听任务结束（吞掉 CancelledError）。
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

        # 清理重订阅唤醒任务与所有在跑的处理器任务。
        await self._cancel_resubscribe_task()
        await self._cancel_handler_tasks()

        self._listener_task = None
        self._ready_event = None

    async def stop_if_idle(self) -> None:
        """Stop the listener when no channels remain subscribed."""
        # 无任何订阅时才停止，供调用方在退订后按需回收监听器资源。
        if self.subscription_count == 0:
            await self.stop()

    @property
    def subscription_count(self) -> int:
        # 当前总订阅数 = 各频道 handler 数之和（注意：不是频道数）。
        return sum(len(handlers) for handlers in self._subscriptions.values())

    def describe_state(self) -> dict[str, Any]:
        # 返回可读的状态快照（频道数、订阅数、各频道订阅明细），用于日志与调试。
        channels = {
            channel: len(handlers) for channel, handlers in sorted(self._subscriptions.items())
        }
        return {
            "channel_count": len(channels),
            "subscription_count": sum(channels.values()),
            "channels": channels,
        }

    async def _listener_loop(self) -> None:
        # 后台监听主循环：维持一条到 Redis 的 pub/sub 连接，订阅当前所有频道并分发消息。
        # 具备：无订阅时空转等待、异常断线的指数退避重连、以及订阅表变化时的主动重订阅。
        # 指数退避的当前延迟（秒），成功建立监听后会被重置为 1。
        delay = 1

        try:
            while self._running:
                # 每轮都取一份「当前频道快照」，从而在订阅表变化后能以最新集合重新订阅。
                channels = sorted(self._subscriptions.keys())
                if not channels:
                    # 尚无任何订阅：也要先置就绪(避免 start 一直阻塞)，然后短暂休眠再轮询。
                    if self._ready_event is not None and not self._ready_event.is_set():
                        self._ready_event.set()
                    await asyncio.sleep(0.05)
                    continue

                pubsub = None
                redis_client = None
                try:
                    # 使用「隔离连接池 + 无 socket 超时」的客户端，避免长驻订阅被普通池的超时打断。
                    redis_client = create_redis_client(
                        isolated_pool=True,
                        socket_timeout=None,
                    )
                    pubsub = redis_client.pubsub()
                    # 加锁记录当前 pubsub，供 stop()/重订阅逻辑安全地主动关闭它。
                    async with self._lock:
                        self._pubsub = pubsub

                    await pubsub.subscribe(*channels)
                    snapshot = self.describe_state()
                    logger.info(
                        "Pub/sub hub listening on %s channels (%s subscriptions): %s",
                        snapshot["channel_count"],
                        snapshot["subscription_count"],
                        ", ".join(channels),
                    )
                    # 成功订阅后置就绪并重置退避延迟。
                    if self._ready_event is not None and not self._ready_event.is_set():
                        self._ready_event.set()
                    delay = 1

                    # 阻塞式消费消息流：只处理 type=="message" 的真实消息，其余(如订阅确认)跳过。
                    async for message in pubsub.listen():
                        if not self._running:
                            break
                        if message.get("type") != "message":
                            continue
                        await self._dispatch_message(message)
                except asyncio.CancelledError:
                    # 取消属于正常关闭路径，向上抛出交由外层处理。
                    raise
                except Exception as e:
                    if not self._running:
                        break
                    # 若这是我们「主动关闭 pubsub」触发的预期断开(为了重订阅)，则立即无退避重来。
                    if pubsub is not None and self._is_expected_disconnect(pubsub, e):
                        delay = 1
                        logger.debug("Pub/sub hub restarting listener after resubscribe")
                        continue
                    # 真正的异常断线：置就绪(避免卡住 start)、记录错误、按指数退避后重试。
                    if self._ready_event is not None and not self._ready_event.is_set():
                        self._ready_event.set()
                    logger.error("Pub/sub hub listener error: %s", e)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _MAX_RECONNECT_DELAY)
                finally:
                    # 无论何种退出路径，都要解绑并关闭本轮的 pubsub 与 redis 客户端，防止连接泄漏。
                    await self._detach_pubsub(pubsub)
                    await self._close_redis_client(redis_client)
        except asyncio.CancelledError:
            logger.info("Pub/sub hub listener cancelled")
        finally:
            self._running = False
            logger.info("Pub/sub hub listener stopped")

    async def _dispatch_message(self, message: dict[str, Any]) -> None:
        # 把一条 Redis 消息分发给该频道注册的所有 handler：为每个 handler 起一个受信号量限流的任务。
        channel = message.get("channel")
        if not isinstance(channel, str):
            return
        # 超大消息直接丢弃并告警，防止异常负载拖垮处理链路。
        data_size = _message_data_size(message.get("data"))
        if data_size > self._max_message_bytes:
            logger.warning(
                "Dropping oversized pub/sub message on channel %s: %s bytes > %s",
                channel,
                data_size,
                self._max_message_bytes,
            )
            return

        # 取当前 handler 快照后逐个派发；每派发一个先 acquire 信号量做背压(超出上限则在此等待)。
        # 给每个 handler 传入 message 的拷贝(dict(message))，避免多个 handler 相互修改同一对象。
        handlers = list(self._subscriptions.get(channel, {}).values())
        for handler in handlers:
            await self._handler_semaphore.acquire()
            task = asyncio.create_task(
                self._run_handler(channel, handler, dict(message)),
                name=f"pubsub-handler:{channel}",
            )
            self._handler_tasks.add(task)
            # 任务完成回调里会把它移出集合并释放信号量。
            task.add_done_callback(self._on_handler_task_done)

    def _on_handler_task_done(self, task: asyncio.Task[None]) -> None:
        # 处理器任务结束：从在跑集合移除并归还一个信号量名额。
        self._handler_tasks.discard(task)
        self._handler_semaphore.release()

    async def _run_handler(
        self,
        channel: str,
        handler: PubSubHandler,
        message: dict[str, Any],
    ) -> None:
        # 执行单个 handler：兼容同步/异步两种返回；handler 内部异常只记录不外抛，
        # 以免单个订阅者出错影响其他订阅者或拖垮监听循环。
        try:
            result = handler(message)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Pub/sub hub handler failed for channel %s: %s", channel, e)

    async def _cancel_handler_tasks(self) -> None:
        # 关闭时取消所有仍在运行的处理器任务并等待其结束（忽略产生的异常）。
        tasks = list(self._handler_tasks)
        if not tasks:
            return
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._handler_tasks.clear()

    def _schedule_resubscribe(self) -> None:
        # 调度一次「重订阅唤醒」：订阅表变化时被调用。
        # 由于 pubsub.listen() 处于阻塞状态，无法直接加订阅，故用一次性任务去关闭当前连接，
        # 迫使监听循环走到 finally 后以最新频道集合重新订阅。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 不在事件循环中(例如启动前的登记)则无需唤醒，监听循环启动时自会读取最新订阅表。
            return
        # 已有未完成的唤醒任务则复用，避免重复关闭连接。
        if self._resubscribe_task is not None and not self._resubscribe_task.done():
            return
        self._resubscribe_task = loop.create_task(self._poke_listener())
        self._resubscribe_task.add_done_callback(self._on_resubscribe_task_done)

    def _on_resubscribe_task_done(self, task: asyncio.Task[None]) -> None:
        # 唤醒任务完成回调：清空引用，并把任务内的异常降级为 warning 日志。
        if self._resubscribe_task is task:
            self._resubscribe_task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as e:
            logger.warning("Pub/sub hub resubscribe poke failed: %s", e)

    async def _poke_listener(self) -> None:
        # 通过关闭当前 pubsub 连接来「戳醒」阻塞在 listen() 的监听循环，触发其重订阅。
        pubsub = self._pubsub
        if pubsub is not None:
            await self._close_pubsub(pubsub)

    async def _cancel_resubscribe_task(self) -> None:
        # 取消并等待重订阅唤醒任务（关闭流程的一部分）。
        task = self._resubscribe_task
        if task is None:
            return
        self._resubscribe_task = None
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _detach_pubsub(self, pubsub: Any | None) -> None:
        # 解绑本轮 pubsub：关闭它、清除「预期断开」标记，并在它仍是当前 pubsub 时置空引用。
        if pubsub is None:
            return

        await self._close_pubsub(pubsub)
        self._expected_disconnects.discard(id(pubsub))
        async with self._lock:
            if self._pubsub is pubsub:
                self._pubsub = None

    async def _close_pubsub(self, pubsub: Any) -> None:
        # 主动关闭一个 pubsub 连接。先把它的 id 标记为「预期断开」，
        # 这样监听循环因此产生的 ConnectionError 会被识别为主动重订阅而非异常断线。
        self._expected_disconnects.add(id(pubsub))
        try:
            await pubsub.close()
        except Exception as e:
            logger.warning("Failed to close shared pub/sub connection: %s", e)

    async def _close_redis_client(self, redis_client: Any | None) -> None:
        # 关闭底层 Redis 客户端（隔离连接池），失败仅告警不抛出。
        if redis_client is None:
            return
        try:
            await redis_client.aclose()
        except Exception as e:
            logger.warning("Failed to close shared pub/sub Redis client: %s", e)

    def _is_expected_disconnect(self, pubsub: Any, error: Exception) -> bool:
        # 判断某次断开是否为我们主动关闭连接引发的「预期断开」：
        # 既要 id 在预期集合中，又要错误确实是服务端关闭连接的 ConnectionError。
        if id(pubsub) not in self._expected_disconnects:
            return False
        return (
            isinstance(error, RedisConnectionError) and str(error) == "Connection closed by server."
        )


# 进程内单例：整个进程共享同一个 pub/sub 中枢，减少空闲连接与后台监听任务数。
_pubsub_hub: RedisPubSubHub | None = None


def get_pubsub_hub() -> RedisPubSubHub:
    # 惰性获取(必要时创建)进程级 pub/sub 中枢单例。
    global _pubsub_hub
    if _pubsub_hub is None:
        _pubsub_hub = RedisPubSubHub()
    return _pubsub_hub


async def close_pubsub_hub() -> None:
    """Stop and release the process-local pub/sub hub without creating it."""
    # 关闭并释放已存在的中枢单例；若从未创建则不会「顺手创建再关闭」，避免副作用。
    global _pubsub_hub
    hub = _pubsub_hub
    _pubsub_hub = None
    if hub is not None:
        await hub.stop()

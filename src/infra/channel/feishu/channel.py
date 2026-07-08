"""
Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection.

Supports per-user bot configurations - each user can have their own Feishu bot.
"""

import asyncio
import importlib
import importlib.util
import json
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.channel.base import BaseChannel
from src.infra.channel.feishu.sender import FeishuSenderMixin
from src.infra.channel.feishu.state import ConnectionState
from src.infra.channel.feishu.utils import (
    MSG_TYPE_MAP,
    extract_post_content,
    extract_share_card_content,
)
from src.infra.logging import get_logger
from src.infra.storage.redis import get_redis_client
from src.kernel.schemas.channel import ChannelCapability, ChannelType
from src.kernel.schemas.feishu import (
    DEFAULT_AUDIO_TRANSCRIBE_PROMPT,
    FeishuConfig,
    FeishuGroupPolicy,
)

logger = get_logger(__name__)

# 探测 lark-oapi（飞书官方 SDK）是否已安装；未安装则飞书渠道整体不可用。
FEISHU_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None
# 已处理消息 ID 的去重缓存 TTL（15 分钟）与本地缓存容量上限。
_PROCESSED_MESSAGE_TTL_SECONDS = 15 * 60
_PROCESSED_MESSAGE_CACHE_MAX = 1000
# 以下三个进程级全局共同维护"唯一一个飞书 WS 事件循环线程"：
# 锁保护创建过程，_FEISHU_WS_LOOP 是共享事件循环，_FEISHU_WS_THREAD 是承载它的线程。
_FEISHU_WS_LOOP_LOCK = threading.Lock()
_FEISHU_WS_LOOP: asyncio.AbstractEventLoop | None = None
_FEISHU_WS_THREAD: threading.Thread | None = None
# 依赖的 lark-oapi 私有 WS API 的版本号（本文件适配的是该版本的内部实现）。
_LARK_OAPI_WS_PRIVATE_API_VERSION = "1.6.5"


async def _cancel_and_wait_future(future: Any) -> None:
    # 统一取消并等待一个 future/Task 结束；已完成或为空则直接返回。
    # 兼容 asyncio.Future 与 concurrent.futures.Future（后者需 wrap_future 桥接）。
    if future is None or future.done():
        return
    future.cancel()
    try:
        if isinstance(future, asyncio.Future):
            await future
        else:
            await asyncio.wrap_future(future)
    except (asyncio.CancelledError, Exception):
        # 取消过程中的任何异常都吞掉：这是清理路径，不应再抛出。
        pass


def _ensure_feishu_ws_loop() -> asyncio.AbstractEventLoop:
    """Return the shared lark-oapi WebSocket loop.

    lark-oapi keeps a process-global ``lark_oapi.ws.client.loop`` and uses it
    inside client methods. Running each tenant on a separate event loop makes
    SDK tasks await futures created by another loop, so all Feishu WS clients
    share one dedicated loop thread.
    """
    global _FEISHU_WS_LOOP, _FEISHU_WS_THREAD
    # 加锁保证并发调用只会创建一个共享循环线程。
    with _FEISHU_WS_LOOP_LOCK:
        # 已有且未关闭则直接复用。
        if _FEISHU_WS_LOOP and not _FEISHU_WS_LOOP.is_closed():
            return _FEISHU_WS_LOOP

        # 用 Event 等待新线程真正把事件循环跑起来后再返回，避免竞态。
        ready = threading.Event()
        ws_loop = asyncio.new_event_loop()

        def _run_feishu_ws_loop() -> None:
            import lark_oapi.ws.client as _lark_ws_client

            # 在该线程内绑定事件循环，并把它写入 lark-oapi 的进程级全局，
            # 使 SDK 内部创建的 future 都归属于这同一个循环。
            asyncio.set_event_loop(ws_loop)
            _lark_ws_client.loop = ws_loop
            ready.set()
            ws_loop.run_forever()

        _FEISHU_WS_LOOP = ws_loop
        # 守护线程：进程退出时自动结束，不阻塞关停。
        _FEISHU_WS_THREAD = threading.Thread(
            target=_run_feishu_ws_loop,
            daemon=True,
            name="feishu-ws-loop",
        )
        _FEISHU_WS_THREAD.start()
        ready.wait(timeout=5)
        return ws_loop


class FeishuChannel(FeishuSenderMixin, BaseChannel):
    """Feishu/Lark channel implementation for a single user."""

    channel_type = ChannelType.FEISHU
    display_name = "Feishu / Lark"
    description = "Feishu/Lark enterprise communication platform"
    icon = "BotMessageSquare"

    # Reconnection configuration
    INITIAL_RECONNECT_DELAY = 1.0  # Initial delay in seconds
    MAX_RECONNECT_DELAY = 60.0  # Maximum delay in seconds
    RECONNECT_BACKOFF_FACTOR = 2.0  # Exponential backoff factor
    HEALTH_CHECK_INTERVAL = 30.0  # Check connection health every 30 seconds
    CONNECTION_TIMEOUT = 180.0  # Consider connection dead if no response for 3 minutes

    # Override SDK defaults for faster reconnection
    _SDK_RECONNECT_INTERVAL = 10  # SDK retry interval (default 120s, too slow)
    _SDK_RECONNECT_NONCE = 5  # SDK first-reconnect jitter (default 30s, too much)

    # Processing status emoji shown while the agent is working.
    PROCESSING_EMOJI = "StatusInFlight"

    def __init__(self, config: FeishuConfig, message_handler: Optional[Callable] = None):
        super().__init__(config, message_handler)
        # 各类 SDK 客户端句柄（惰性构建）：普通 API client、HTTP client、WS client。
        self._client: Any = None
        self._feishu_http_client: Any = None
        self._ws_client: Any = None
        # WS 相关的线程/future 引用，用于启停时精确取消。
        self._ws_thread: threading.Thread | None = None
        self._ws_future: Any = None
        self._health_check_future: Any = None
        # 共享 WS 事件循环引用，以及本渠道所在的主事件循环引用。
        self._ws_loop_ref: asyncio.AbstractEventLoop | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # 已处理消息 ID 的 LRU 集合（OrderedDict 当有序集合用）做本地去重。
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._chat_mode_cache: OrderedDict[str, str] = (
            OrderedDict()
        )  # Cache: chat_id -> "group"|"thread"

        # Connection state tracking
        # 连接状态跟踪：状态值、保护它的锁、最近活跃时间与重连退避计数。
        self._connection_state = ConnectionState.DISCONNECTED
        self._state_lock = threading.Lock()
        self._last_activity_time = 0.0
        self._reconnect_attempts = 0
        self._current_reconnect_delay = self.INITIAL_RECONNECT_DELAY

    @classmethod
    def get_capabilities(cls) -> list[ChannelCapability]:
        """Get Feishu channel capabilities."""
        return [
            ChannelCapability.WEBSOCKET,
            ChannelCapability.WEBHOOK,
            ChannelCapability.SEND_MESSAGE,
            ChannelCapability.SEND_IMAGE,
            ChannelCapability.SEND_FILE,
            ChannelCapability.REACTIONS,
            ChannelCapability.GROUP_CHAT,
            ChannelCapability.DIRECT_MESSAGE,
        ]

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        """Get JSON schema for Feishu configuration."""
        return {
            "type": "object",
            "required": ["app_id", "app_secret"],
            "properties": {
                "app_id": {
                    "type": "string",
                    "title": "App ID",
                    "description": "Feishu application App ID",
                },
                "app_secret": {
                    "type": "string",
                    "title": "App Secret",
                    "description": "Feishu application App Secret",
                    "sensitive": True,
                },
                "verification_token": {
                    "type": "string",
                    "title": "Verification Token",
                    "description": "Verification token for webhook events (optional)",
                },
                "encrypt_key": {
                    "type": "string",
                    "title": "Encrypt Key",
                    "description": "Encryption key for event decryption (optional)",
                    "sensitive": True,
                },
                "group_policy": {
                    "type": "string",
                    "enum": ["open", "mention"],
                    "title": "Group Policy",
                    "description": "How to handle group messages",
                    "default": "mention",
                },
                "react_emoji": {
                    "type": "string",
                    "title": "Reaction Emoji",
                    "description": "Emoji to react when receiving messages",
                    "default": "THUMBSUP",
                },
                "stream_reply": {
                    "type": "boolean",
                    "title": "Stream Replies",
                    "description": "Render replies with Feishu CardKit streaming updates",
                    "default": True,
                },
                "auto_transcribe_audio": {
                    "type": "boolean",
                    "title": "Auto Transcribe Audio",
                    "description": "Attach audio and ask the agent to transcribe it",
                    "default": True,
                },
                "audio_transcribe_prompt": {
                    "type": "string",
                    "title": "Audio Transcription Prompt",
                    "description": "Prompt sent to the agent when an audio message arrives",
                    "default": DEFAULT_AUDIO_TRANSCRIBE_PROMPT,
                },
            },
        }

    @classmethod
    def get_config_fields(cls) -> list[dict[str, Any]]:
        """Get configuration fields for UI rendering."""
        return [
            {
                "name": "app_id",
                "title": "App ID",
                "type": "text",
                "required": True,
                "sensitive": False,
                "placeholder": "cli_xxxxxxxxxx",
            },
            {
                "name": "app_secret",
                "title": "App Secret",
                "type": "password",
                "required": True,
                "sensitive": True,
                "placeholder": "",
            },
            {
                "name": "encrypt_key",
                "title": "Encrypt Key",
                "type": "text",
                "required": False,
                "sensitive": True,
                "placeholder": "",
            },
            {
                "name": "verification_token",
                "title": "Verification Token",
                "type": "text",
                "required": False,
                "sensitive": False,
                "placeholder": "",
            },
            {
                "name": "react_emoji",
                "title": "Reaction Emoji",
                "type": "select",
                "required": False,
                "sensitive": False,
                "default": "THUMBSUP",
                "options": [
                    {"value": "THUMBSUP", "label": "👍 已收到"},
                    {"value": "OK", "label": "👌 好的"},
                    {"value": "DONE", "label": "✅ 已完成"},
                    {"value": "Yes", "label": "☑️ 确认"},
                    {"value": "CheckMark", "label": "✔️ 打勾"},
                    {"value": "Get", "label": "📥 收到"},
                    {"value": "OnIt", "label": "🎯 在做了"},
                    {"value": "OneSecond", "label": "⏳ 稍等"},
                    {"value": "LGTM", "label": "👀 看过了"},
                    {"value": "MeMeMe", "label": "🙋 我来"},
                    {"value": "THANKS", "label": "🙏 谢谢"},
                    {"value": "SALUTE", "label": "🫡 收到"},
                    {"value": "CLAP", "label": "👏 好的"},
                    {"value": "Fire", "label": "🔥 处理中"},
                    {"value": "MUSCLE", "label": "💪 加油"},
                    {"value": "PRAISE", "label": "🏅 好样的"},
                ],
            },
            {
                "name": "group_policy",
                "title": "Group Message Policy",
                "type": "select",
                "required": False,
                "sensitive": False,
                "default": "mention",
                "options": [
                    {"value": "mention", "label": "Reply only when @mentioned"},
                    {"value": "open", "label": "Reply to all messages"},
                ],
            },
            {
                "name": "stream_reply",
                "title": "Stream Replies",
                "type": "toggle",
                "required": False,
                "sensitive": False,
                "default": True,
            },
            {
                "name": "auto_transcribe_audio",
                "title": "Auto Transcribe Audio",
                "type": "toggle",
                "required": False,
                "sensitive": False,
                "default": True,
            },
            {
                "name": "audio_transcribe_prompt",
                "title": "Audio Transcription Prompt",
                "type": "textarea",
                "required": False,
                "sensitive": False,
                "default": DEFAULT_AUDIO_TRANSCRIBE_PROMPT,
            },
        ]

    @classmethod
    def get_setup_guide(cls) -> list[str]:
        """Get Feishu setup guide."""
        return [
            "Go to Feishu Open Platform (open.feishu.cn)",
            "Create a custom app and get App ID and App Secret",
            "Enable bot capability and subscribe to message events",
            "Use WebSocket long connection (no public IP required)",
        ]

    def _set_connection_state(self, new_state: ConnectionState) -> None:
        """Update connection state with logging."""
        # 加锁更新状态并打点；状态未变则不记录。
        with self._state_lock:
            old_state = self._connection_state
            if old_state != new_state:
                self._connection_state = new_state
                logger.info(
                    f"Feishu connection state changed for user {self.config.user_id}: "
                    f"{old_state.value} -> {new_state.value}"
                )
                # Reset reconnect delay on successful connection
                # 一旦成功连上，就把重连退避计数与延迟复位，并刷新活跃时间。
                if new_state == ConnectionState.CONNECTED:
                    self._reconnect_attempts = 0
                    self._current_reconnect_delay = self.INITIAL_RECONNECT_DELAY
                    self._last_activity_time = time.time()

    def _get_connection_state(self) -> ConnectionState:
        """Get current connection state."""
        with self._state_lock:
            return self._connection_state

    def _update_activity_time(self) -> None:
        """Update last activity timestamp."""
        self._last_activity_time = time.time()

    def _get_reconnect_delay(self) -> float:
        """Calculate reconnect delay with exponential backoff."""
        # 指数退避：返回当前延迟，同时把下一次延迟翻倍（上限 MAX_RECONNECT_DELAY），
        # 并累加重连尝试次数。
        delay = self._current_reconnect_delay
        self._reconnect_attempts += 1
        self._current_reconnect_delay = min(
            self._current_reconnect_delay * self.RECONNECT_BACKOFF_FACTOR,
            self.MAX_RECONNECT_DELAY,
        )
        return delay

    def _reset_reconnect_delay(self) -> None:
        """Reset reconnect delay to initial value."""
        self._reconnect_attempts = 0
        self._current_reconnect_delay = self.INITIAL_RECONNECT_DELAY

    def _is_connection_healthy(self) -> bool:
        """Check if connection is healthy based on activity."""
        # 依据"最近活跃时间"判断连接是否僵死：从未活跃视为健康，
        # 否则要求距上次活跃未超过 CONNECTION_TIMEOUT。
        if self._last_activity_time == 0:
            return True  # No activity recorded yet
        elapsed = time.time() - self._last_activity_time
        return elapsed < self.CONNECTION_TIMEOUT

    async def start(self) -> bool:
        """Start the Feishu bot with WebSocket long connection."""
        # 前置校验：SDK 未安装或缺少 app_id/app_secret 都无法启动。
        if not FEISHU_AVAILABLE:
            logger.error(
                f"Feishu SDK not installed for user {self.config.user_id}. Run: pip install lark-oapi"
            )
            return False

        if not self.config.app_id or not self.config.app_secret:
            logger.error(
                f"Feishu app_id and app_secret not configured for user {self.config.user_id}"
            )
            return False

        # 记录当前主事件循环（消息回调会被调度回它上面执行）。
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._set_connection_state(ConnectionState.CONNECTING)

        # Build SDK clients in executor to avoid blocking the event loop
        # (lark SDK import/constructors may make synchronous work)
        # 在线程池里构建 SDK 客户端与事件分发器，避免其同步导入/构造阻塞事件循环。
        def _build_clients():
            lark = importlib.import_module("lark_oapi")
            client = (
                lark.Client.builder()
                .app_id(self.config.app_id)
                .app_secret(self.config.app_secret)
                .log_level(lark.LogLevel.INFO)
                .build()
            )

            # 事件分发器：注册各类事件回调。这里同时注册消息接收、
            # 表情增删（占位空回调）与卡片按钮点击（审批交互）。
            builder = lark.EventDispatcherHandler.builder(
                self.config.encrypt_key or "",
                self.config.verification_token or "",
            )
            builder = builder.register_p2_im_message_receive_v1(self._on_message_sync)
            # 部分 SDK 版本才有这些注册方法，用 hasattr 做兼容性判断。
            if hasattr(builder, "register_p2_im_message_reaction_created_v1"):
                builder = builder.register_p2_im_message_reaction_created_v1(lambda data: None)
            if hasattr(builder, "register_p2_im_message_reaction_deleted_v1"):
                builder = builder.register_p2_im_message_reaction_deleted_v1(lambda data: None)
            if hasattr(builder, "register_p2_card_action_trigger"):
                builder = builder.register_p2_card_action_trigger(self._on_card_action_sync)

            event_handler = builder.build()
            return client, event_handler

        self._client, event_handler = await run_blocking_io(_build_clients)

        # 把 WS 客户端协程调度到"共享 WS 事件循环线程"上运行（跨循环用 threadsafe）。
        self._ws_loop_ref = _ensure_feishu_ws_loop()
        self._ws_future = asyncio.run_coroutine_threadsafe(
            self._run_ws_client(event_handler),
            self._ws_loop_ref,
        )

        # 健康检查协程同样跑在共享 WS 循环上，用于检测并强制重连僵尸连接。
        self._health_check_future = asyncio.run_coroutine_threadsafe(
            self._health_check_loop(),
            self._ws_loop_ref,
        )

        logger.info(
            f"Feishu bot started for user {self.config.user_id} with WebSocket long connection"
        )
        return True

    async def _run_ws_client(self, event_handler: Any) -> None:
        """Run one SDK WebSocket client on the shared lark-oapi loop."""
        import lark_oapi as lark
        import lark_oapi.ws.client as _lark_ws_client

        ws_loop = asyncio.get_running_loop()
        # lark-oapi reads this process-global loop; every tenant is scheduled
        # onto the shared loop from _ensure_feishu_ws_loop(), so this assignment
        # is idempotent across tenants.
        _lark_ws_client.loop = ws_loop
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )
        # 覆盖 SDK 默认的重连参数，加快断线后的恢复速度。
        self._ws_client._reconnect_interval = self._SDK_RECONNECT_INTERVAL
        self._ws_client._reconnect_nonce = self._SDK_RECONNECT_NONCE

        ping_task: asyncio.Task | None = None
        try:
            # 外层重连循环：只要渠道仍在运行，断线后就按退避策略重连。
            while self._running:
                try:
                    self._set_connection_state(ConnectionState.CONNECTING)
                    logger.info(
                        f"Feishu WebSocket connecting for user {self.config.user_id} "
                        f"(attempt {self._reconnect_attempts + 1})"
                    )
                    await self._sdk_ws_connect()
                    # 连接成功：置为已连接、复位退避，并启动心跳 ping 任务（若未运行）。
                    self._set_connection_state(ConnectionState.CONNECTED)
                    self._reset_reconnect_delay()
                    if ping_task is None or ping_task.done():
                        ping_task = self._sdk_ws_start_ping(ws_loop)
                    # 内层保活循环：连接正常期间在此空转，直到出错跳出重连。
                    while self._running:
                        await asyncio.sleep(1)
                except Exception as e:
                    logger.warning(f"Feishu WebSocket error for user {self.config.user_id}: {e}")
                    # 出错且仍需运行：进入重连中，按指数退避等待后重试。
                    if self._running:
                        self._set_connection_state(ConnectionState.RECONNECTING)
                        delay = self._get_reconnect_delay()
                        logger.info(
                            f"Reconnecting in {delay:.1f}s (attempt {self._reconnect_attempts})"
                        )
                        await asyncio.sleep(delay)
        finally:
            # 无论正常退出还是异常，都要清理心跳任务与底层连接。
            if ping_task:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
            if self._ws_client is not None:
                try:
                    await self._sdk_ws_disconnect()
                except Exception:
                    pass
            self._set_connection_state(ConnectionState.DISCONNECTED)

    async def _sdk_ws_connect(self) -> None:
        """Connect through lark-oapi's private WS API.

        lark-oapi 1.6.5 does not expose a public async runner that can host all
        tenant clients on the SDK's process-global loop. Keep the private-method
        dependency in these adapter methods so future SDK changes have one place
        to update.
        """
        await self._ws_client._connect()

    def _sdk_ws_start_ping(self, loop: asyncio.AbstractEventLoop) -> asyncio.Task:
        """Start the lark-oapi 1.6.5 private ping loop."""
        return loop.create_task(self._ws_client._ping_loop())

    async def _sdk_ws_disconnect(self) -> None:
        """Disconnect through lark-oapi's private WS API."""
        await self._ws_client._disconnect()

    async def _health_check_loop(self) -> None:
        """Health check loop to detect and force-reconnect zombie connections."""
        # 周期性巡检：飞书某些断线场景下 SDK 感知不到（僵尸连接），
        # 靠"最近活跃时间"超时来主动断开，从而触发 SDK 的重连逻辑。
        while self._running:
            await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)
            if not self._running:
                break

            state = self._get_connection_state()
            if state == ConnectionState.CONNECTED:
                if not self._is_connection_healthy():
                    logger.warning(
                        f"Feishu connection appears dead for user {self.config.user_id} "
                        f"(no activity for {time.time() - self._last_activity_time:.0f}s), "
                        "force-closing to trigger reconnect"
                    )
                    self._set_connection_state(ConnectionState.RECONNECTING)
                    # Force-close the underlying connection so the SDK detects
                    # the disconnect and triggers its reconnection loop.
                    # 强制断开底层连接（带超时），让 SDK 察觉断线并进入自动重连。
                    try:
                        if self._ws_loop_ref is None or self._ws_client is None:
                            continue
                        await asyncio.wait_for(self._sdk_ws_disconnect(), timeout=5)
                    except Exception:
                        pass
                else:
                    logger.debug(f"Feishu connection healthy for user {self.config.user_id}")

    async def stop(self) -> None:
        """Stop the Feishu bot."""
        # 置停并主动断开：由于 WS 客户端跑在另一线程的循环上，
        # 需用 run_coroutine_threadsafe 跨循环调度断开操作。
        self._running = False
        if self._ws_loop_ref is not None and self._ws_client is not None:
            try:
                await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(
                        self._sdk_ws_disconnect(),
                        self._ws_loop_ref,
                    )
                )
            except Exception:
                pass
        # 取消 WS 主协程与健康检查协程，并关闭 HTTP 客户端。
        await _cancel_and_wait_future(self._ws_future)
        await _cancel_and_wait_future(self._health_check_future)
        await self.close_feishu_http_client()
        self._set_connection_state(ConnectionState.DISCONNECTED)
        logger.info(f"Feishu bot stopped for user {self.config.user_id}")

    def _is_bot_mentioned(self, message: Any) -> bool:
        """Check if the bot is @mentioned in the message."""
        # @所有人 直接视为提到机器人。
        raw_content = message.content or ""
        if "@_all" in raw_content:
            return True

        # 遍历 mentions：机器人的被提及项特征是——没有 user_id 且 open_id 以 "ou_" 开头。
        for mention in getattr(message, "mentions", None) or []:
            mid = getattr(mention, "id", None)
            if not mid:
                continue
            if not getattr(mid, "user_id", None) and (
                getattr(mid, "open_id", None) or ""
            ).startswith("ou_"):
                return True
        return False

    def _is_group_message_for_bot(self, message: Any) -> bool:
        """Allow group messages when policy is open or bot is @mentioned."""
        # 群策略为 OPEN 时回应所有群消息；否则仅在被 @ 时回应。
        if self.config.group_policy == FeishuGroupPolicy.OPEN:
            return True
        return self._is_bot_mentioned(message)

    def _on_message_sync(self, data: Any) -> None:
        """Sync handler for incoming messages."""
        # SDK 在 WS 线程里以同步方式回调本方法，这里只做轻量处理并把重活
        # 调度回主事件循环执行。
        # Update activity time to indicate connection is alive
        # 收到消息即刷新活跃时间，作为连接存活的信号。
        self._update_activity_time()
        # Set state to connected if not already
        if self._get_connection_state() != ConnectionState.CONNECTED:
            self._set_connection_state(ConnectionState.CONNECTED)
        # 跨线程把真正的异步处理调度回主循环。
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    def _on_card_action_sync(self, data: Any) -> Any:
        """Sync handler for Feishu interactive card button clicks."""
        # 卡片按钮点击的同步回调（HITL 人机确认/审批流的入口）。
        # 飞书要求卡片回调"即时同步返回"一个响应，因此这里先构造并返回响应，
        # 真正的业务处理再异步调度到主循环执行。
        logger.debug("[HITL] Received card action callback")
        self._update_activity_time()
        if self._get_connection_state() != ConnectionState.CONNECTED:
            self._set_connection_state(ConnectionState.CONNECTED)
        # 立即生成"处理中"卡片作为同步响应，给用户即时反馈。
        response_payload = self._build_card_action_response_payload(data)
        approval_id = self._extract_lambchat_approval_id(data)
        logger.debug(
            "[HITL] approval_id=%s Returned processing card synchronously",
            approval_id,
        )
        # 异步执行真正的审批处理（更新状态、恢复被暂停的 agent 等）。
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_card_action(data), self._loop)

        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )

            return P2CardActionTriggerResponse(response_payload)
        except Exception:
            return None

    def _build_card_action_response_payload(self, data: Any) -> dict[str, Any]:
        """Build immediate Feishu callback response payload for known card actions."""
        # 仅对已知的 LambChat 审批动作返回定制响应（更新为处理中卡片 + toast 提示）；
        # 其它动作返回空 payload。
        approval_id = self._extract_lambchat_approval_id(data)
        if not approval_id:
            return {}

        from src.infra.channel.feishu.approval import build_feishu_approval_processing_card_data

        return {
            "card": {
                "type": "raw",
                "data": build_feishu_approval_processing_card_data(approval_id),
            },
            "toast": {
                "type": "success",
                "content": "已收到确认操作，正在处理",
            },
        }

    @staticmethod
    def _extract_lambchat_approval_id(data: Any) -> str | None:
        """Return the LambChat approval id from a Feishu card action callback."""
        # 从卡片回调里安全地解析出 LambChat 的 approval_id：
        # action.value 可能是 JSON 字符串或 dict，且必须匹配约定的 action 类型。
        try:
            event = getattr(data, "event", None)
            action = getattr(event, "action", None)
            value = getattr(action, "value", None)
            if isinstance(value, str) and value.strip():
                value = json.loads(value)
            if not isinstance(value, dict):
                return None

            from src.infra.channel.feishu.approval import FEISHU_APPROVAL_ACTION

            # 非本系统审批动作则忽略（同一 bot 可能承载多种卡片交互）。
            if value.get("action") != FEISHU_APPROVAL_ACTION:
                return None
            approval_id = value.get("approval_id")
            return approval_id if isinstance(approval_id, str) and approval_id else None
        except Exception:
            return None

    async def _on_card_action(self, data: Any) -> None:
        """Handle Feishu interactive card actions."""
        # This coroutine runs on the main event loop (scheduled from the lark WS
        # thread), so it bypasses the HTTP middleware that normally populates the
        # request context. Seed user_id here so downstream logs auto-include it.
        # 该协程由 WS 线程调度到主循环，绕过了通常注入请求上下文的 HTTP 中间件，
        # 因此这里手动补种 user_id，使后续日志能自动带上它。
        try:
            from src.infra.logging.context import TraceContext

            TraceContext.set_request_context(user_id=self.config.user_id)
        except Exception as e:
            logger.debug("[HITL] Failed to set request context for card action: %s", e)

        try:
            # 从回调中取出动作值、上下文与被操作的消息 ID。
            event = getattr(data, "event", None)
            action = getattr(event, "action", None)
            context = getattr(event, "context", None)
            value = getattr(action, "value", None)
            message_id = getattr(context, "open_message_id", None)

            approval_id = self._extract_lambchat_approval_id(data)
            logger.debug("[HITL] approval_id=%s Handling card action", approval_id)

            # 委托审批模块完成实际处理（写回审批结果、更新卡片、唤醒等待中的流程）。
            from src.infra.channel.feishu.approval import handle_feishu_approval_action
            from src.infra.channel.feishu.manager import get_feishu_channel_manager

            await handle_feishu_approval_action(
                value=value,
                message_id=message_id,
                user_id=self.config.user_id,
                instance_id=self.config.instance_id,
                manager=get_feishu_channel_manager(),
            )
        except Exception as e:
            logger.error(
                f"Error processing Feishu card action for user {self.config.user_id}: {e}",
                exc_info=True,
            )

    async def _on_message(self, data: Any) -> None:
        """Handle incoming message from Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # Deduplication check
            # 去重：飞书可能重复投递同一事件，已处理过的消息直接跳过。
            message_id = message.message_id
            if not await self._mark_message_processed(message_id):
                return

            # Skip bot messages
            # 忽略机器人自身/其它 bot 发出的消息，避免自问自答与回环。
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            # 群聊按群策略过滤：非开放模式且未 @ 机器人时不处理。
            if chat_type == "group" and not self._is_group_message_for_bot(message):
                logger.debug(
                    f"Feishu: skipping group message (not mentioned) for user {self.config.user_id}"
                )
                return

            # Add reaction to indicate the message is being handled; the handler
            # receives the reaction id so it can remove it after processing.
            # 先加一个表情回应表示"已收到/处理中"，并把 reaction_id 透传给处理器，
            # 以便处理完成后移除该表情。
            reaction_id = await self._add_reaction(message_id, self.config.react_emoji)

            # Parse content and extract attachments
            # 按消息类型解析文本与附件：content_parts 收集文本，attachments 收集下载后的附件。
            content_parts = []
            attachments = []

            try:
                content_json = (
                    await run_blocking_io(json.loads, message.content) if message.content else {}
                )
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    content_parts.append(text)

            elif msg_type == "post":
                # 富文本 post：抽取正文文本与内嵌图片 key，逐张下载入库为附件。
                text, image_keys = extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download embedded images from post
                for img_key in image_keys:
                    attachment = await self._download_and_store_image(img_key, message_id)
                    if attachment:
                        attachments.append(attachment)

            elif msg_type == "image":
                image_key = content_json.get("image_key")
                if image_key:
                    content_parts.append("[image]")
                    attachment = await self._download_and_store_image(image_key, message_id)
                    if attachment:
                        attachments.append(attachment)
                else:
                    content_parts.append("[image]")

            elif msg_type in ("audio", "file", "media"):
                # 音频/文件/视频：解析 file_key 与文件名，补齐缺失的扩展名后下载入库。
                file_key = content_json.get("file_key")
                file_name = content_json.get("file_name") or content_json.get("name") or file_key
                if msg_type == "audio" and file_name and "." not in file_name:
                    file_name = f"{file_name}.opus"
                if msg_type == "media" and file_name and "." not in file_name:
                    file_name = f"{file_name}.mp4"

                if file_key and file_name:
                    attachment_type = (
                        "audio"
                        if msg_type == "audio"
                        else "video"
                        if msg_type == "media"
                        else "document"
                    )
                    content_type = (
                        "audio/ogg"
                        if msg_type == "audio"
                        else "video/mp4"
                        if msg_type == "media"
                        else None
                    )
                    attachment = await self._download_and_store_resource(
                        file_key,
                        message_id,
                        resource_type="file",
                        file_name=file_name,
                        attachment_type=attachment_type,
                        content_type=content_type,
                    )
                    if attachment:
                        attachments.append(attachment)

                # 若开启了音频自动转写，则把转写提示词作为文本一并发给 agent；
                # 否则只放一个类型占位符。
                if msg_type == "audio" and getattr(self.config, "auto_transcribe_audio", True):
                    content_parts.append(
                        getattr(
                            self.config,
                            "audio_transcribe_prompt",
                            DEFAULT_AUDIO_TRANSCRIBE_PROMPT,
                        )
                        or DEFAULT_AUDIO_TRANSCRIBE_PROMPT
                    )
                else:
                    content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            elif msg_type in (
                "share_chat",
                "share_user",
                "interactive",
                "share_calendar_event",
                "system",
                "merge_forward",
            ):
                # 分享卡片/系统消息/合并转发等：抽取其可读文本摘要。
                text = await run_blocking_io(extract_share_card_content, content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                # 未知类型：放类型占位符兜底。
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            content = "\n".join(content_parts) if content_parts else ""

            # Replace @_user_N mentions with actual sender
            # 飞书正文里的 @_user_N 占位替换为真实发送者标识，便于下游理解。
            content = re.sub(r"@_user_\d+", f"@{sender_id}", content)

            # 既无文本也无附件的消息没有处理价值，直接返回。
            if not content and not attachments:
                return

            # Determine reply_to and handle topic groups
            # 决定回复目标：群聊回到群，单聊回到发送者。
            reply_to = chat_id if chat_type == "group" else sender_id
            root_id = None

            if chat_type == "group":
                # 话题群（thread 模式）需以话题 root_id 作为会话隔离键，
                # 使同一群里的不同话题各自拥有独立会话上下文。
                chat_mode = await self._get_chat_mode(chat_id)
                if chat_mode == "thread":
                    root_id = message.root_id or message_id
                    # Use root_id as session isolation key
                    reply_to = f"{chat_id}#{root_id}"

            # Forward to message handler via base class method
            # 组装元数据并交给基类的 _handle_message 统一转发到上层 agent 流程。
            metadata = {
                "message_id": message_id,
                "chat_type": chat_type,
                "msg_type": msg_type,
                "sender_id": sender_id,
                "reply_chat_id": chat_id,
            }
            if reaction_id:
                metadata["reaction_id"] = reaction_id
            if root_id:
                metadata["root_id"] = root_id
            if attachments:
                metadata["attachments"] = attachments

            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                metadata=metadata,
            )

        except Exception as e:
            logger.error(f"Error processing Feishu message for user {self.config.user_id}: {e}")

    async def _mark_message_processed(self, message_id: str) -> bool:
        """Mark a message as processed using local cache plus Redis NX dedupe."""
        # 两级去重：先查本地 LRU 缓存，命中说明本进程已处理过。
        if message_id in self._processed_message_ids:
            return False

        # 再用 Redis SET NX 做跨实例去重：多实例部署时保证同一消息只被一个实例认领。
        # NX 表示"仅当键不存在时写入成功"，EX 设过期，避免键无限堆积。
        redis_claimed = True
        try:
            redis_client = get_redis_client()
            redis_claimed = bool(
                await redis_client.set(
                    f"feishu:processed:{message_id}",
                    self.config.instance_id or self.config.user_id,
                    nx=True,
                    ex=_PROCESSED_MESSAGE_TTL_SECONDS,
                )
            )
        except Exception as e:
            # Redis 不可用时降级：仅依赖本地去重，不因此丢消息。
            logger.warning(
                "Feishu distributed dedupe unavailable for message %s: %s",
                message_id,
                e,
            )

        # 未抢到 Redis 锁说明别的实例已处理，跳过。
        if not redis_claimed:
            return False

        # 记入本地缓存，并按容量上限淘汰最旧的键（FIFO）。
        self._processed_message_ids[message_id] = None
        while len(self._processed_message_ids) > _PROCESSED_MESSAGE_CACHE_MAX:
            self._processed_message_ids.popitem(last=False)
        return True

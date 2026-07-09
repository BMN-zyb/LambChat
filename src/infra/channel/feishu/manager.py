"""
Feishu channel manager for managing multiple user bot connections.
"""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 飞书渠道管理器：管理"多用户 / 多实例"的飞书机器人长连接（每个 app_id 一条）。
# 关键难点是分布式协调——同一 app_id 在整个集群里只能由一个节点建立连接，否则会
# 重复收发。这里用 Redis 实现：
#   - 节点成员心跳（feishu:nodes:*，带 TTL）标记本节点存活；
#   - 一致性分配（HRW / Rendezvous 哈希，见 _preferred_owner）决定每个 app_id 的
#     首选归属节点，节点增减时只迁移少量 app_id；
#   - 租约（feishu:lease:*，SET NX + 后台续约 + Lua 原子释放/续期）保证独占；
#   - 周期性再均衡（_rebalance_loop）在节点上下线后把连接迁移到正确的节点。
# 一旦续约失败（丢失租约）就立即停掉本地连接以让出所有权；租约相关操作使用独立的
# Redis 连接池，与业务 Redis 隔离。
# 关键依赖：ChannelStorage、FeishuChannel、Redis、FeishuConfig。
# ============================================================================

import asyncio
import hashlib
import uuid
from typing import Any, Callable, Optional, cast

from redis.asyncio import Redis

from src.infra.channel.base import UserChannelManager
from src.infra.channel.channel_storage import ChannelStorage
from src.infra.channel.feishu.channel import FEISHU_AVAILABLE, FeishuChannel
from src.infra.logging import get_logger
from src.infra.storage.redis import create_redis_client
from src.kernel.schemas.channel import ChannelType
from src.kernel.schemas.feishu import (
    DEFAULT_AUDIO_TRANSCRIBE_PROMPT,
    FeishuConfig,
    FeishuGroupPolicy,
)

logger = get_logger(__name__)
# 分布式租约/节点相关的 Redis 键前缀与各类 TTL、刷新/再均衡周期（秒）。
# 集群里每个 app_id 通过"租约"保证仅由一个节点持有并连接，避免重复连线。
_FEISHU_LEASE_PREFIX = "feishu:lease"
_FEISHU_NODE_PREFIX = "feishu:nodes"
_FEISHU_LEASE_TTL_SECONDS = 60
_FEISHU_NODE_TTL_SECONDS = 60
_FEISHU_LEASE_REFRESH_INTERVAL = 20
_FEISHU_REBALANCE_INTERVAL = 20
# 释放租约的 Lua 脚本：仅当租约仍归本实例（值匹配）时才删除，
# 用原子操作避免"误删他人租约"的竞态。
_RELEASE_LEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""
# 续约的 Lua 脚本：同样先校验持有者再 EXPIRE 续期，保证只有持有者能续约。
_REFRESH_LEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""


class FeishuChannelManager(UserChannelManager):
    """
    Manager for all user Feishu channels.

    Manages multiple Feishu bot connections, one per user.
    """

    channel_type = ChannelType.FEISHU
    config_class = FeishuConfig

    def __init__(self, message_handler: Optional[Callable] = None):
        super().__init__(message_handler)
        self._storage = ChannelStorage()
        self._message_handler: Optional[Callable] = message_handler
        # Track active app_ids to prevent duplicate bot connections
        # 记录已激活的 app_id -> channel_key，用于防止同一机器人被重复连接。
        self._active_app_ids: dict[str, str] = {}  # app_id -> channel_key
        # 本节点在集群中的唯一实例 ID（租约/节点成员用它标识归属）。
        self._instance_id = uuid.uuid4().hex
        # 各 app_id 的租约续期后台任务；独立的租约用 Redis 连接；集群再均衡任务。
        self._lease_tasks: dict[str, asyncio.Task] = {}
        self._lease_redis: Redis | None = None
        self._rebalance_task: asyncio.Task | None = None

    # 取全局单例（与模块级 get_feishu_channel_manager 保持一致），覆盖基类默认实现。
    @classmethod
    def get_instance(cls) -> "FeishuChannelManager":
        """Get the singleton instance, consistent with get_feishu_channel_manager()."""
        return get_feishu_channel_manager()

    # 把存储读出的配置字典转换成 FeishuConfig：instance_id 优先用显式入参、其次取字典、
    # 最后回退空串；各字段带默认值（群策略/表情/流式回复/音频转写提示等）。
    def _dict_to_config(
        self,
        user_id: str,
        config_dict: dict[str, Any],
        instance_id: Optional[str] = None,
    ) -> FeishuConfig:
        """Convert a config dict to FeishuConfig."""
        # Use explicit instance_id, fallback to config_dict's instance_id, then empty string
        resolved_instance_id = instance_id or config_dict.get("instance_id") or ""
        return FeishuConfig(
            user_id=user_id,
            instance_id=resolved_instance_id,
            app_id=config_dict.get("app_id") or "",
            app_secret=config_dict.get("app_secret") or "",
            encrypt_key=config_dict.get("encrypt_key") or "",
            verification_token=config_dict.get("verification_token") or "",
            react_emoji=config_dict.get("react_emoji") or "THUMBSUP",
            group_policy=FeishuGroupPolicy(config_dict.get("group_policy") or "mention"),
            stream_reply=config_dict.get("stream_reply", True),
            auto_transcribe_audio=config_dict.get("auto_transcribe_audio", True),
            audio_transcribe_prompt=config_dict.get("audio_transcribe_prompt")
            or DEFAULT_AUDIO_TRANSCRIBE_PROMPT,
            enabled=config_dict.get("enabled", True),
        )

    async def start(self) -> None:
        """Start all enabled Feishu channels."""
        if not FEISHU_AVAILABLE:
            logger.warning("Feishu SDK not installed. Run: pip install lark-oapi")
            return

        self._running = True

        # 启动即做一次"对账"：只启动分配给本节点的配置，并拉起周期性再均衡任务。
        started, skipped = await self._reconcile_enabled_configs()
        self._ensure_rebalance_task()
        logger.info(
            "Feishu startup processed enabled configurations: started=%s skipped=%s",
            started,
            skipped,
        )

    async def stop(self) -> None:
        """Stop all Feishu channels."""
        self._running = False
        # 先停再均衡任务，避免停机过程中又拉起新连接。
        await self._cancel_rebalance_task()

        for user_id, client in list(self._channels.items()):
            try:
                await client.stop()
            except Exception as e:
                logger.error(f"Error stopping Feishu client for user {user_id}: {e}")

        # 释放本节点持有的所有租约、注销节点成员并关闭独立 Redis 连接，做到干净退出。
        await self._release_all_leases()
        await self._unregister_node()
        await self._close_lease_redis()
        self._channels.clear()
        self._active_app_ids.clear()
        await self._storage.close()

    async def _reconcile_enabled_configs(self) -> tuple[int, int]:
        """Start only configs assigned to this node and stop unassigned local channels."""
        # 对账核心：刷新本节点成员心跳，失败则本轮放弃（返回 0,0）。
        if not await self._refresh_node_membership():
            return 0, 0

        # 取当前存活节点列表，并确保包含本节点（用于一致性分配计算）。
        node_ids = await self._list_active_node_ids()
        if self._instance_id not in node_ids:
            node_ids.append(self._instance_id)
            node_ids.sort()

        started = 0
        skipped = 0
        # desired_local_keys 记录本轮"应由本节点持有"的渠道键，用于收尾时下线多余连接。
        desired_local_keys: set[str] = set()

        async for config_dict in self._storage.iter_enabled_configs(ChannelType.FEISHU):
            try:
                user_id = config_dict.get("user_id")
                if not user_id:
                    logger.warning("Skipping config without user_id")
                    skipped += 1
                    continue

                app_id = config_dict.get("app_id") or ""
                app_secret = config_dict.get("app_secret") or ""

                # 缺 app_id/app_secret 通常意味着解密失败，跳过并提示用户重存配置。
                if not app_id or not app_secret:
                    logger.warning(
                        f"Skipping Feishu config for user {user_id}: "
                        "missing app_id or app_secret (decryption may have failed). "
                        "Please re-save the channel configuration."
                    )
                    skipped += 1
                    continue

                channel_key = self._channel_key(
                    user_id,
                    config_dict.get("instance_id") or "",
                )
                # 一致性分配：仅当该 app_id 的"首选归属节点"是本节点时才在本地启动，
                # 否则确保本地不残留该渠道（可能是刚从别的节点迁移过来）。
                if self._preferred_owner(app_id, node_ids) != self._instance_id:
                    await self._stop_channel_by_key(channel_key)
                    skipped += 1
                    continue

                desired_local_keys.add(channel_key)
                config = self._dict_to_config(user_id, config_dict)
                # replace_existing=False：已在健康运行的相同连接不重启，避免抖动。
                if await self._start_user_client(config, replace_existing=False):
                    started += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(
                    f"Failed to reconcile Feishu client for user {config_dict.get('user_id')}: {e}"
                )
                skipped += 1

        # 收尾：本地仍在运行、但本轮不再分配给本节点的连接一律停掉。
        for channel_key in list(self._channels.keys()):
            if channel_key not in desired_local_keys:
                await self._stop_channel_by_key(channel_key)

        return started, skipped

    async def _refresh_node_membership(self) -> bool:
        # 写入/续期本节点的成员键（带 TTL），作为"本节点仍存活"的心跳。
        try:
            redis = self._get_lease_redis()
            await redis.set(
                self._node_key(self._instance_id),
                self._instance_id,
                ex=_FEISHU_NODE_TTL_SECONDS,
            )
            return True
        except Exception as e:
            logger.warning("[Feishu] Failed to refresh node membership: %s", e)
            return False

    async def _list_active_node_ids(self) -> list[str]:
        # 用 SCAN 遍历所有节点成员键，解析出当前存活的节点 ID 集合（排序后返回）。
        redis = self._get_lease_redis()
        pattern = self._node_key("*")
        cursor: int | str = 0
        node_ids: set[str] = set()
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=100)
            for key in keys:
                key_text = key.decode() if isinstance(key, bytes) else str(key)
                node_id = key_text.rsplit(":", 1)[-1]
                if node_id:
                    node_ids.add(node_id)
            # SCAN 游标回到 0 表示遍历结束。
            if int(cursor) == 0:
                return sorted(node_ids)

    async def _unregister_node(self) -> None:
        # 停机时删除本节点成员键，让其它节点尽快感知并接管其负载。
        try:
            redis = self._get_lease_redis()
            await redis.delete(self._node_key(self._instance_id))
        except Exception as e:
            logger.warning("[Feishu] Failed to unregister node membership: %s", e)

    def _ensure_rebalance_task(self) -> None:
        # 幂等地拉起再均衡后台任务（已在运行则不重复创建）。
        if self._rebalance_task and not self._rebalance_task.done():
            return
        self._rebalance_task = asyncio.create_task(self._rebalance_loop())

    async def _rebalance_loop(self) -> None:
        # 周期性再均衡：节点上下线会改变一致性分配结果，需定期对账把连接迁到正确节点。
        try:
            while self._running:
                await asyncio.sleep(_FEISHU_REBALANCE_INTERVAL)
                await self._reconcile_enabled_configs()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("[Feishu] Rebalance loop stopped unexpectedly: %s", e)
        finally:
            self._rebalance_task = None

    async def _cancel_rebalance_task(self) -> None:
        # 取消并等待再均衡任务结束（吞掉取消异常）。
        task = self._rebalance_task
        self._rebalance_task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _node_key(instance_id: str) -> str:
        # 拼接节点成员键。
        return f"{_FEISHU_NODE_PREFIX}:{instance_id}"

    @staticmethod
    def _channel_key(user_id: str, instance_id: str | None = None) -> str:
        # 渠道键：有实例 ID 用 "user:instance"，否则退化为裸 user_id。
        return f"{user_id}:{instance_id}" if instance_id else user_id

    @staticmethod
    def _preferred_owner(app_id: str, node_ids: list[str]) -> str | None:
        # 用"最高随机权重（HRW/Rendezvous 哈希）"确定 app_id 的首选归属节点：
        # 对每个节点计算 sha256(app_id:node) 取最大者。相比取模，节点增减时
        # 只会迁移少量 app_id，分配也更均匀。
        if not node_ids:
            return None
        return max(
            node_ids,
            key=lambda node_id: hashlib.sha256(f"{app_id}:{node_id}".encode()).hexdigest(),
        )

    async def _start_user_client(
        self,
        config: FeishuConfig,
        *,
        replace_existing: bool = True,
    ) -> bool:
        """Start a user's Feishu client."""
        # Use instance_id if available, otherwise use user_id for backward compatibility
        channel_key = self._channel_key(config.user_id, config.instance_id)

        # 若已存在且不要求替换、且同一 app_id 仍在健康运行，则复用旧连接：
        # 只刷新回调与租约续期任务，避免无谓的断开重连（对账场景常走这里）。
        existing_channel = self._channels.get(channel_key)
        existing_app_id = (
            getattr(existing_channel.config, "app_id", None) if existing_channel else None
        )
        existing_running = bool(
            getattr(existing_channel, "is_running", getattr(existing_channel, "_running", False))
        )
        if (
            existing_channel
            and not replace_existing
            and existing_app_id == config.app_id
            and existing_running
        ):
            existing_channel.message_handler = self._message_handler
            self._active_app_ids[config.app_id] = channel_key
            self._ensure_lease_refresh_task(config.app_id)
            return True

        # Prevent duplicate bot connections: same app_id should only have one active channel
        # 防重：同一 app_id 只允许一个活跃连接；若已被别的 channel_key 占用则跳过。
        app_id = config.app_id
        if app_id in self._active_app_ids:
            existing_key = self._active_app_ids[app_id]
            if existing_key != channel_key and existing_key in self._channels:
                logger.warning(
                    f"[Feishu] Duplicate bot detected: app_id={app_id} already active "
                    f"as '{existing_key}', skipping '{channel_key}'"
                )
                return False

        # 抢占分布式租约：抢不到说明集群里其它实例正持有该 app_id，本地不启动。
        if not await self._acquire_lease(app_id):
            logger.info(
                "[Feishu] Lease for app_id=%s is held by another instance, skipping '%s'",
                app_id,
                channel_key,
            )
            return False

        try:
            # 替换旧连接前先停掉它，并清理其 app_id 追踪记录。
            if channel_key in self._channels:
                await self._channels[channel_key].stop()
                # Clean up old app_id tracking
                old_app_id = getattr(self._channels[channel_key].config, "app_id", None)
                if old_app_id and old_app_id in self._active_app_ids:
                    del self._active_app_ids[old_app_id]

            # 新建并启动飞书客户端；成功则登记并启动租约续期，失败则释放已抢到的租约。
            client = FeishuChannel(config, self._message_handler)
            success = await client.start()

            if success:
                self._channels[channel_key] = client
                self._active_app_ids[app_id] = channel_key
                self._ensure_lease_refresh_task(app_id)
                return True
            await self._release_lease(app_id)
            return False
        except BaseException:
            # 启动异常也要归还租约，防止租约泄漏导致该 app_id 永远无人接管。
            await self._release_lease(app_id)
            raise

    async def reload_user(self, user_id: str, instance_id: Optional[str] = None) -> bool:
        """Reload a user's Feishu configuration and restart the client.

        Args:
            user_id: The user ID
            instance_id: Optional specific instance ID to reload. If None, reloads all instances.
        """
        # If instance_id is provided, stop only that specific instance
        # 指定了 instance_id：只处理该实例。先停掉本地已有连接。
        if instance_id:
            # Check if this specific instance has an active connection
            channel_key = self._channel_key(user_id, instance_id)
            if channel_key in self._channels:
                await self._stop_channel_by_key(channel_key)
                logger.info(f"Stopped Feishu client for {channel_key}")

            # Check if there's still config for this instance
            # 若该实例仍存在且启用，则按一致性分配判断是否应由本节点接管；
            # 不属于本节点则直接返回（交给首选节点去启动）。
            config_dict = await self._storage.get_config(user_id, ChannelType.FEISHU, instance_id)
            if config_dict and config_dict.get("enabled", True):
                if await self._refresh_node_membership():
                    nodes = await self._list_active_node_ids()
                    if self._instance_id not in nodes:
                        nodes.append(self._instance_id)
                    app_id = config_dict.get("app_id") or ""
                    if self._preferred_owner(app_id, sorted(nodes)) != self._instance_id:
                        return True
                config = self._dict_to_config(user_id, config_dict, instance_id)
                return await self._start_user_client(config)
            return True

        # Legacy behavior: reload all instances for user
        # 兼容旧行为：未指定实例时，重载该用户所有飞书配置。
        feishu_configs = await self._storage.list_user_configs_by_type(user_id, ChannelType.FEISHU)

        # Stop all existing clients
        # 先停掉该用户所有本地连接（键以 user_id 前缀）。
        for key in list(self._channels.keys()):
            if key.startswith(user_id):
                await self._stop_channel_by_key(key)

        # Start all enabled clients
        # 再逐个启动启用中的配置，同样先做本节点归属判断。
        for config_dict in feishu_configs:
            if config_dict.get("enabled", True):
                inst_id = config_dict.get("instance_id")
                app_id = config_dict.get("app_id") or ""
                if await self._refresh_node_membership():
                    nodes = await self._list_active_node_ids()
                    if self._instance_id not in nodes:
                        nodes.append(self._instance_id)
                    if self._preferred_owner(app_id, sorted(nodes)) != self._instance_id:
                        continue
                config = self._dict_to_config(user_id, config_dict, inst_id)
                await self._start_user_client(config)

        return True

    # 按 user_id 查找本地渠道实例并 cast 成 FeishuChannel：优先精确匹配
    # "user:instance"，再退化为裸 user_id，最后按 "user_id:" 前缀命中该用户任一实例。
    def _find_channel(
        self, user_id: str, instance_id: Optional[str] = None
    ) -> Optional[FeishuChannel]:
        """Find a channel by user_id, with fallback to prefix match.

        Lookup order:
        1. Exact match: "user_id:instance_id" (if instance_id provided)
        2. Exact match: "user_id"
        3. Prefix match: first key starting with "user_id:"
        """
        if instance_id:
            channel = self._channels.get(f"{user_id}:{instance_id}")
            if channel:
                return cast(FeishuChannel, channel)

        channel = self._channels.get(user_id)
        if channel:
            return cast(FeishuChannel, channel)

        # Fallback: find first channel whose key starts with "user_id:"
        prefix = f"{user_id}:"
        for key, ch in self._channels.items():
            if key.startswith(prefix):
                logger.debug(
                    f"[Feishu] _find_channel fallback: matched key '{key}' for user '{user_id}'"
                )
                return cast(FeishuChannel, ch)

        return None

    # 通过某用户的飞书机器人发送文本消息：找不到本地连接则告警并返回 False。
    async def send_message(
        self,
        user_id: str,
        chat_id: str,
        content: str,
        instance_id: Optional[str] = None,
    ) -> bool:
        """Send a message through a user's Feishu bot."""
        client = self._find_channel(user_id, instance_id)
        if not client:
            logger.warning(f"No Feishu client for user {user_id}, instance {instance_id}")
            return False

        return await client.send_message(chat_id, content)

    # 发送交互式卡片并返回 (是否成功, message_id)：先定位客户端、解析接收方，再走内部
    # 发送实现（供审批卡片等需要记住 message_id 以便后续更新的场景）。
    async def send_card_message_with_id(
        self,
        user_id: str,
        chat_id: str,
        card_content: str,
        instance_id: Optional[str] = None,
        reply_to_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Send an interactive card through a user's Feishu bot."""
        logger.info("[Feishu] Sending card user=%s chat=%s", user_id, chat_id)
        client = self._find_channel(user_id, instance_id)
        if not client:
            logger.warning(f"No Feishu client for user {user_id}, instance {instance_id}")
            return False, None
        receive_id_type, receive_id = client._resolve_receive_id(chat_id)
        success, message_id = await client._send_card_message_internal(
            receive_id_type,
            receive_id,
            card_content,
            reply_to_id,
        )
        logger.info(
            "[Feishu] Card sent ok=%s message_id=%s user=%s chat=%s",
            success,
            message_id,
            user_id,
            chat_id,
        )
        return success, message_id

    # 通过某用户的飞书机器人给消息加表情回应，返回 reaction_id；无连接返回 None。
    async def add_reaction(
        self,
        user_id: str,
        message_id: str,
        emoji_type: str,
        instance_id: Optional[str] = None,
    ) -> str | None:
        """Add a reaction emoji to a message via a user's Feishu bot."""
        client = self._find_channel(user_id, instance_id)
        if not client:
            return None
        return await client._add_reaction(message_id, emoji_type)

    # 通过某用户的飞书机器人删除某条表情回应；无连接返回 False。
    async def delete_reaction(
        self,
        user_id: str,
        message_id: str,
        reaction_id: str,
        instance_id: Optional[str] = None,
    ) -> bool:
        """Delete a reaction emoji from a message via a user's Feishu bot."""
        client = self._find_channel(user_id, instance_id)
        if not client:
            return False
        return await client._delete_reaction(message_id, reaction_id)

    # 判断某用户（可指定实例）的飞书连接在"本地"是否已建立且处于运行态。
    def is_connected(self, user_id: str, instance_id: Optional[str] = None) -> bool:
        """Check if a user's Feishu bot is connected."""
        channel = self._find_channel(user_id, instance_id)
        return channel is not None and channel._running

    async def is_connected_distributed(
        self, user_id: str, instance_id: Optional[str] = None
    ) -> bool:
        """Check whether a Feishu bot is connected anywhere in the cluster."""
        # 先看本地是否连接；本地没有则通过"租约是否被集群任一节点持有"来判断远端连接。
        if self.is_connected(user_id, instance_id):
            return True

        if instance_id:
            config = await self._storage.get_config(user_id, ChannelType.FEISHU, instance_id)
            return await self._has_cluster_lease(config)

        # 未指定实例：遍历该用户所有启用配置，任一本地连接或持有集群租约即视为已连接。
        configs = await self._storage.list_user_configs_by_type(user_id, ChannelType.FEISHU)
        for config in configs:
            if not config.get("enabled", True):
                continue
            config_instance_id = config.get("instance_id")
            if config_instance_id and self.is_connected(user_id, config_instance_id):
                return True
            if await self._has_cluster_lease(config):
                return True
        return False

    async def _stop_channel_by_key(self, channel_key: str) -> None:
        # 按渠道键下线连接：弹出实例、清理 app_id 追踪、停止连接并释放其租约。
        channel = self._channels.pop(channel_key, None)
        if not channel:
            return

        old_app_id = getattr(channel.config, "app_id", None)
        if old_app_id and self._active_app_ids.get(old_app_id) == channel_key:
            del self._active_app_ids[old_app_id]

        try:
            await channel.stop()
        except Exception as e:
            logger.error(f"Error stopping Feishu client {channel_key}: {e}")

        if old_app_id:
            await self._release_lease(old_app_id)

    async def _has_cluster_lease(self, config: dict[str, Any] | None) -> bool:
        # 判断某 app_id 的租约当前是否被集群中任意节点持有（用于跨节点连接探测）。
        if not config or not config.get("enabled", True):
            return False
        app_id = config.get("app_id") or ""
        if not app_id:
            return False
        try:
            redis = self._get_lease_redis()
            return bool(await redis.get(self._lease_key(app_id)))
        except Exception as e:
            logger.warning("[Feishu] Failed to read lease for app_id=%s: %s", app_id, e)
            return False

    @staticmethod
    def _lease_key(app_id: str) -> str:
        # 拼接租约键。
        return f"{_FEISHU_LEASE_PREFIX}:{app_id}"

    async def _acquire_lease(self, app_id: str) -> bool:
        # 用 SET NX + EX 原子抢占租约：只有键不存在时才写入成功（即抢到）。
        try:
            redis = self._get_lease_redis()
            claimed = await redis.set(
                self._lease_key(app_id),
                self._instance_id,
                nx=True,
                ex=_FEISHU_LEASE_TTL_SECONDS,
            )
            return bool(claimed)
        except Exception as e:
            logger.warning("[Feishu] Failed to acquire lease for app_id=%s: %s", app_id, e)
            return False

    def _ensure_lease_refresh_task(self, app_id: str) -> None:
        # 为某 app_id 启动一个后台续约任务（同一 app_id 只启一个）。
        if app_id in self._lease_tasks:
            return

        async def _refresh() -> None:
            # 定期续约：每隔 REFRESH_INTERVAL 用 Lua 脚本"仅持有者可续期"续 TTL。
            # 一旦续约失败（说明租约已被他人接管或丢失），立即停掉本地连接以让出所有权。
            try:
                redis = self._get_lease_redis()
                while True:
                    await asyncio.sleep(_FEISHU_LEASE_REFRESH_INTERVAL)
                    # 该 app_id 已不再活跃则结束续约任务。
                    if app_id not in self._active_app_ids:
                        return
                    refreshed = await redis.eval(
                        _REFRESH_LEASE_LUA,
                        1,
                        self._lease_key(app_id),
                        self._instance_id,
                        _FEISHU_LEASE_TTL_SECONDS,
                    )
                    if not refreshed:
                        logger.warning(
                            "[Feishu] Lost lease refresh for app_id=%s on instance=%s",
                            app_id,
                            self._instance_id,
                        )
                        await self._stop_channel_after_lost_lease(app_id)
                        return
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("[Feishu] Lease refresh failed for app_id=%s: %s", app_id, e)
                await self._stop_channel_after_lost_lease(app_id)
            finally:
                self._lease_tasks.pop(app_id, None)

        self._lease_tasks[app_id] = asyncio.create_task(_refresh())

    async def _stop_channel_after_lost_lease(self, app_id: str) -> None:
        # 丢失租约后的收尾：找到对应连接并停掉，让出该 app_id 给新的持有节点。
        channel_key = self._active_app_ids.pop(app_id, None)
        if not channel_key:
            return

        channel = self._channels.pop(channel_key, None)
        if not channel:
            return

        try:
            await channel.stop()
            logger.warning(
                "[Feishu] Stopped local channel '%s' after losing lease for app_id=%s",
                channel_key,
                app_id,
            )
        except Exception as e:
            logger.error(
                "[Feishu] Failed to stop channel '%s' after losing lease for app_id=%s: %s",
                channel_key,
                app_id,
                e,
            )

    async def _release_lease(self, app_id: str) -> None:
        # 释放租约：先取消该 app_id 的续约任务，再用 Lua 脚本"仅持有者可删除"归还租约。
        task = self._lease_tasks.pop(app_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            redis = self._get_lease_redis()
            await redis.eval(_RELEASE_LEASE_LUA, 1, self._lease_key(app_id), self._instance_id)
        except Exception as e:
            logger.warning("[Feishu] Failed to release lease for app_id=%s: %s", app_id, e)

    def _cancel_all_lease_tasks(self) -> None:
        # 取消全部续约任务（不等待，用于快速清理）。
        for app_id in list(self._lease_tasks.keys()):
            task = self._lease_tasks.pop(app_id, None)
            if task and not task.done():
                task.cancel()

    async def _release_all_leases(self) -> None:
        # 归还本节点持有的所有租约（停机时调用）。
        for app_id in list(self._active_app_ids.keys()):
            await self._release_lease(app_id)

    def _get_lease_redis(self):
        # 惰性创建"独立连接池"的 Redis 客户端：租约相关操作与业务 Redis 隔离，
        # 避免相互影响（如阻塞命令占用连接）。
        if self._lease_redis is None:
            self._lease_redis = create_redis_client(isolated_pool=True)
        return self._lease_redis

    async def _close_lease_redis(self) -> None:
        # 关闭并释放租约专用 Redis 客户端。
        if self._lease_redis is None:
            return
        try:
            await self._lease_redis.aclose()
        except Exception as e:
            logger.warning("[Feishu] Failed to close lease redis client: %s", e)
        finally:
            self._lease_redis = None


# Global instance
# 进程级全局飞书渠道管理器单例。
_feishu_channel_manager: Optional[FeishuChannelManager] = None


def get_feishu_channel_manager() -> FeishuChannelManager:
    """Get the global Feishu channel manager instance."""
    global _feishu_channel_manager
    if _feishu_channel_manager is None:
        _feishu_channel_manager = FeishuChannelManager()
    return _feishu_channel_manager


async def start_feishu_channels(message_handler=None) -> None:
    """Start the Feishu channel manager with all enabled user bots."""
    # 便捷入口：取全局管理器、注入消息回调并启动。
    manager = get_feishu_channel_manager()
    manager._message_handler = message_handler
    await manager.start()


async def stop_feishu_channels() -> None:
    """Stop the Feishu channel manager."""
    # 停止并释放全局管理器，并顺带关闭注册流程的 HTTP 会话（清理资源）。
    global _feishu_channel_manager
    if _feishu_channel_manager:
        await _feishu_channel_manager.stop()
        _feishu_channel_manager = None
    from src.infra.channel.feishu.registration import close_registration_sessions

    close_registration_sessions()

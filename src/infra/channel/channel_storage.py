"""Generic channel configuration storage using MongoDB.

Stores user-level channel configurations with encrypted sensitive fields.
Supports multiple channel types (Feishu, WeChat, DingTalk, etc.)
"""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 通用的"渠道配置"持久化存储（MongoDB，集合 user_channel_configs）。相比旧版
# feishu/storage.py（只存飞书、每用户一份），本模块面向所有渠道类型，且支持同一
# 用户在同一渠道类型下拥有多份配置（多实例，以 instance_id 区分）。
# 核心职责：
#   - 敏感字段（app_secret / token / ... 见 SENSITIVE_FIELDS）落库前加密、对外脱敏；
#   - 提供 CRUD、按用户/类型的列表与计数，以及供渠道管理器批量拉取"已启用配置"的游标；
#   - 进程内"仅建一次索引"（唯一索引保证 user + type + instance 唯一）。
# 加解密为阻塞操作，统一经 run_blocking_io 丢线程池执行，避免阻塞事件循环。
# 关键依赖：get_mongo_client、encrypt_value / decrypt_value、ChannelType 等 schema。
# ============================================================================

import asyncio
import types
import uuid
from typing import Any, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.mcp.encryption import decrypt_value, encrypt_value
from src.infra.storage.mongodb import get_mongo_client
from src.infra.utils.datetime import utc_now_iso
from src.kernel.config import settings
from src.kernel.schemas.channel import (
    ChannelConfigResponse,
    ChannelConfigStatus,
    ChannelType,
)

logger = get_logger(__name__)

# Fields that should be encrypted
# 需落库前加密、展示时脱敏的敏感字段名集合（app_secret/token/密码等）。
SENSITIVE_FIELDS = frozenset(
    {"app_secret", "secret", "token", "password", "api_key", "access_token"}
)
# 列表类查询的最大返回条数上限，防止单用户配置过多时一次性拉全表。
CHANNEL_CONFIG_LIST_LIMIT = 200


class ChannelStorage:
    """
    Generic channel configuration storage.

    Stores per-user channel configurations in MongoDB.
    Each user can have multiple configurations per channel type (multi-instance support).
    """

    # 以下三个类级变量共同实现"每进程仅建一次索引"：
    # _indexes_done 标记是否已成功建立；_indexes_task 缓存正在执行的建索引协程，
    # 使并发调用共享同一个任务；_indexes_lock 保护上述状态避免竞态。
    _indexes_done = False
    _indexes_task: asyncio.Task | None = None
    _indexes_lock: asyncio.Lock | None = None

    def __init__(self):
        # MongoDB 客户端与集合句柄均惰性初始化（见 _get_collection）。
        self._client = None
        self._collection = None

    def _get_collection(self):
        """Get channel config collection lazily"""
        # 首次访问时才建立连接并选择集合，之后复用同一句柄。
        if self._collection is None:
            self._client = get_mongo_client()
            db = self._client[settings.MONGODB_DB]
            self._collection = db["user_channel_configs"]
        return self._collection

    async def ensure_indexes_if_needed(self) -> None:
        """Ensure channel indexes exist once per process."""
        # 双重检查加锁：先无锁快速判断，已完成则直接返回。
        cls = type(self)
        if cls._indexes_done:
            return

        # 惰性创建类级锁（避免在模块导入期就绑定事件循环）。
        if cls._indexes_lock is None:
            cls._indexes_lock = asyncio.Lock()

        async with cls._indexes_lock:
            # 进入临界区后二次确认，防止等锁期间别的协程已建好。
            if cls._indexes_done:
                return
            # 复用同一个建索引任务：并发调用者都 await 这同一个 task，避免重复建索引。
            if cls._indexes_task is None or cls._indexes_task.cancelled():
                cls._indexes_task = asyncio.create_task(self._ensure_indexes())
            task = cls._indexes_task

        # 在锁外等待任务完成，成功才置位 _indexes_done。
        succeeded = await task
        if succeeded:
            cls._indexes_done = True
            return

        # 失败则清理任务引用，使后续调用可重试建索引。
        async with cls._indexes_lock:
            if cls._indexes_task is task:
                cls._indexes_task = None

    async def _ensure_indexes(self) -> bool:
        try:
            collection = self._get_collection()
            # 唯一索引：保证同一用户+渠道类型+实例只有一条配置（多实例的唯一键）。
            await collection.create_index(
                [("user_id", 1), ("channel_type", 1), ("instance_id", 1)],
                name="user_channel_instance_idx",
                unique=True,
                background=True,
            )
            # 辅助索引：加速渠道管理器按"类型+是否启用"批量拉取待启动配置。
            await collection.create_index(
                [("channel_type", 1), ("enabled", 1)],
                name="channel_enabled_idx",
                background=True,
            )
            return True
        except Exception as e:
            # 建索引失败不抛出（不阻断主流程），仅告警并由上层决定是否重试。
            logger.warning(f"Failed to create channel indexes: {e}")
            return False

    # 读取某用户在指定渠道类型（可选实例）下的配置：命中则解密并展开为扁平字典，
    # 未命中返回 None。
    async def get_config(
        self,
        user_id: str,
        channel_type: ChannelType,
        instance_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Get channel configuration for a user and optionally instance"""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()

        query: dict[str, Any] = {"user_id": user_id, "channel_type": channel_type.value}
        if instance_id:
            query["instance_id"] = instance_id

        doc = await collection.find_one(query)
        if doc:
            return await self._doc_to_config(doc)
        return None

    # 新建一份渠道配置：自动生成唯一 instance_id，敏感字段加密后连同绑定关系
    # （agent / model / project / team / persona）与时间戳一并写入，返回解密后的配置字典。
    async def create_config(
        self,
        user_id: str,
        channel_type: ChannelType,
        config: dict[str, Any],
        name: str,
        enabled: bool = True,
        agent_id: str | None = None,
        model_id: str | None = None,
        project_id: str | None = None,
        team_id: str | None = None,
        persona_preset_id: str | None = None,
    ) -> dict[str, Any]:
        """Create channel configuration for a user"""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()

        # Generate unique instance_id
        # 生成唯一实例 ID，作为该配置在"多实例"场景下的稳定标识。
        instance_id = str(uuid.uuid4())

        now = utc_now_iso()
        doc = {
            "user_id": user_id,
            "channel_type": channel_type.value,
            "instance_id": instance_id,
            "name": name,
            # 敏感字段在写入前加密（见 _encrypt_config）。
            "config": await self._encrypt_config(config),
            "enabled": enabled,
            "agent_id": agent_id,
            "model_id": model_id,
            "project_id": project_id,
            "team_id": team_id,
            "persona_preset_id": persona_preset_id,
            "created_at": now,
            "updated_at": now,
        }

        await collection.insert_one(doc)
        logger.info(
            f"Created {channel_type.value} config '{name}' ({instance_id}) for user {user_id}"
        )

        return await self._doc_to_config(doc)

    # 更新指定实例的配置：不存在返回 None。enabled/name 传 None 表示"不改"；
    # 而 agent_id/model_id/... 用 Ellipsis(...) 作"未传参"哨兵，从而把"显式清空(None)"
    # 与"保持不变"区分开（见下方逐字段判断）。更新后回读并返回最新配置。
    async def update_config(
        self,
        user_id: str,
        channel_type: ChannelType,
        config: dict[str, Any],
        instance_id: str,
        enabled: Optional[bool] = None,
        name: Optional[str] = None,
        agent_id: Optional[str] | types.EllipsisType = ...,
        model_id: Optional[str] | types.EllipsisType = ...,
        project_id: Optional[str] | types.EllipsisType = ...,
        team_id: Optional[str] | types.EllipsisType = ...,
        persona_preset_id: Optional[str] | types.EllipsisType = ...,
    ) -> Optional[dict[str, Any]]:
        """Update channel configuration for a user"""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()

        doc = await collection.find_one(
            {"user_id": user_id, "channel_type": channel_type.value, "instance_id": instance_id}
        )
        if not doc:
            return None

        update_data: dict[str, Any] = {
            "updated_at": utc_now_iso(),
            "config": await self._encrypt_config(config),
        }

        if enabled is not None:
            update_data["enabled"] = enabled
        if name is not None:
            update_data["name"] = name
        # 这里用 Ellipsis(...) 作为"未传参"哨兵，从而把"显式传 None（清空）"
        # 与"根本没传该参数（保持不变）"区分开：只有传了才写入 update_data。
        if agent_id is not ...:
            update_data["agent_id"] = agent_id
        if model_id is not ...:
            update_data["model_id"] = model_id
        if project_id is not ...:
            update_data["project_id"] = project_id
        if team_id is not ...:
            update_data["team_id"] = team_id
        if persona_preset_id is not ...:
            update_data["persona_preset_id"] = persona_preset_id

        await collection.update_one(
            {"user_id": user_id, "channel_type": channel_type.value, "instance_id": instance_id},
            {"$set": update_data},
        )
        logger.info(f"Updated {channel_type.value} config ({instance_id}) for user {user_id}")

        updated_doc = await collection.find_one(
            {"user_id": user_id, "channel_type": channel_type.value, "instance_id": instance_id}
        )
        return await self._doc_to_config(updated_doc) if updated_doc else None

    # 删除配置：给定实例则精确删该实例，否则删该用户该类型下匹配的一条；删掉返回 True。
    async def delete_config(
        self,
        user_id: str,
        channel_type: ChannelType,
        instance_id: Optional[str] = None,
    ) -> bool:
        """Delete channel configuration for a user"""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()

        query: dict[str, Any] = {"user_id": user_id, "channel_type": channel_type.value}
        if instance_id:
            query["instance_id"] = instance_id

        result = await collection.delete_one(query)

        if result.deleted_count > 0:
            logger.info(f"Deleted {channel_type.value} config ({instance_id}) for user {user_id}")
            return True
        return False

    # 批量清除某用户下所有引用了该 project_id 的配置（置空 project 绑定），
    # 用于项目被删除时的级联清理；返回被修改的条数。
    async def clear_project_id(self, project_id: str, user_id: str) -> int:
        """Clear a project reference from channel configurations for a user."""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()
        result = await collection.update_many(
            {"user_id": user_id, "project_id": project_id},
            {
                "$set": {
                    "project_id": None,
                    "updated_at": utc_now_iso(),
                }
            },
        )
        return result.modified_count

    # 清除单条配置的 project 绑定（把该实例的 project_id 置空），返回被修改的条数。
    async def clear_config_project_id(
        self, user_id: str, channel_type: ChannelType, instance_id: str
    ) -> int:
        """Clear the project reference for one channel configuration."""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()
        result = await collection.update_one(
            {"user_id": user_id, "channel_type": channel_type.value, "instance_id": instance_id},
            {
                "$set": {
                    "project_id": None,
                    "updated_at": utc_now_iso(),
                }
            },
        )
        return result.modified_count

    # 取配置并构建对外响应（敏感字段脱敏）：无配置返回 None。
    async def get_response(
        self,
        user_id: str,
        channel_type: ChannelType,
        instance_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[ChannelConfigResponse]:
        """Get channel configuration response (with masked sensitive fields)"""
        config = await self.get_config(user_id, channel_type, instance_id)
        if not config:
            return None

        return self.build_response_from_config(config, channel_type, user_id, metadata)

    def build_response_from_config(
        self,
        config: dict[str, Any],
        channel_type: ChannelType,
        user_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ChannelConfigResponse:
        """Build a response from an already loaded config."""

        # Get sensitive field names from metadata
        # 敏感字段 = 通用集合 SENSITIVE_FIELDS 叠加该渠道元数据中标注 sensitive 的字段，
        # 二者合并后统一做脱敏处理。
        sensitive_fields = set(SENSITIVE_FIELDS)
        if metadata:
            for field in metadata.get("config_fields", []):
                if field.get("sensitive"):
                    sensitive_fields.add(field["name"])

        # 生成脱敏后的配置（敏感值替换为 ***），避免明文回传前端。
        masked_config = self._mask_config(config, sensitive_fields)

        return ChannelConfigResponse(
            id=config.get("instance_id", ""),
            channel_type=channel_type,
            name=config.get("name", ""),
            user_id=user_id,
            enabled=config.get("enabled", True),
            config=masked_config,
            capabilities=metadata.get("capabilities", []) if metadata else [],
            agent_id=config.get("agent_id"),
            model_id=config.get("model_id"),
            project_id=config.get("project_id"),
            team_id=config.get("team_id"),
            persona_preset_id=config.get("persona_preset_id"),
            created_at=config.get("created_at"),
            updated_at=config.get("updated_at"),
        )

    # 取配置并构建连接状态对象：无配置视为未启用、未连接。
    async def get_status(
        self,
        user_id: str,
        channel_type: ChannelType,
        instance_id: Optional[str] = None,
    ) -> ChannelConfigStatus:
        """Get channel connection status for a user"""
        config = await self.get_config(user_id, channel_type, instance_id)
        if not config:
            return ChannelConfigStatus(channel_type=channel_type, enabled=False, connected=False)

        return self.build_status_from_config(config, channel_type)

    # 从已加载的配置构建状态对象：connected 恒为 False，真实连接态由渠道管理器另行填充。
    def build_status_from_config(
        self,
        config: dict[str, Any],
        channel_type: ChannelType,
    ) -> ChannelConfigStatus:
        """Build a status object from an already loaded config."""

        return ChannelConfigStatus(
            channel_type=channel_type,
            enabled=config.get("enabled", True),
            connected=False,  # Will be updated by channel manager
        )

    async def list_user_configs(self, user_id: str) -> list[dict[str, Any]]:
        """List all channel configurations for a user"""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()
        configs = []
        # 限制返回条数（CHANNEL_CONFIG_LIST_LIMIT）防止极端情况下拉取过多文档。
        async for doc in collection.find({"user_id": user_id}).limit(CHANNEL_CONFIG_LIST_LIMIT):
            configs.append(await self._doc_to_config(doc))
        return configs

    # 列出某用户在指定渠道类型下的所有配置（限量返回）。
    async def list_user_configs_by_type(
        self, user_id: str, channel_type: ChannelType
    ) -> list[dict[str, Any]]:
        """List channel configurations for a user and channel type."""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()
        configs = []
        async for doc in collection.find(
            {"user_id": user_id, "channel_type": channel_type.value}
        ).limit(CHANNEL_CONFIG_LIST_LIMIT):
            configs.append(await self._doc_to_config(doc))
        return configs

    # 统计某用户的配置总数（只计数、不加载/解密配置内容，避免无谓开销）。
    async def count_user_configs(self, user_id: str) -> int:
        """Count channel configurations for a user without loading config payloads."""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()
        return int(await collection.count_documents({"user_id": user_id}))

    # 统计某用户在指定渠道类型下的配置数（同样只计数、不加载内容）。
    async def count_user_configs_by_type(self, user_id: str, channel_type: ChannelType) -> int:
        """Count channel configurations for a user and type without loading payloads."""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()
        return int(
            await collection.count_documents(
                {"user_id": user_id, "channel_type": channel_type.value}
            )
        )

    # 列出某渠道类型下所有已启用的配置（把 iter_enabled_configs 游标物化成列表）。
    async def list_enabled_configs(self, channel_type: ChannelType) -> list[dict[str, Any]]:
        """List all enabled configurations for a channel type (for channel manager)"""
        configs = []
        async for config in self.iter_enabled_configs(channel_type):
            configs.append(config)
        return configs

    async def iter_enabled_configs(self, channel_type: ChannelType):
        """Iterate enabled configurations for a channel type without materializing all rows."""
        await self.ensure_indexes_if_needed()
        collection = self._get_collection()
        # 用游标逐条产出，避免一次性把所有启用配置读进内存（供渠道管理器启动时遍历）。
        cursor = collection.find({"channel_type": channel_type.value, "enabled": True}).limit(
            CHANNEL_CONFIG_LIST_LIMIT
        )
        async for doc in cursor:
            yield await self._doc_to_config(doc)

    async def _encrypt_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Encrypt sensitive fields in config"""
        encrypted = {}
        for key, value in config.items():
            # 仅对敏感且为非空字符串的字段加密；加密是阻塞型 CPU 操作，
            # 放到线程池（run_blocking_io）执行以免阻塞事件循环。
            if key in SENSITIVE_FIELDS and isinstance(value, str) and value:
                encrypted[key] = await run_blocking_io(encrypt_value, {"value": value})
            else:
                encrypted[key] = value
        return encrypted

    async def _decrypt_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Decrypt sensitive fields in config"""
        from src.infra.mcp.encryption import DecryptionError

        decrypted = {}
        for key, value in config.items():
            if key in SENSITIVE_FIELDS and value:
                if isinstance(value, dict):
                    # Encrypted value
                    # 密文以 dict 形式存储；解密同样是阻塞操作，交给线程池执行。
                    try:
                        dec = await run_blocking_io(decrypt_value, value)
                        if isinstance(dec, dict):
                            decrypted[key] = dec.get("value", "")
                        else:
                            decrypted[key] = dec
                    except DecryptionError as e:
                        # 解密失败通常意味着加密密钥已更换：不报错中断，
                        # 而是置 None 标记需重新填写，并提示用户重新保存配置。
                        logger.warning(
                            f"Failed to decrypt field '{key}': {e}. "
                            "Config may have been encrypted with a different key. "
                            "Please re-save the channel configuration."
                        )
                        decrypted[key] = None  # Mark as needing re-entry
                else:
                    # 非 dict 说明是历史明文数据，原样保留。
                    decrypted[key] = value
            else:
                decrypted[key] = value
        return decrypted

    def _mask_config(self, config: dict[str, Any], sensitive_fields: set[str]) -> dict[str, Any]:
        """Mask sensitive fields in config for display"""
        masked = {}
        for key, value in config.items():
            # 敏感字段：有值统一显示为 ***，无值显示空串；非敏感字段原样透出。
            if key in sensitive_fields:
                if value:
                    masked[key] = "***"
                else:
                    masked[key] = ""
            else:
                masked[key] = value
        return masked

    async def _doc_to_config(self, doc: dict) -> dict[str, Any]:
        """Convert MongoDB document to config dict"""
        # 将存储文档转为扁平配置字典：先解密 config 子文档，
        # 再把解密结果与顶层元字段（名称/绑定的 agent、model、project 等）合并展开。
        config = doc.get("config", {})
        decrypted_config = await self._decrypt_config(config)

        return {
            "user_id": doc.get("user_id"),  # Include user_id from document
            "channel_type": doc.get("channel_type"),
            "instance_id": doc.get("instance_id"),
            "name": doc.get("name"),
            **decrypted_config,
            "enabled": doc.get("enabled", True),
            "agent_id": doc.get("agent_id"),
            "model_id": doc.get("model_id"),
            "project_id": doc.get("project_id"),
            "team_id": doc.get("team_id"),
            "persona_preset_id": doc.get("persona_preset_id"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        }

    async def close(self):
        """Close MongoDB connection (only clears local refs, does not close global client)"""
        # 只置空本地句柄；全局 Mongo 客户端由外部统一管理，不在此关闭。
        self._collection = None

"""
Feishu/Lark configuration storage using MongoDB

Stores user-level Feishu bot configurations with encrypted sensitive fields.
"""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 飞书机器人配置的持久化存储（MongoDB，集合 user_feishu_configs）。这是"每个用户
# 一份飞书配置"的旧版存储；新版支持多实例的通用配置存储见
# channel_storage.ChannelStorage。
# 敏感字段（app_secret）落库前加密、对外响应时脱敏；加解密复用 MCP 的实现，并放到
# 线程池执行以免阻塞事件循环。对外提供：CRUD、对外响应/状态对象构建，以及供渠道
# 管理器启动时批量拉取"已启用配置"的接口。
# 关键依赖：get_mongo_client、encrypt_value/decrypt_value、run_blocking_io、
# FeishuConfig 及相关 schema。
# ============================================================================

from datetime import datetime
from typing import Any, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.mcp.encryption import decrypt_value, encrypt_value
from src.infra.storage.mongodb import get_mongo_client
from src.infra.utils.datetime import utc_now_iso
from src.kernel.config import settings
from src.kernel.schemas.feishu import (
    DEFAULT_AUDIO_TRANSCRIBE_PROMPT,
    FeishuConfig,
    FeishuConfigCreate,
    FeishuConfigResponse,
    FeishuConfigStatus,
    FeishuConfigUpdate,
    FeishuGroupPolicy,
)

logger = get_logger(__name__)

# 列表查询返回条数上限。
FEISHU_CONFIG_LIST_LIMIT = 200


class FeishuStorage:
    """
    Feishu configuration storage

    Stores per-user Feishu bot configurations in MongoDB.
    Each user can have their own Feishu bot configuration.
    """

    def __init__(self):
        # Mongo 客户端与集合句柄惰性初始化。
        self._client = None
        self._collection = None

    def _get_collection(self):
        """Get Feishu config collection lazily"""
        # 首次访问时建立连接并选中集合；这是旧版"每用户单份飞书配置"的集合。
        if self._collection is None:
            self._client = get_mongo_client()
            db = self._client[settings.MONGODB_DB]
            self._collection = db["user_feishu_configs"]
        return self._collection

    # 读取某用户的飞书配置：查不到返回 None，查到则解密敏感字段并转成 FeishuConfig。
    async def get_config(self, user_id: str) -> Optional[FeishuConfig]:
        """Get Feishu configuration for a user"""
        collection = self._get_collection()
        doc = await collection.find_one({"user_id": user_id})
        if doc:
            return await self._doc_to_config(doc)
        return None

    # 创建配置：每用户仅允许一份，已存在则抛 ValueError；app_secret 加密后落库，
    # 写入创建/更新时间戳，最后返回解密后的 FeishuConfig。
    async def create_config(self, config: FeishuConfigCreate, user_id: str) -> FeishuConfig:
        """Create Feishu configuration for a user"""
        collection = self._get_collection()

        # Check if config already exists
        # 该集合约束每用户仅一份配置：已存在则直接报错，避免重复创建。
        existing = await collection.find_one({"user_id": user_id})
        if existing:
            raise ValueError("Feishu configuration already exists for this user")

        now = utc_now_iso()
        doc = {
            "user_id": user_id,
            "app_id": config.app_id,
            # app_secret 落库前加密。
            "app_secret": await self._encrypt_secret(config.app_secret),
            "encrypt_key": config.encrypt_key,
            "verification_token": config.verification_token,
            "react_emoji": config.react_emoji,
            "group_policy": config.group_policy.value,
            "stream_reply": config.stream_reply,
            "auto_transcribe_audio": config.auto_transcribe_audio,
            "audio_transcribe_prompt": config.audio_transcribe_prompt,
            "enabled": config.enabled,
            "created_at": now,
            "updated_at": now,
        }

        await collection.insert_one(doc)
        logger.info(f"Created Feishu config for user {user_id}")

        return await self._doc_to_config(doc)

    # 部分更新配置：配置不存在返回 None；仅对显式传入（非 None）的字段写入 $set，
    # app_secret 若变更会重新加密；更新后回读并返回最新的 FeishuConfig。
    async def update_config(
        self, user_id: str, updates: FeishuConfigUpdate
    ) -> Optional[FeishuConfig]:
        """Update Feishu configuration for a user"""
        collection = self._get_collection()

        doc = await collection.find_one({"user_id": user_id})
        if not doc:
            return None

        update_data: dict[str, Any] = {"updated_at": utc_now_iso()}

        # 仅对显式传入（非 None）的字段做部分更新；app_secret 变更时重新加密。
        if updates.app_id is not None:
            update_data["app_id"] = updates.app_id
        if updates.app_secret is not None:
            update_data["app_secret"] = await self._encrypt_secret(updates.app_secret)
        if updates.encrypt_key is not None:
            update_data["encrypt_key"] = updates.encrypt_key
        if updates.verification_token is not None:
            update_data["verification_token"] = updates.verification_token
        if updates.react_emoji is not None:
            update_data["react_emoji"] = updates.react_emoji
        if updates.group_policy is not None:
            update_data["group_policy"] = updates.group_policy.value
        if updates.stream_reply is not None:
            update_data["stream_reply"] = updates.stream_reply
        if updates.auto_transcribe_audio is not None:
            update_data["auto_transcribe_audio"] = updates.auto_transcribe_audio
        if updates.audio_transcribe_prompt is not None:
            update_data["audio_transcribe_prompt"] = updates.audio_transcribe_prompt
        if updates.enabled is not None:
            update_data["enabled"] = updates.enabled

        await collection.update_one({"user_id": user_id}, {"$set": update_data})
        logger.info(f"Updated Feishu config for user {user_id}")

        updated_doc = await collection.find_one({"user_id": user_id})
        return await self._doc_to_config(updated_doc) if updated_doc else None

    # 删除某用户的飞书配置：确实删掉一条时返回 True，否则返回 False。
    async def delete_config(self, user_id: str) -> bool:
        """Delete Feishu configuration for a user"""
        collection = self._get_collection()
        result = await collection.delete_one({"user_id": user_id})

        if result.deleted_count > 0:
            logger.info(f"Deleted Feishu config for user {user_id}")
            return True
        return False

    # 构建对外配置响应：无配置返回 None；有配置则对敏感字段脱敏后返回（详见下方）。
    async def get_response(self, user_id: str) -> Optional[FeishuConfigResponse]:
        """Get Feishu configuration response (with masked sensitive fields)"""
        config = await self.get_config(user_id)
        if not config:
            return None

        # 对外响应脱敏：不回传明文密钥，只用 has_app_secret 标识是否已设置，
        # encrypt_key/verification_token 有值则以 *** 占位。
        return FeishuConfigResponse(
            user_id=config.user_id,
            app_id=config.app_id,
            has_app_secret=bool(config.app_secret),
            encrypt_key="***" if config.encrypt_key else "",
            verification_token="***" if config.verification_token else "",
            react_emoji=config.react_emoji,
            group_policy=config.group_policy,
            stream_reply=config.stream_reply,
            auto_transcribe_audio=config.auto_transcribe_audio,
            audio_transcribe_prompt=config.audio_transcribe_prompt,
            enabled=config.enabled,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )

    # 构建连接状态：无配置视为未启用、未连接；connected 恒为 False，真实连接态
    # 由渠道管理器另行填充（见下方 TODO）。
    async def get_status(self, user_id: str) -> FeishuConfigStatus:
        """Get Feishu connection status for a user"""
        config = await self.get_config(user_id)
        if not config:
            return FeishuConfigStatus(enabled=False, connected=False)

        # TODO: Check actual connection status from channel manager
        return FeishuConfigStatus(
            enabled=config.enabled,
            connected=False,  # Will be updated by channel manager
        )

    # 列出所有已启用的飞书配置（供渠道管理器启动时逐一拉起连接），限量返回。
    async def list_enabled_configs(self) -> list[FeishuConfig]:
        """List all enabled Feishu configurations (for channel manager)"""
        collection = self._get_collection()
        configs = []
        async for doc in collection.find({"enabled": True}).limit(FEISHU_CONFIG_LIST_LIMIT):
            configs.append(await self._doc_to_config(doc))
        return configs

    async def _encrypt_secret(self, secret: str) -> dict[str, Any] | str:
        """Encrypt a secret string"""
        if not secret:
            return ""
        # Use the same encryption as MCP
        # 复用 MCP 的加密实现；加密是阻塞操作，放到线程池执行。
        return await run_blocking_io(encrypt_value, {"value": secret})

    async def _decrypt_secret(self, encrypted: dict | str) -> str:
        """Decrypt a secret string"""
        if not encrypted:
            return ""
        # 兼容历史明文数据：字符串形态直接返回，dict 形态才解密。
        if isinstance(encrypted, str):
            return encrypted  # Legacy unencrypted
        decrypted = await run_blocking_io(decrypt_value, encrypted)
        if isinstance(decrypted, dict):
            return decrypted.get("value", "")
        return ""

    async def _doc_to_config(self, doc: dict) -> FeishuConfig:
        """Convert MongoDB document to FeishuConfig"""
        # 时间字段以 ISO 字符串存储；解析时把末尾的 Z 转为 +00:00 以便 fromisoformat 识别。
        created_at = doc.get("created_at")
        updated_at = doc.get("updated_at")

        if created_at and isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if updated_at and isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))

        return FeishuConfig(
            user_id=doc["user_id"],
            app_id=doc["app_id"],
            app_secret=await self._decrypt_secret(doc.get("app_secret", "")),
            encrypt_key=doc.get("encrypt_key", ""),
            verification_token=doc.get("verification_token", ""),
            react_emoji=doc.get("react_emoji", "THUMBSUP"),
            group_policy=FeishuGroupPolicy(doc.get("group_policy", "mention")),
            stream_reply=doc.get("stream_reply", True),
            auto_transcribe_audio=doc.get("auto_transcribe_audio", True),
            audio_transcribe_prompt=doc.get(
                "audio_transcribe_prompt", DEFAULT_AUDIO_TRANSCRIBE_PROMPT
            )
            or DEFAULT_AUDIO_TRANSCRIBE_PROMPT,
            enabled=doc.get("enabled", True),
            created_at=created_at,
            updated_at=updated_at,
        )

    # 只清空本地句柄；全局 Mongo 客户端由外部统一管理，不在此处关闭。
    async def close(self):
        """Clear local MongoDB references without closing the global client."""
        self._client = None
        self._collection = None

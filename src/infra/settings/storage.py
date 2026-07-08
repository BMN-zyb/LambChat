"""
Settings storage using MongoDB
"""

from typing import Any, Optional

from src.infra.utils.datetime import utc_now_iso
from src.kernel.config import (
    RESTART_REQUIRED_SETTINGS,
    SETTING_DEFINITIONS,
    _get_default_from_settings,
    settings,
)
from src.kernel.schemas.setting import SettingItem


class SettingsStorage:
    """Settings storage using MongoDB"""

    # 直接操作 MongoDB 的 "system_settings" collection，每个文档的 _id 就是配置 key；
    # 本类不做缓存，每次调用都查库——缓存/单例/DB 与 .env 的优先级仲裁职责交给上层 SettingsService。
    def __init__(self):
        self._client = None
        self._collection = None

    def _get_collection(self):
        """Get MongoDB collection lazily"""
        if self._collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            # 复用全局共享的 motor 客户端，不为配置存储单独开一套连接
            self._client = get_mongo_client()
            db = self._client[settings.MONGODB_DB]
            self._collection = db["system_settings"]
        return self._collection

    async def get_all(
        self, admin_mode: bool = False, mask_sensitive: bool = True
    ) -> dict[str, list[SettingItem]]:
        """Get all settings grouped by category

        Args:
            admin_mode: If True, return all settings.
                       If False, only return frontend_visible settings.
            mask_sensitive: If True, mask sensitive values with ********.
                           If False, return actual values (for internal use).
        """
        collection = self._get_collection()
        setting_keys = list(SETTING_DEFINITIONS.keys())
        # 一次性把所有已定义配置 key 在 DB 中的覆盖记录查出来；
        # 某个 key 若未出现在查询结果里，说明它尚未被覆盖，仍应使用默认值
        cursor = collection.find(
            {"_id": {"$in": setting_keys}},
            {
                "_id": 1,
                "value": 1,
                "updated_at": 1,
                "updated_by": 1,
            },
        )
        db_settings = {doc["_id"]: doc for doc in await cursor.to_list(length=len(setting_keys))}

        result: dict[str, list[SettingItem]] = {}

        # 以 SETTING_DEFINITIONS（唯一权威来源）为骨架遍历，逐项决定是否展示、取哪个值、是否脱敏
        for key, definition in SETTING_DEFINITIONS.items():
            # Filter non-admin users
            # 非管理员模式下过滤掉未标记为前端可见的配置项（比如更底层/更敏感的运维配置）
            if not admin_mode and not definition.get("frontend_visible", False):
                continue

            category = definition["category"].value
            if category not in result:
                result[category] = []

            # Get default from SETTING_DEFINITIONS (single source of truth)
            default_value = _get_default_from_settings(key, SETTING_DEFINITIONS)

            # Use DB value if exists, otherwise use default
            # DB 中存在覆盖值就用 DB 值，否则回退到配置定义里的默认值
            db_doc = db_settings.get(key)
            value = db_doc["value"] if db_doc else default_value

            is_sensitive = definition.get("is_sensitive", False)

            # Mask sensitive settings in API responses
            # 敏感配置（如 API Key）需要脱敏时统一替换为固定占位符，避免真实值泄露到前端
            if mask_sensitive and is_sensitive and value:
                value = "********"

            item = SettingItem(
                key=key,
                value=value,
                type=definition["type"],
                category=definition["category"],
                subcategory=definition.get("subcategory", ""),
                description=definition["description"],
                default_value=default_value,
                requires_restart=key in RESTART_REQUIRED_SETTINGS,
                is_sensitive=is_sensitive,
                frontend_visible=definition.get("frontend_visible", False),
                depends_on=definition.get("depends_on"),
                options=definition.get("options"),
                json_schema=definition.get("json_schema"),
                updated_at=db_doc.get("updated_at") if db_doc else None,
                updated_by=db_doc.get("updated_by") if db_doc else None,
            )
            result[category].append(item)

        return result

    async def get(self, key: str) -> Optional[SettingItem]:
        """Get single setting by key (with sensitive values masked)"""
        return await self._get_internal(key, mask_sensitive=True)

    async def get_raw(self, key: str) -> Optional[SettingItem]:
        """Get single setting by key (without masking - for internal use only)"""
        # 仅供内部逻辑使用（比如需要拿真实的 API Key 去调用第三方服务），
        # 对外暴露的 HTTP 接口一律应该走上面的 get()，避免敏感值泄露
        return await self._get_internal(key, mask_sensitive=False)

    async def _get_internal(self, key: str, mask_sensitive: bool = True) -> Optional[SettingItem]:
        """Internal method to get setting by key"""
        definition = SETTING_DEFINITIONS.get(key)
        if not definition:
            return None

        collection = self._get_collection()
        doc = await collection.find_one({"_id": key})

        # Get default from SETTING_DEFINITIONS (single source of truth)
        default_value = _get_default_from_settings(key)

        # DB 中有覆盖记录则用 DB 值，否则用默认值（与 get_all 中的单条逻辑一致）
        value = doc["value"] if doc else default_value

        is_sensitive = definition.get("is_sensitive", False)

        # Mask sensitive settings in API responses (if requested)
        if mask_sensitive and is_sensitive and value:
            value = "********"

        return SettingItem(
            key=key,
            value=value,
            type=definition["type"],
            category=definition["category"],
            subcategory=definition.get("subcategory", ""),
            description=definition["description"],
            default_value=default_value,
            requires_restart=key in RESTART_REQUIRED_SETTINGS,
            is_sensitive=is_sensitive,
            frontend_visible=definition.get("frontend_visible", False),
            depends_on=definition.get("depends_on"),
            options=definition.get("options"),
            json_schema=definition.get("json_schema"),
            updated_at=doc.get("updated_at") if doc else None,
            updated_by=doc.get("updated_by") if doc else None,
        )

    async def set(self, key: str, value: Any, user_id: str) -> Optional[SettingItem]:
        """Set setting value"""
        definition = SETTING_DEFINITIONS.get(key)
        if not definition:
            return None

        # Don't allow setting masked values
        # 前端回传的脱敏占位符不能被当作真实值写回，否则会把原值永久覆盖成星号
        if value == "********":
            raise ValueError("Cannot set masked value")

        # Type validation
        # 按配置定义声明的类型做基础校验/转换，防止脏数据写入数据库
        expected_type = definition["type"]
        if expected_type.value == "number":
            if not isinstance(value, (int, float)):
                raise ValueError(f"Setting {key} expects a number")
        elif expected_type.value == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"Setting {key} expects a boolean")
        elif expected_type.value == "string":
            value = str(value)
        elif expected_type.value == "text":
            value = str(value)
        elif expected_type.value == "select":
            valid_options = definition.get("options", [])
            if valid_options and value not in valid_options:
                raise ValueError(f"Setting {key} expects one of: {valid_options}")
            value = str(value)
        elif expected_type.value == "json":
            # JSON type accepts arrays and objects
            if not isinstance(value, (list, dict)):
                raise ValueError(f"Setting {key} expects a JSON array or object")

        collection = self._get_collection()
        now = utc_now_iso()

        # Get default from SETTING_DEFINITIONS (single source of truth)
        default_value = _get_default_from_settings(key)

        # upsert：不存在则插入，存在则整体覆盖；顺带把类型/分类/描述/默认值也写进文档，
        # 这样直接查 Mongo 的运维工具也能看到完整上下文，无需反查 SETTING_DEFINITIONS
        await collection.update_one(
            {"_id": key},
            {
                "$set": {
                    "value": value,
                    "type": expected_type.value,
                    "category": definition["category"].value,
                    "description": definition["description"],
                    "default_value": default_value,
                    "updated_at": now,
                    "updated_by": user_id,
                }
            },
            upsert=True,
        )

        return await self.get(key)

    async def reset(self, key: Optional[str] = None) -> int:
        """Reset settings to default values"""
        collection = self._get_collection()

        if key:
            # 只重置单个 key：删除 DB 中的覆盖记录即可恢复为默认值
            # （get 系列方法在没有 DB 记录时会自动回退到默认值，无需额外写入默认值）
            if key not in SETTING_DEFINITIONS:
                return 0
            result = await collection.delete_one({"_id": key})
            return 1 if result.deleted_count > 0 else 0
        else:
            # Reset all
            # 不指定 key 则重置全部：删除所有已知配置 key 对应的覆盖记录
            keys_to_delete = list(SETTING_DEFINITIONS.keys())
            result = await collection.delete_many({"_id": {"$in": keys_to_delete}})
            return result.deleted_count

    async def close(self):
        """Close MongoDB connection (only clears local refs, does not close global client)"""
        # 仅清空本实例持有的引用；全局共享的 motor client 生命周期由别处统一管理，这里不能真正关闭它
        self._client = None
        self._collection = None


# Re-export for backward compatibility
__all__ = [
    "RESTART_REQUIRED_SETTINGS",
    "SETTING_DEFINITIONS",
    "SettingsStorage",
]

"""
Settings Service - Database-first settings with .env fallback
"""

import json
import os
from typing import Any, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.settings.storage import (
    RESTART_REQUIRED_SETTINGS,
    SETTING_DEFINITIONS,
    SettingsStorage,
)
from src.kernel.schemas.setting import SettingItem, SettingType

# 取值优先级：MongoDB（管理员通过界面/接口修改过的值）> .env 环境变量 > SETTING_DEFINITIONS 中
# 声明的默认值。本类是应用范围内的单例（见 get_instance），持有一个 SettingsStorage 实例负责
# 真正的数据库读写；写入成功后会通过 SettingsPubSub 把变更广播给其他实例，
# 使多实例部署下所有进程的内存态配置最终保持一致。


class SettingsService:
    """
    Database-first settings service.

    Reads settings from MongoDB, falls back to environment variables.
    Handles initialization from .env on first startup.
    """

    _instance: Optional["SettingsService"] = None

    def __init__(self):
        self._storage = SettingsStorage()
        self._initialized = False

    @classmethod
    def get_instance(cls) -> "SettingsService":
        """Get singleton instance"""
        # 懒创建单例：本项目在单事件循环下调用，无需额外加锁
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def initialize(self) -> None:
        """Initialize service and import from .env if needed"""
        # 只在应用启动时真正执行一次：把 .env 中已配置但数据库里还没有记录的配置项导入数据库，
        # 之后同一进程内即使再次调用也会因 _initialized 标记而直接跳过
        if self._initialized:
            return

        # Import any missing settings from environment
        await self.init_from_env()
        self._initialized = True

    async def get(self, key: str) -> Any:
        """
        Get setting value: DB -> .env fallback (with sensitive values masked)

        Args:
            key: Setting key name

        Returns:
            Setting value (sensitive values will be masked)
        """
        # Check if key is valid
        if key not in SETTING_DEFINITIONS:
            # Try environment variable directly
            # 未在 SETTING_DEFINITIONS 中声明的 key 视为"临时/自定义"配置，直接透传环境变量，不查数据库
            return os.environ.get(key)

        # Try database first
        # 优先查数据库：管理员可能已经通过界面/接口覆盖过默认值
        setting = await self._storage.get(key)
        if setting is not None:
            return setting.value

        # Fallback to environment variable
        # 数据库里没有覆盖记录，回退到 .env / 环境变量
        env_value = os.environ.get(key)
        if env_value is not None:
            return await self._parse_env_value_async(key, env_value)

        # Return default
        # 环境变量也没有配置，最终回退到代码里声明的默认值
        return SETTING_DEFINITIONS[key]["default"]

    async def get_raw(self, key: str) -> Any:
        """
        Get raw setting value (without masking) - for internal use only

        Args:
            key: Setting key name

        Returns:
            Raw setting value (sensitive values NOT masked)
        """
        # Check if key is valid
        if key not in SETTING_DEFINITIONS:
            # Try environment variable directly
            return os.environ.get(key)

        # Try database first (without masking)
        setting = await self._storage.get_raw(key)
        if setting is not None:
            return setting.value

        # Fallback to environment variable
        env_value = os.environ.get(key)
        if env_value is not None:
            return await self._parse_env_value_async(key, env_value)

        # Return default
        return SETTING_DEFINITIONS[key]["default"]

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
        return await self._storage.get_all(admin_mode=admin_mode, mask_sensitive=mask_sensitive)

    async def set(self, key: str, value: Any, user_id: str) -> Optional[SettingItem]:
        """
        Set setting value in database.

        Args:
            key: Setting key name
            value: New value
            user_id: User making the change

        Returns:
            Updated setting item
        """
        # 先写入数据库——这是配置的唯一持久化来源
        result = await self._storage.set(key, value, user_id)

        # Refresh the global settings object to reflect the change
        # 写库成功后立即刷新本进程持有的全局 settings 对象，
        # 保证同一进程内紧接着发生的读取能立刻看到新值，无需等待下一次轮询/重启
        from src.kernel.config import refresh_settings

        await refresh_settings(key)

        # Broadcast to other instances via Redis pub/sub
        # 再通过 Redis 广播给其他实例，让它们各自刷新本地内存态，实现跨实例配置同步
        await self._publish_change(key, value)

        return result

    async def init_from_env(self) -> int:
        """
        Import settings from .env to database if not already set.

        Only imports values that don't exist in database yet.
        Each imported setting is also refreshed locally and broadcast to other instances.

        Returns:
            Number of settings imported
        """
        imported = 0

        # 遍历所有已声明的配置项，逐个检查是否需要从环境变量导入初始值
        for key, definition in SETTING_DEFINITIONS.items():
            # Check if already in database
            # 数据库中已有明确写入记录（updated_at 不为空，说明曾被真正写入过而非默认值兜底）
            # 则跳过，不用 .env 覆盖管理员已经设置过的值
            existing = await self._storage.get(key)
            if existing is not None and existing.updated_at is not None:
                continue  # Already set, skip

            # Get value from environment
            env_value = os.environ.get(key)
            if env_value is None:
                continue  # No env value, skip

            # Parse and store via self.set() to trigger refresh + pub/sub broadcast
            # 按配置声明的类型解析字符串值；通过 self.set() 写入是为了复用其中的
            # "刷新本地全局配置 + 广播其他实例" 逻辑，而不是绕过它直接调用 storage.set
            parsed_value = await self._parse_env_value_async(key, env_value)
            result = await self.set(key, parsed_value, "system:init")
            if result is not None:
                imported += 1

        return imported

    async def reset(self, key: Optional[str] = None) -> int:
        """
        Reset settings to default values.

        Args:
            key: Specific key to reset, or None for all

        Returns:
            Number of settings reset
        """
        count = await self._storage.reset(key)

        # Refresh the global settings object to reflect the change
        # 与 set() 一样，重置后也要刷新本进程全局配置，并广播给其他实例
        from src.kernel.config import refresh_settings

        await refresh_settings(key)

        # Broadcast reset to other instances
        await self._publish_change(key, None)

        return count

    def get_sync(self, key: str) -> Any:
        """
        Synchronous get for backward compatibility.

        Note: This only checks environment variables, not database.
        Use async get() for full database access.

        Args:
            key: Setting key name

        Returns:
            Setting value from environment or default
        """
        # 同步接口只能读环境变量/默认值，读不到数据库中的覆盖值——
        # 这是历史遗留的向后兼容入口，新代码应优先使用异步的 get()
        if key not in SETTING_DEFINITIONS:
            return os.environ.get(key)

        env_value = os.environ.get(key)
        if env_value is not None:
            return self._parse_env_value(key, env_value)

        return SETTING_DEFINITIONS[key]["default"]

    def _parse_env_value(self, key: str, value: str) -> Any:
        """Parse environment variable string to correct type"""
        # 把 .env 中天然是字符串的值，按配置声明的类型转换为 bool/number/json 等原生类型；
        # 未声明类型或转换失败时原样返回字符串
        if key not in SETTING_DEFINITIONS:
            return value

        setting_type = SETTING_DEFINITIONS[key]["type"]

        if setting_type == SettingType.BOOLEAN:
            return value.lower() in ("true", "1", "yes", "on")
        elif setting_type == SettingType.NUMBER:
            try:
                # 先尝试整数，失败再退化为浮点数
                return int(value)
            except ValueError:
                return float(value)
        elif setting_type == SettingType.JSON:
            import json

            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        else:
            return value

    async def _parse_env_value_async(self, key: str, value: str) -> Any:
        """Parse environment variable values without blocking async request paths."""
        if key not in SETTING_DEFINITIONS:
            return value

        setting_type = SETTING_DEFINITIONS[key]["type"]
        if setting_type == SettingType.JSON:
            # 只有 JSON 解析可能相对更耗时（值可能较大），丢到线程池执行；
            # 其余类型转换本身足够轻量，直接复用同步版本即可
            try:
                return await run_blocking_io(json.loads, value)
            except json.JSONDecodeError:
                return value

        return self._parse_env_value(key, value)

    @staticmethod
    def requires_restart(key: str) -> bool:
        """Check if setting requires server restart"""
        return key in RESTART_REQUIRED_SETTINGS

    @staticmethod
    def is_sensitive(key: str) -> bool:
        """Check if setting is sensitive (should be hidden in API)"""
        definition = SETTING_DEFINITIONS.get(key)
        return definition.get("is_sensitive", False) if definition else False

    async def close(self) -> None:
        """Close connections"""
        # 关闭底层存储连接，并把自身从单例槽位里摘掉——下次 get_instance() 会创建全新实例。
        # 主要用于测试之间的状态隔离，或需要彻底重建连接时。
        await self._storage.close()
        self._initialized = False
        if SettingsService._instance is self:
            SettingsService._instance = None

    @staticmethod
    async def _publish_change(key: Optional[str], value: Any) -> None:
        """Broadcast a settings change to other instances via Redis pub/sub."""
        try:
            # 延迟导入，避免模块加载期产生循环依赖
            from src.infra.settings.pubsub import get_settings_pubsub
            from src.infra.storage.redis import get_redis_client
            from src.infra.task.constants import SETTINGS_CHANNEL

            redis_client = get_redis_client()
            instance_id = get_settings_pubsub().instance_id
            payload = await run_blocking_io(json.dumps, {"key": key, "instance_id": instance_id})
            await redis_client.publish(
                SETTINGS_CHANNEL,
                payload,
            )
        except Exception as e:
            # Pub/sub failure should not block the setting update
            # 广播失败（例如 Redis 暂时不可用）不应影响本次配置修改的主流程——
            # 本实例自身已经生效，只是其他实例暂时未同步，等它们后续自行刷新时会追上
            import logging

            logging.getLogger(__name__).warning(f"Failed to publish setting change: {e}")


# Global instance getter
def get_settings_service() -> SettingsService:
    """Get the global SettingsService instance"""
    return SettingsService.get_instance()

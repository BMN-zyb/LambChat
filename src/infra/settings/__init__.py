"""
Settings infrastructure module

This module provides database-first settings with .env fallback.
All setting definitions are in src.kernel.config for single source of truth.
"""

# 本包实现"数据库优先、.env 兜底"的配置系统：
# - 配置项的定义（类型/分类/默认值/是否敏感/是否需要重启等元数据）统一维护在 src.kernel.config，
#   这里只负责配置的存取与同步逻辑，避免"两处定义、容易不一致"的问题；
# - SettingsService 负责单例 + 内存缓存 + 读取时 DB/.env 的优先级仲裁；
# - SettingsStorage 负责实际的 MongoDB 持久化读写。
from src.infra.settings.service import SettingsService, get_settings_service
from src.infra.settings.storage import SettingsStorage

# Re-export constants from config.py for backward compatibility
from src.kernel.config import (
    RESTART_REQUIRED_SETTINGS,
    SENSITIVE_SETTINGS,
    SETTING_DEFINITIONS,
)

__all__ = [
    "SettingsService",
    "SettingsStorage",
    "get_settings_service",
    "RESTART_REQUIRED_SETTINGS",
    "SENSITIVE_SETTINGS",
    "SETTING_DEFINITIONS",
]

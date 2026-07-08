"""Configuration management using pydantic-settings.

This module provides centralized configuration management for the application.
"""
# 这是配置子系统真正的包入口（src/kernel/config/ 目录，含 __init__.py）。
# 由于 Python 的导入机制中"同名包"优先于"同名模块"，代码库里所有
# `from src.kernel.config import xxx` 实际都会加载到本文件，
# 而不是与本目录同级、同名的 src/kernel/config.py（那是一个不会被执行的兼容占位文件）。
#
# 本文件的职责是把拆分到各子模块中的实现汇总成统一的公共 API：
#   - base.py        Settings 类定义（pydantic-settings）+ 全局单例 settings/get_settings
#   - constants.py   静态常量（长度限制、需要重启的配置项集合、敏感配置项集合）
#   - definitions.py 所有配置项的元数据定义（single source of truth），聚合自
#                    _definitions_core/_extra/_infra/_sandbox/_tools 等领域子模块
#   - service.py     配置的数据库集成：启动时加载、运行时刷新（无需重启进程）
#   - utils.py       底层工具函数（密钥扩展、读取版本号、读取 git 信息等）
# 其余业务代码应通过本包统一导入配置，而不是直接 import 某个子模块。

from .base import Settings, get_settings, settings
from .constants import (
    JWT_SECRET_KEY_MIN_LENGTH,
    RESTART_REQUIRED_SETTINGS,
    SENSITIVE_SETTINGS,
)
from .definitions import SETTING_DEFINITIONS
from .service import initialize_settings, refresh_settings
from .utils import get_default_from_settings

# Alias for backward compatibility
_get_default_from_settings = get_default_from_settings

# 按来源子模块分组列出所有对外公开的名称，方便对照上面的职责说明逐一核对，
# 避免子模块新增导出后忘记在这里同步暴露
__all__ = [
    # Settings class and instance
    "Settings",
    "get_settings",
    "settings",
    # Definitions
    "SETTING_DEFINITIONS",
    # Constants
    "JWT_SECRET_KEY_MIN_LENGTH",
    "RESTART_REQUIRED_SETTINGS",
    "SENSITIVE_SETTINGS",
    # Service functions
    "initialize_settings",
    "refresh_settings",
    # Utility functions
    "get_default_from_settings",
    "_get_default_from_settings",
]

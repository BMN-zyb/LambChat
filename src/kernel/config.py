"""Configuration management using pydantic-settings.

This module re-exports all public API from src.kernel.config package for backward compatibility.
"""
# 重要说明：当同一目录下同时存在同名的模块文件 config.py 和同名的包目录 config/
# （即 src/kernel/config/，其中有 __init__.py）时，Python 的默认导入机制会优先把
# `src.kernel.config` 解析成"包"而不是这个单文件模块——也就是说，代码库里所有
# `from src.kernel.config import xxx` 或 `import src.kernel.config` 实际加载的
# 都是 src/kernel/config/__init__.py，这个 config.py 文件在正常导入路径下
# 不会被真正执行到（已通过实验验证：同名 package 优先于同名 module 被 FileFinder 解析）。
# 推测这是项目把原本单文件的 config.py 拆分成 config/ 包（拆成 base/constants/
# definitions/service/utils 等子模块）之后，为兼容"可能仍有代码以旧方式引用"的场景
# 而保留下来的兼容层/文档占位；如果确实需要维护这层兼容性，也应同步维护下面的
# 导入列表和 __all__，使其始终与 config/__init__.py 的公共 API 保持一致。

from src.kernel.config import (
    JWT_SECRET_KEY_MIN_LENGTH,
    RESTART_REQUIRED_SETTINGS,
    SENSITIVE_SETTINGS,
    SETTING_DEFINITIONS,
    Settings,
    get_settings,
    initialize_settings,
    refresh_settings,
    settings,
)

# __all__ 需要和 src/kernel/config/__init__.py 中导出的公共 API 保持同步，
# 否则 `from src.kernel.config import *` 在两种解析结果下的行为会不一致
__all__ = [
    "Settings",
    "get_settings",
    "settings",
    "SETTING_DEFINITIONS",
    "JWT_SECRET_KEY_MIN_LENGTH",
    "RESTART_REQUIRED_SETTINGS",
    "SENSITIVE_SETTINGS",
    "initialize_settings",
    "refresh_settings",
]

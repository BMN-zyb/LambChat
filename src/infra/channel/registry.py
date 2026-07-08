"""Channel registry for auto-discovery of channel types.

Provides discovery mechanism for built-in channel implementations and
external plugins registered via entry points.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Optional

from src.infra.logging import get_logger

if TYPE_CHECKING:
    from src.infra.channel.base import BaseChannel, UserChannelManager
    from src.kernel.schemas.channel import ChannelType

logger = get_logger(__name__)
# Internal modules to skip during discovery
# 自动发现时需跳过的内部模块名（这些是基础设施本身，不是具体渠道实现）。
_INTERNAL = frozenset({"base", "registry", "manager", "__init__"})


def discover_channel_modules() -> list[str]:
    """
    Return all built-in channel module names by scanning the package.

    Returns:
        List of module names that contain channel implementations.
    """
    import os

    import src.infra.channel as pkg

    # Get modules (files)
    # 第一步：扫描包内的单文件模块（.py），排除内部模块与子包。
    modules = [
        name
        for _, name, ispkg in pkgutil.iter_modules(pkg.__path__)
        if name not in _INTERNAL and not ispkg
    ]

    # Also discover packages (directories with __init__.py) in the channel directory
    # 第二步：把带 __init__.py 的子目录也视作渠道模块（如 feishu/ 这种整包实现）。
    channel_dir = pkg.__path__[0]  # This is the channel directory itself
    for item in os.listdir(channel_dir):
        item_path = os.path.join(channel_dir, item)
        if os.path.isdir(item_path) and item not in _INTERNAL:
            init_file = os.path.join(item_path, "__init__.py")
            if os.path.exists(init_file):
                modules.append(item)

    return modules


def load_channel_class(module_name: str) -> Optional[type["BaseChannel"]]:
    """
    Import a channel module and return the BaseChannel subclass found.

    Args:
        module_name: Name of the channel module.

    Returns:
        The BaseChannel subclass, or None if not found.
    """
    from src.infra.channel.base import BaseChannel as _Base

    try:
        # 动态导入目标模块，遍历其属性寻找 BaseChannel 的具体子类（排除基类本身）。
        mod = importlib.import_module(f"src.infra.channel.{module_name}")
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, type) and issubclass(obj, _Base) and obj is not _Base:
                return obj
    except ImportError as e:
        # 依赖缺失/可选渠道未安装：降级为 debug 日志，不视为错误。
        logger.debug(f"Could not import channel module '{module_name}': {e}")
    except Exception as e:
        logger.warning(f"Error loading channel module '{module_name}': {e}")

    return None


def load_manager_class(module_name: str) -> Optional[type["UserChannelManager"]]:
    """
    Import a channel module and return the UserChannelManager subclass found.

    Args:
        module_name: Name of the channel module.

    Returns:
        The UserChannelManager subclass, or None if not found.
    """
    from src.infra.channel.base import UserChannelManager as _Manager

    try:
        # 与 load_channel_class 同理，但查找的是 UserChannelManager 的具体子类。
        mod = importlib.import_module(f"src.infra.channel.{module_name}")
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, type) and issubclass(obj, _Manager) and obj is not _Manager:
                return obj
    except ImportError as e:
        logger.debug(f"Could not import channel module '{module_name}': {e}")
    except Exception as e:
        logger.warning(f"Error loading manager from '{module_name}': {e}")

    return None


def discover_plugins() -> dict[str, type["BaseChannel"]]:
    """
    Discover external channel plugins registered via entry_points.

    Returns:
        Dictionary mapping plugin names to their channel classes.
    """
    plugins: dict[str, type["BaseChannel"]] = {}

    try:
        from importlib.metadata import entry_points

        # 通过 setuptools entry_points 的 "lambchat.channels" 组发现第三方渠道插件。
        for ep in entry_points(group="lambchat.channels"):
            try:
                cls = ep.load()
                plugins[ep.name] = cls
            except Exception as e:
                # 单个插件加载失败不影响其它插件。
                logger.warning(f"Failed to load channel plugin '{ep.name}': {e}")
    except Exception as e:
        logger.debug(f"Could not discover entry_points plugins: {e}")

    return plugins


def discover_all_channels() -> dict[str, type["BaseChannel"]]:
    """
    Return all available channels: built-in (pkgutil) merged with external (entry_points).

    Built-in channels take priority — an external plugin cannot shadow a built-in name.

    Returns:
        Dictionary mapping channel type values to their classes.
    """
    builtin: dict[str, type["BaseChannel"]] = {}

    for modname in discover_channel_modules():
        cls = load_channel_class(modname)
        if cls:
            # Use channel_type.value as the key for consistent lookup
            key = cls.channel_type.value
            builtin[key] = cls

    external = discover_plugins()
    # Also key external plugins by their channel_type.value
    # 外部插件也按 channel_type.value 重新建键，以便与内置渠道用同一命名空间比较。
    external_keyed = {}
    for name, cls in external.items():
        key = cls.channel_type.value
        external_keyed[key] = cls

    # 冲突检测：外部插件若与内置渠道同名（同 channel_type），内置优先、插件被忽略。
    shadowed = set(external_keyed) & set(builtin)
    if shadowed:
        logger.warning(f"Plugin(s) shadowed by built-in channels (ignored): {shadowed}")

    # 合并顺序保证 builtin 覆盖 external_keyed，实现"内置优先、插件不可遮蔽内置"。
    return {**external_keyed, **builtin}


def discover_all_managers() -> dict[str, type["UserChannelManager"]]:
    """
    Return all available channel managers.

    Returns:
        Dictionary mapping channel type values to their manager classes.
    """
    managers: dict[str, type["UserChannelManager"]] = {}

    for modname in discover_channel_modules():
        cls = load_manager_class(modname)
        if cls:
            # Use channel_type.value as the key for consistent lookup
            key = cls.channel_type.value
            managers[key] = cls

    return managers


class ChannelRegistry:
    """
    Registry for all available channel types.

    Provides a centralized registry for channel discovery and lookup.
    """

    _instance: Optional["ChannelRegistry"] = None
    # 类级缓存：已发现的渠道类与管理器类，以及是否已完成一次性初始化的标记。
    _channels: dict[str, type["BaseChannel"]] = {}
    _managers: dict[str, type["UserChannelManager"]] = {}
    _initialized = False

    def __new__(cls) -> "ChannelRegistry":
        # 单例模式：无论构造多少次都返回同一实例，保证发现结果全局共享。
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def initialize(self) -> None:
        """Initialize the registry by discovering all channels."""
        # 幂等初始化：仅在首次调用时执行昂贵的自动发现。
        if self._initialized:
            return

        self._channels = discover_all_channels()
        self._managers = discover_all_managers()
        self._initialized = True

        logger.info(f"Channel registry initialized: {list(self._channels.keys())}")

    def get_channel_class(self, channel_type: "ChannelType") -> Optional[type["BaseChannel"]]:
        """
        Get a channel class by type.

        Args:
            channel_type: The channel type enum value.

        Returns:
            The channel class, or None if not found.
        """
        return self._channels.get(channel_type.value)

    def get_manager_class(
        self, channel_type: "ChannelType"
    ) -> Optional[type["UserChannelManager"]]:
        """
        Get a manager class by channel type.

        Args:
            channel_type: The channel type enum value.

        Returns:
            The manager class, or None if not found.
        """
        return self._managers.get(channel_type.value)

    def get_all_channels(self) -> dict[str, type["BaseChannel"]]:
        """Get all registered channel classes."""
        return self._channels.copy()

    def get_all_managers(self) -> dict[str, type["UserChannelManager"]]:
        """Get all registered manager classes."""
        return self._managers.copy()

    def get_channel_metadata(self) -> list[dict]:
        """Get metadata for all registered channels."""
        # 汇总所有渠道类的元数据（供前端展示可选渠道列表）。
        return [cls.get_metadata() for cls in self._channels.values()]


def get_registry() -> ChannelRegistry:
    """Get the singleton channel registry instance."""
    # 取（或创建）单例并确保已初始化；因 initialize 幂等，可安全重复调用。
    registry = ChannelRegistry()
    registry.initialize()
    return registry

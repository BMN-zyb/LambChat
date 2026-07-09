"""
存储基类

定义存储服务的抽象接口。
"""

# ---------------------------------------------------------------------------
# 模块说明：键值存储抽象接口（StorageBase）
#
# 本模块定义所有键值存储实现必须遵循的统一异步接口（get/set/delete/exists/keys），
# 是「面向接口编程」的地基：上层只依赖这里的抽象，具体后端（如 Redis、MongoDB）
# 各自实现，从而可无缝替换而不影响调用方。
# 用 abc.ABC + @abstractmethod 强制子类必须实现全部方法，否则实例化即报错。
# ---------------------------------------------------------------------------

from abc import ABC, abstractmethod
from typing import Any, Optional


# 存储抽象基类：用 ABC + abstractmethod 约束所有键值存储实现统一对外接口，
# 便于上层在不同后端（Redis 等）之间无缝替换
class StorageBase(ABC):
    """
    存储抽象基类

    定义所有存储实现必须遵循的接口。
    """

    @abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        """
        获取数据

        Args:
            key: 键名

        Returns:
            数据或 None
        """
        # 抽象方法：由具体后端实现按 key 读取
        pass

    @abstractmethod
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """
        设置数据

        Args:
            key: 键名
            value: 值
            ttl: 过期时间（秒）
        """
        pass

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """
        删除数据

        Args:
            key: 键名

        Returns:
            是否删除成功
        """
        pass

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """
        检查键是否存在

        Args:
            key: 键名

        Returns:
            是否存在
        """
        pass

    @abstractmethod
    async def keys(self, pattern: str) -> list[str]:
        """
        获取匹配的键列表

        Args:
            pattern: 匹配模式

        Returns:
            键列表
        """
        pass

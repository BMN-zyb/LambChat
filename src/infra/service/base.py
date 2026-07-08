"""
服务基类
"""

from abc import ABC, abstractmethod


class BaseService(ABC):
    """
    服务基类

    所有第三方服务的抽象基类。
    """

    # 抽象方法定义了统一的服务生命周期契约:初始化 -> (使用) -> 关闭,外加健康检查。
    # 子类必须实现这三个方法;三者均为异步,以适配网络/IO 型第三方服务。
    @abstractmethod
    async def initialize(self) -> None:
        """初始化服务"""
        pass

    @abstractmethod
    async def close(self) -> None:
        """关闭服务连接"""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查"""
        pass

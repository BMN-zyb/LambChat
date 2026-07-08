"""
角色管理模块
"""

# RoleManager 为业务门面，RoleStorage 为带 Redis 缓存的 MongoDB 持久化层
from src.infra.role.manager import RoleManager
from src.infra.role.storage import RoleStorage

# 对外导出角色管理器与存储类
__all__ = [
    "RoleManager",
    "RoleStorage",
]

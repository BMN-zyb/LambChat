"""
用户管理模块
"""

# UserManager 为业务门面（注册/登录/资料等编排），UserStorage 为 MongoDB 持久化层
from src.infra.user.manager import UserManager
from src.infra.user.storage import UserStorage

# 对外导出用户管理器与存储类
__all__ = [
    "UserManager",
    "UserStorage",
]

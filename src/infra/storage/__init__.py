"""
存储模块

提供存储服务的抽象和实现。
"""

# StorageBase：键值存储的抽象基类（统一 get/set/delete/exists/keys 接口）
from src.infra.storage.base import StorageBase
# MongoDBStorage：MongoDB 客户端工厂与封装（文档型数据地基）
from src.infra.storage.mongodb import MongoDBStorage
# postgres：连接池获取/关闭 + LangGraph Postgres store 工厂
from src.infra.storage.postgres import (
    close_connection_pool,
    create_postgres_store,
    get_connection_pool,
)
# RedisStorage：Redis 客户端封装（缓存 / SSE Stream / 分布式锁地基）
from src.infra.storage.redis import RedisStorage

# 对外导出的统一存储入口清单
__all__ = [
    "StorageBase",
    "MongoDBStorage",
    "RedisStorage",
    "get_connection_pool",
    "create_postgres_store",
    "close_connection_pool",
]

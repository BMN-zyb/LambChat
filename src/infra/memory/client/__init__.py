"""
Memory backend client (native MongoDB-backed).
"""

# 对外统一导出记忆后端的抽象接口（MemoryBackend）、工厂函数（create_memory_backend）、
# 功能开关判断（is_memory_enabled），以及当前唯一支持的具体实现 NativeMemoryBackend。
from src.infra.memory.client.base import (
    MemoryBackend,
    create_memory_backend,
    is_memory_enabled,
)
from src.infra.memory.client.native import NativeMemoryBackend

__all__ = [
    "MemoryBackend",
    "NativeMemoryBackend",
    "create_memory_backend",
    "is_memory_enabled",
]

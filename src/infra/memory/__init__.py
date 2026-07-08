"""
Memory Infrastructure Module

Provides cross-session long-term memory capabilities with unified tool interface.
Uses the native MongoDB-backed backend.
"""

# 本包对外提供两类接口：
# 1) 统一的记忆工具（get_all_memory_tools 等）：供 agent 以 retain/recall/delete 语义调用，
#    内部自动分发到当前启用的具体 backend 实现，调用方无需关心后端细节；
# 2) Backend 抽象（MemoryBackend/create_memory_backend/is_memory_enabled）：
#    供需要接入新记忆后端的开发者继承/实现，当前仓库默认使用基于 MongoDB 的 native backend。

# Unified memory tools (auto-dispatch to active backend)
# Base abstractions (for adding new backends)
from src.infra.memory.client.base import (
    MemoryBackend,
    create_memory_backend,
    is_memory_enabled,
)
from src.infra.memory.tools import (
    get_all_memory_tools,
    get_memory_delete_tool,
    get_memory_recall_tool,
    get_memory_retain_tool,
)

__all__ = [
    # Unified tools (preferred API)
    "get_all_memory_tools",
    "get_memory_retain_tool",
    "get_memory_recall_tool",
    "get_memory_delete_tool",
    # Backend factory
    "create_memory_backend",
    "is_memory_enabled",
    "MemoryBackend",
]

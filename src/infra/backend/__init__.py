"""Backend implementations for file operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .context import (
    clear_user_context,
    get_session_id,
    get_user_id,
    set_user_context,
)
from .deepagent import (
    create_memory_backend_factory,
    create_persistent_backend_factory,
    create_sandbox_backend_factory,
)

if TYPE_CHECKING:
    from .skills_store import SkillsStoreBackend

__all__ = [
    # Context
    "set_user_context",
    "get_user_id",
    "get_session_id",
    "clear_user_context",
    # DeepAgent Backend
    "create_memory_backend_factory",
    "create_persistent_backend_factory",
    "create_sandbox_backend_factory",
    # Skills Store Backend
    "SkillsStoreBackend",
    "create_skills_backend",
]


# 延迟导入 skills_store：它依赖 MongoDB 等较重的模块，用模块级 __getattr__ 实现按需加载，
# 避免 import 本包时就拉起这些依赖（同时规避潜在的循环导入）。
def __getattr__(name: str):
    if name in {"SkillsStoreBackend", "create_skills_backend"}:
        from .skills_store import SkillsStoreBackend, create_skills_backend

        return {
            "SkillsStoreBackend": SkillsStoreBackend,
            "create_skills_backend": create_skills_backend,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

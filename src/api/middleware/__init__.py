"""Middleware module."""

# 将 UserContextMiddleware 上提到包命名空间，便于外部通过 `src.api.middleware` 直接导入
from src.api.middleware.user_context import UserContextMiddleware

# 声明包的公开导出符号，仅对外暴露 UserContextMiddleware
__all__ = ["UserContextMiddleware"]

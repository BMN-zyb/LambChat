"""
Sandbox 模块

提供统一的 Sandbox 管理，支持 Daytona、E2B 平台。
"""

# base：SandboxFactory 与三平台配置类（负责"如何创建"一个沙箱）
from .base import (
    DaytonaConfig,
    E2BConfig,
    SandboxConfig,
    SandboxFactory,
    get_sandbox_config_from_settings,
    get_sandbox_from_settings,
)
# session_manager：沙箱的"绑定与生命周期"（每用户一沙箱、跨 session 共享）
from .session_manager import (
    SessionSandboxManager,
    close_session_sandbox_manager,
    get_session_sandbox_manager,
)

__all__ = [
    # 配置类
    "SandboxConfig",
    "DaytonaConfig",
    "E2BConfig",
    # 工厂
    "SandboxFactory",
    "get_sandbox_config_from_settings",
    "get_sandbox_from_settings",
    # Session 绑定管理
    "SessionSandboxManager",
    "get_session_sandbox_manager",
    "close_session_sandbox_manager",
]

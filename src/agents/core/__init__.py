"""
Agent 核心模块

提供 Graph Agent 基类和注册机制。
"""

# 从 base 子模块集中再导出 core 包对外暴露的构建块，
# 让调用方可以直接 `from src.agents.core import ...`，无需深入具体子模块路径。
# base 提供：Graph Agent 基类与构图器、全局 agent 注册表及注册/查找函数、
# agent 工厂，以及从 RunnableConfig 中取出 presenter 的辅助函数。
from src.agents.core.base import (
    # 注册
    _AGENT_REGISTRY,
    # 工厂
    AgentFactory,
    # 基类
    BaseGraphAgent,
    GraphBuilder,
    get_agent_class,
    # 辅助
    get_presenter,
    list_registered_agents,
    register_agent,
    resolve_agent_name,
)

# __all__ 显式声明本包的公共 API：约束 `from src.agents.core import *` 的导出范围，
# 同时作为对外契约清单。注意下划线开头的 _AGENT_REGISTRY 也被特意导出，供注册流程复用。
__all__ = [
    # 基类
    "BaseGraphAgent",
    "GraphBuilder",
    # 注册
    "_AGENT_REGISTRY",
    "register_agent",
    "get_agent_class",
    "list_registered_agents",
    # 工厂
    "AgentFactory",
    # 辅助
    "get_presenter",
    "resolve_agent_name",
]

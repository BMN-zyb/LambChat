"""
Fast Agent 模块 - 快速响应，无沙箱

Agent 已通过 @register_agent("fast") 装饰器自动注册。
"""

# 导入本包四个核心组件并向外重新导出（见下方 __all__）。
# 关键副作用：import .graph 会执行其中的 @register_agent("fast") 装饰器，
# 将 FastAgent 注册进全局 Agent 注册表——因此“导入本包”即完成注册，
# 上层无需显式调用注册函数。fast / search / team 三个 agent 都用此模式接入。
from src.agents.fast_agent.context import FastAgentContext
from src.agents.fast_agent.graph import FastAgent
from src.agents.fast_agent.nodes import fast_agent_node
from src.agents.fast_agent.state import FastAgentState

# 显式声明包对外暴露的公共符号，约束 `from ... import *` 的导出范围。
__all__ = [
    "FastAgent",
    "FastAgentContext",
    "FastAgentState",
    "fast_agent_node",
]

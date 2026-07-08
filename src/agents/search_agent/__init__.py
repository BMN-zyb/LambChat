"""
Search Agent 模块

Agent 已通过 @register_agent("search") 装饰器自动注册。
"""

# 汇总并重导出本包的公共符号；导入 graph 模块这一行会触发其中的
# @register_agent("search") 装饰器执行，把 SearchAgent 登记进全局注册表
# （见 core/base.py 的 _AGENT_REGISTRY），因此本包"被 import 即完成注册"。
# 运行时上下文：模型 / 工具 / 技能的解析与承载（见 context.py）
from src.agents.search_agent.context import SearchAgentContext
# 外层 graph 薄壳类 + 注册入口；import 本行即触发 @register_agent 副作用
from src.agents.search_agent.graph import SearchAgent
# 外层 graph 的唯一节点，内部用 deepagents 装配 ReAct 内层 graph（见 nodes.py）
from src.agents.search_agent.nodes import agent_node
# LangGraph 外层 State 的 TypedDict 定义（见 state.py）
from src.agents.search_agent.state import SearchAgentState

# 显式声明本包对外导出的符号集合
__all__ = [
    "SearchAgent",
    "SearchAgentContext",
    "SearchAgentState",
    "agent_node",
]

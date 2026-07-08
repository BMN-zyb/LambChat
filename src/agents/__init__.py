"""
Agent 模块

提供 Graph Agent 基类和注册机制。

每个 Agent 就是一个 CompiledGraph：
- 流式请求接入 graph
- 节点通过 config 获取 Presenter 输出 SSE 事件
"""

# 把 core 子包里的共享基础设施重新导出到 src.agents 包根，
# 让调用方统一用 `from src.agents import ...` 就能拿到注册表 / 工厂 / 基类，
# 而不必关心它们实际定义在 core 的哪个模块里。
from src.agents.core import (
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
)


# 触发式注册：本项目真正注册的 agent 只有 fast / search / team 三个。
# 把这三个模块 import 进来，其模块顶部的 @register_agent 装饰器就会执行，
# 从而把对应实现类登记进 _AGENT_REGISTRY。
# 之所以延迟到函数内部才 import，是为了规避包加载期的循环依赖，
# 并把"注册时机"交给调用方在应用启动时显式触发。
def discover_agents() -> None:
    """发现并注册所有 Agent"""
    # 导入会触发 @register_agent 装饰器
    from src.agents.fast_agent import FastAgent  # noqa: F401
    from src.agents.search_agent import SearchAgent  # noqa: F401
    from src.agents.team_agent import TeamAgent  # noqa: F401


# 便捷包装：委托 AgentFactory.get，按 agent_id 取到（并按需构建）对应的 Agent 实例。
async def get_agent_async(agent_id: str) -> BaseGraphAgent:
    """异步获取 Agent 实例"""
    return await AgentFactory.get(agent_id)


# 便捷包装：委托 AgentFactory.list_agents，产出给前端做 agent 选择列表的数据；
# default_agent_id 对应的项会被排到最前，作为默认选项。
def list_agents(default_agent_id: str | None = None) -> list[dict[str, str]]:
    """列出所有注册的 Agent（按名称排序，默认 agent 排在最前面）"""
    return AgentFactory.list_agents(default_agent_id=default_agent_id)


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
    # 便捷函数
    "get_agent_async",
    "list_agents",
    "discover_agents",
]

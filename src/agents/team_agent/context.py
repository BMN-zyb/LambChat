"""Team Agent context — reuses FastAgentContext tool/skill loading."""

# 直接复用 fast_agent 的上下文实现：工具/技能的加载、过滤，以及 MCP 延迟工具管理等逻辑完全一致。
from src.agents.fast_agent.context import FastAgentContext


# 运行时上下文对象：承载模型能力解析所需的用户/会话信息，并负责工具、技能、MCP 的加载与过滤。
# 团队专属逻辑（团队解析、角色子代理装配）不放在这里，而是全部集中在 nodes.py 的
# team_router_node 中完成，因此本类只是薄薄地继承 FastAgentContext、不做任何额外覆写。
class TeamAgentContext(FastAgentContext):
    """Reuses FastAgentContext tool/skill loading. Team-specific logic is in the node."""

    # 无需新增字段或方法——占位即可，行为完全等同父类 FastAgentContext。
    pass

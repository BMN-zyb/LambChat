"""Team Agent module."""

# 包级导出：只在导入时立即引入 prompt 模块里的纯函数（不依赖 deepagents 等重型库），
# 让 `import src.agents.team_agent` 的代价保持极低。真正装配 Agent 运行时的 graph/nodes
# 属于重依赖，延迟到 __getattr__ 里按需加载。
from src.agents.team_agent.prompt import build_team_members_description

# 对外公开的符号；TeamAgent 故意不在此静态导出，改由下方 __getattr__ 惰性提供。
__all__ = ["build_team_members_description"]


def __getattr__(name):
    """Lazy import for heavy dependencies (graph, nodes)."""
    # 只有真正访问 team_agent.TeamAgent 时才导入 graph 模块，进而触发其中
    # @register_agent("team") 的注册；同时把 deepagents / nodes 等重依赖推迟到此刻才加载。
    if name == "TeamAgent":
        from src.agents.team_agent.graph import TeamAgent

        return TeamAgent
    # 其余未知属性按标准模块语义抛出 AttributeError，保证属性访问行为正确。
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""
MCP infrastructure module
"""

# 从存储子模块导出 MCPStorage：MCP（Model Context Protocol）配置的持久化入口
# MCPStorage 统一管理五大集合——系统级服务器、用户级服务器、用户偏好、策略、配额
from src.infra.mcp.storage import MCPStorage

# 对外暴露的公共符号：外部只需 `from src.infra.mcp import MCPStorage` 即可
__all__ = ["MCPStorage"]

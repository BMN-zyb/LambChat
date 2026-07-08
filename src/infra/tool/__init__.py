"""
工具管理模块
"""

# 预先导入若干内置工具子模块，确保它们在包被加载时即完成模块级初始化
# （例如工具定义、装饰器注册等副作用），并作为包的公开子模块对外暴露
from src.infra.tool import (
    audio_transcribe_tool,
    image_analysis_tool,
    image_generation_tool,
    team_tool,
)
# MCPClient：MCP（Model Context Protocol）客户端入口类
from src.infra.tool.mcp_client import MCPClient
# ToolRegistry：进程内工具注册表，负责工具的注册与发现
from src.infra.tool.registry import ToolRegistry

# __all__ 显式声明本包对外导出的符号，控制 `from src.infra.tool import *` 的行为
__all__ = [
    "ToolRegistry",
    "MCPClient",
    "audio_transcribe_tool",
    "image_analysis_tool",
    "image_generation_tool",
    "team_tool",
]

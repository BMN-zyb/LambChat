"""
Human Tool 模块

支持多字段表单的 ask_human 工具。
"""

# 数据模型：AskHumanInput 是工具调用的入参结构，FieldType/FormField 描述表单里
# 单个字段的类型与配置，供 Agent 动态构造"向人类提问"的多字段表单
from src.infra.tool.human_tool.models import AskHumanInput, FieldType, FormField
# 工具实现：AskHumanTool 是真正的 LangChain 工具类，get_human_tool 是获取其
# 单例/实例的工厂函数，供 Agent 注册与调用
from src.infra.tool.human_tool.tool import AskHumanTool, get_human_tool

# 显式声明包的公开 API，避免 from ... import * 时带出内部实现细节
__all__ = [
    "AskHumanInput",
    "AskHumanTool",
    "FieldType",
    "FormField",
    "get_human_tool",
]

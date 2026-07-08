"""Message-related schemas."""

# 模块说明：定义对话消息、工具调用与工具执行结果相关的数据模型。
# MessageType 实际定义于 src/kernel/types.py，这里重新导出（re-export）方便
# 其它模块统一从 src.kernel.schemas.message 导入，无需关心其定义位置。
# 主要使用方：src/kernel/schemas/agent.py（AgentStep 中的 tool_calls 字段）、
# src/kernel/schemas/__init__.py（对外统一导出）等消息相关处理逻辑。
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.infra.utils.datetime import utc_now
from src.kernel.types import MessageType

# 显式声明本模块对外导出的符号，其中 MessageType 是从 src.kernel.types 转导出的
__all__ = ["Message", "MessageType", "ToolCall", "ToolResult"]


# 对话中的单条消息。
class Message(BaseModel):
    """Single message in a conversation."""

    # 消息角色/类型：human（用户）、ai（模型）、system（系统）、tool（工具）
    type: MessageType
    # 消息文本内容
    content: str
    # 消息产生时间，默认取当前 UTC 时间
    timestamp: datetime = Field(default_factory=utc_now)
    # 附加元数据，结构不固定（如附件信息、渲染提示等）
    metadata: dict[str, Any] = Field(default_factory=dict)


# 一次工具调用的请求信息（模型决定调用某个工具时产生）。
class ToolCall(BaseModel):
    """Tool call details."""

    # 工具名称
    name: str
    # 调用参数（工具入参字典）
    arguments: dict[str, Any] = Field(default_factory=dict)
    # 调用 ID，用于将请求与对应的 ToolResult 配对
    call_id: Optional[str] = None


# 一次工具执行后的结果信息。
class ToolResult(BaseModel):
    """Tool execution result."""

    # 对应的调用 ID（与 ToolCall.call_id 一致）
    call_id: str
    # 工具名称
    name: str
    # 执行结果内容（文本形式）
    content: str
    # 是否执行成功
    success: bool
    # 执行失败时的错误信息
    error: Optional[str] = None
    # 执行耗时，单位毫秒
    execution_time_ms: Optional[float] = None

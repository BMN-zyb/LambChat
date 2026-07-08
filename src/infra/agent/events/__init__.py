"""Agent stream event processing package."""

# 事件处理器：本包对外的核心入口，负责路由/转换原始事件流
from src.infra.agent.events.processor import AgentEventProcessor
# StreamEvent 类型别名：统一 LangChain 标准事件与自定义事件
from src.infra.agent.events.types import StreamEvent

# 对外导出的公共符号
__all__ = ["AgentEventProcessor", "StreamEvent"]

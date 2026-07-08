"""
Search Agent 状态定义
"""

from typing import Any, Dict, List, Optional, TypedDict


# LangGraph 外层 graph 的状态 Schema（TypedDict）。
# 这是"外层薄壳 graph"的状态；它与 deepagents 内层 graph 各自维护的消息历史是两套东西。
# 外层几乎无状态：messages 一般留空，真正的多轮对话历史由内层 checkpointer 持久化。
class SearchAgentState(TypedDict):
    """
    Search Agent 状态

    Attributes:
        input: 用户输入
        session_id: 会话 ID
        messages: 消息历史
        output: 输出结果
        context: Agent 上下文（运行时注入）
        attachments: 用户上传的附件列表
    """

    # 用户本轮输入的文本
    input: str
    # 会话 ID；同时作为内外层 checkpoint 的 thread_id 贯穿整个请求
    session_id: str
    # 消息历史（外层一般为空；真正历史由内层 deep agent 的 checkpointer 托管）
    messages: List[Any]
    # Agent 的最终输出文本
    output: str
    # 运行时注入的上下文快照（如 kwargs），可选
    context: Optional[Dict[str, Any]]
    # 用户上传的附件列表（图片 / 文档等），可选
    attachments: Optional[List[Dict[str, Any]]]

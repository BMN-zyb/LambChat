"""
Fast Agent 状态定义 - 极简状态
"""

from typing import Any, Dict, List, Optional, TypedDict


# LangGraph 外层 graph 的状态 schema（TypedDict）。外层 graph 是薄壳
# （START -> agent_node -> END），字段刻意保持极简：只承载一轮请求的入参与出参，
# 复杂的 ReAct 中间状态全部下沉到内层 deep agent，避免两层状态互相污染。
class FastAgentState(TypedDict):
    """
    Fast Agent 状态 - 最小化字段

    Attributes:
        input: 用户输入
        session_id: 会话 ID
        messages: 消息历史
        output: 输出结果
        attachments: 用户上传的附件列表（可选）
    """

    # 本轮用户输入的原始文本；由 graph._stream 写入初始状态，节点据此构建 HumanMessage。
    input: str
    # 会话 ID；同时用作外层 thread_id 以及内层 deep agent checkpointer 的线程键，串联同一会话的多轮对话。
    session_id: str
    # 消息历史列表。注意：外层 graph 无 checkpointer，此字段在本 agent 中基本留空，
    # 真正的多轮历史由内层 deep agent 的 checkpointer + add_messages reducer 维护。
    messages: List[Any]
    # 最终输出文本；节点执行完把 AgentEventProcessor 汇总的回复写回这里作为 graph 返回值。
    output: str
    # 用户上传的附件列表（可选）；节点会按模型视觉能力选择内联为 data URL 或仅作文本摘要。
    attachments: Optional[List[Dict[str, Any]]]

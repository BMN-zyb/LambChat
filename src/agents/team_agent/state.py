"""Team Agent state."""

# TypedDict 用于给 LangGraph 的状态通道做静态类型标注：节点函数以普通 dict 读写这些键。
from typing import Any, Dict, List, Optional, TypedDict


class TeamAgentState(TypedDict):
    """LambChat 外层 graph 的状态定义（状态通道 schema）。

    外层 graph 只有一个 agent 节点（见 graph.py 的 START -> agent -> END）：
    team_router_node 从这里读取 input / session_id / attachments 作为输入，运行结束时把
    最终文本写回 output。messages 通道在当前实现里是占位——真正的对话历史由 nodes.py 内层
    deep agent 的 checkpointer 负责持久化，外层 graph 保持无状态。
    """
    # 本轮用户输入的原始文本。
    input: str
    # 会话 ID：同时用作沙箱会话键、内层 checkpointer 的 thread_id 与多租户隔离依据。
    session_id: str
    # 消息通道；外层 graph 当前不使用（占位），历史由内层 deep agent 维护。
    messages: List[Any]
    # 节点最终输出文本，作为 team_router_node 的返回值写回。
    output: str
    # 附件列表（图片 / 文档等），可能为 None；图片附件在节点内会按模型视觉能力内联处理。
    attachments: Optional[List[Dict[str, Any]]]

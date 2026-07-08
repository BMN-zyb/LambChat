"""Session-related schemas."""

# 模块说明：定义"会话"（Session，即一次完整的多轮对话）相关的数据模型，
# 以及会话的"消息级检查点/分支（fork）"功能所需的辅助模型与工具函数。
# 检查点允许用户从历史会话中的某条消息处创建一个书签，之后可以从该书签
# 派生（fork）出一个新的独立会话，从而实现"从某个历史节点开始另一条对话分支"。
# 主要使用方：src/infra/session/manager.py / storage.py（会话的业务逻辑与持久化）、
# src/api/routes/session.py（会话相关 HTTP 接口）。
from copy import deepcopy
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.infra.utils.datetime import utc_now


# 会话的公共基础字段，被创建请求与完整会话模型共同继承。
class SessionBase(BaseModel):
    """Base session schema."""

    # 会话名称，可选（为空时前端通常展示"新会话"或取首条消息摘要）
    name: Optional[str] = None
    # 会话级自由元数据（如分支/fork 信息、来源标记等），结构不固定
    metadata: dict[str, Any] = Field(default_factory=dict)


# 创建会话的请求体，字段与 SessionBase 完全一致。
class SessionCreate(SessionBase):
    """Schema for creating a session."""

    pass


# 更新会话的请求体，所有字段均可选（PATCH 语义）。
class SessionUpdate(BaseModel):
    """Schema for updating a session."""

    name: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


# 会话的完整模型（数据库实体视图）。
class Session(SessionBase):
    """Session model."""

    # 会话 ID
    id: str
    # 所属用户 ID，可为空（如匿名/系统会话场景）
    user_id: Optional[str] = None
    # 绑定使用的 Agent ID，默认使用名为 "fast" 的内置 Agent
    agent_id: str = "fast"
    # 创建时间
    created_at: datetime = Field(default_factory=utc_now)
    # 最近更新时间
    updated_at: datetime = Field(default_factory=utc_now)
    # 会话是否处于激活状态（False 通常表示已被停用/软删除）
    is_active: bool = True
    # Task execution status
    task_status: Optional[str] = None  # pending, running, completed, failed
    # 任务失败时的错误信息
    task_error: Optional[str] = None
    # 当前这一轮任务的完成时间
    completed_at: Optional[datetime] = None
    # 未读消息数（用于消息中心/会话列表的未读提示）
    unread_count: int = 0

    class Config:
        # 允许从属性对象（如 Mongo 文档转换后的对象）直接构造本模型
        from_attributes = True


# 消息级分支检查点的元数据：为会话中某条消息创建一个"书签"，
# 之后可基于该书签派生出新的独立会话（fork）。
class SessionCheckpoint(BaseModel):
    """Message-level fork checkpoint metadata."""

    # 检查点 ID
    id: str
    # 锚定的消息 ID（从该消息处可以派生新会话）
    message_id: str
    # 检查点展示名称
    name: str
    # 创建时间
    created_at: datetime = Field(default_factory=utc_now)
    # 创建检查点时所在的运行（run）ID，便于回溯上下文
    source_run_id: Optional[str] = None
    # 创建检查点时所在的 trace ID，便于关联调用链
    source_trace_id: Optional[str] = None
    # 附加元数据，结构不固定
    metadata: dict[str, Any] = Field(default_factory=dict)


# 创建消息检查点的请求体。
class MessageCheckpointCreate(BaseModel):
    """Payload for creating a message checkpoint."""

    # 检查点名称，可选（为空时由服务端填充默认名称）
    name: Optional[str] = None


# 复制会话 fork（分叉）新会话时使用：在拷贝源会话 metadata 的基础上，
# 剔除只属于源会话、不应该被新会话继承的"瞬态分支状态"字段。
def clone_session_metadata(
    metadata: dict[str, Any] | None,
    *,
    include_checkpoints: bool = False,
) -> dict[str, Any]:
    """Return a copy of session metadata without transient branching state."""
    copied = deepcopy(metadata or {})
    if not include_checkpoints:
        # checkpoints 是源会话上记录的消息级检查点列表；除非显式要求保留
        # （include_checkpoints=True），否则新会话不应继承源会话的检查点
        copied.pop("checkpoints", None)
    # current_run_id 指向源会话"当前所在的运行"，属于瞬态运行状态，
    # 新会话应当拥有独立的运行状态，因此始终剔除
    copied.pop("current_run_id", None)
    return copied

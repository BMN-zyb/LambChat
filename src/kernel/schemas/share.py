"""
Share-related schemas.

Schema definitions for session sharing feature.
"""

# 模块说明：定义"会话分享"功能相关的数据模型。
# 用户可以把自己的一个会话（整个会话，或其中指定的若干次运行 run）生成一条分享链接，
# 供匿名/其他登录用户查看，而不需要暴露真实的会话 ID 或登录本账号。
# 主要使用方：src/infra/share/storage.py（分享记录的 MongoDB 存取层）、
# src/api/routes/share.py（创建/更新/查询分享的 HTTP 接口）。
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from src.infra.utils.datetime import utc_now


# 分享范围枚举：控制一次分享覆盖的内容多少。
class ShareType(str, Enum):
    """Share type enum."""

    # 分享整个会话的全部消息/运行记录
    FULL = "full"
    # 仅分享会话中指定的部分运行（run_ids），需配合 run_ids 字段使用
    PARTIAL = "partial"


# 分享可见性枚举：控制谁可以打开分享链接查看内容。
class ShareVisibility(str, Enum):
    """Share visibility enum."""

    # 公开：任何拿到链接的人（无需登录本系统）都可以查看
    PUBLIC = "public"
    # 需登录：仅登录本系统的用户才能查看分享内容
    AUTHENTICATED = "authenticated"


# 创建分享的请求体（对应 POST /share 等接口的入参）。
class ShareCreate(BaseModel):
    """Schema for creating a share."""

    # 要分享的会话 ID（即 Session.id）
    session_id: str
    # 分享范围，默认整份分享
    share_type: ShareType = ShareType.FULL
    run_ids: Optional[list[str]] = None  # Required when share_type=partial
    # 可见性，默认公开可见
    visibility: ShareVisibility = ShareVisibility.PUBLIC


# 更新分享的请求体，所有字段均可选，用于部分字段更新（PATCH 语义）。
class ShareUpdate(BaseModel):
    """Schema for updating a share."""

    share_type: Optional[ShareType] = None
    run_ids: Optional[list[str]] = None
    visibility: Optional[ShareVisibility] = None


# 分享记录的完整模型（数据库实体视图），对应 MongoDB 中一条分享文档。
class SharedSession(BaseModel):
    """Shared session model."""

    # 分享记录自身的数据库主键 ID
    id: str
    share_id: str  # Public share identifier (for URL)
    session_id: str  # Original session ID
    owner_id: str  # Owner user ID

    # Share scope
    share_type: ShareType
    run_ids: Optional[list[str]] = None

    # Access control
    visibility: ShareVisibility

    # Timestamps
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Config:
        # 允许通过属性访问的对象（如从 Mongo 文档转换出的对象）直接构造本模型
        from_attributes = True


# 创建/查询分享接口返回给调用方（分享所有者）的响应模型，比 SharedSession 多了可直接使用的 url。
class SharedSessionResponse(BaseModel):
    """Response model for share creation/retrieval."""

    id: str
    share_id: str
    url: str  # Share URL path
    session_id: str
    share_type: ShareType
    visibility: ShareVisibility
    run_ids: Optional[list[str]] = None
    created_at: datetime


# 分享列表页中的单条条目，比响应模型少了 url，多了 session_name 便于展示。
class SharedSessionListItem(BaseModel):
    """List item model for shares."""

    id: str
    share_id: str
    session_id: str
    # 所属会话的名称，便于列表展示；会话可能未命名故为可选
    session_name: Optional[str] = None
    share_type: ShareType
    visibility: ShareVisibility
    run_ids: Optional[list[str]] = None
    created_at: datetime


# "我的分享"列表接口的响应结构。
class ShareListResponse(BaseModel):
    """Response model for listing shares."""

    shares: list[SharedSessionListItem]
    total: int


# 分享内容页中展示的"分享人"信息，是脱敏后的简化用户信息（不含邮箱等敏感字段）。
class SharedContentOwner(BaseModel):
    """Owner info in shared content response."""

    username: str
    avatar_url: Optional[str] = None


# 供第三方/未登录用户查看分享内容的响应体，是分享功能真正对外展示数据的接口。
class SharedContentResponse(BaseModel):
    """Response model for viewing shared content."""

    session: dict  # Session info
    events: list[dict]  # Session events
    owner: SharedContentOwner
    share_type: ShareType
    run_ids: Optional[list[str]] = None
    # 事件是否因数量过多被截断（True 表示未返回全部事件）
    events_limited: bool = False
    # 当 events_limited=True 时，本次实际返回的事件数量上限
    events_limit: Optional[int] = None

"""Project-related schemas for session organization."""

# 模块说明：定义"项目"（Project，即会话分组/文件夹）相关的数据模型，
# 用于把用户的会话组织到不同分组中展示（类似聊天软件里的"文件夹"）。
# 其中 type="favorites" 的项目是系统为每个用户自动创建的特殊"收藏夹"分组，
# 由 src/infra/folder/storage.py 的 ensure_favorites_project 保证存在，
# 不允许用户通过创建/删除接口手动创建或删除（校验见 src/api/routes/project.py）。
# 主要使用方：src/infra/folder/storage.py（项目的增删改查）、
# src/api/routes/project.py（项目管理 HTTP 接口）。
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from src.infra.utils.datetime import utc_now


# 项目的公共基础字段，被创建请求与完整模型共同继承。
class ProjectBase(BaseModel):
    """Base project schema."""

    # 项目（分组）名称
    name: str
    # 项目类型："favorites" 为系统自动维护的收藏夹（特殊单例），"custom" 为用户自建分组
    type: str = "custom"  # "favorites" or "custom"
    # 展示图标：可以是 emoji，也可以是 lucide-react 的图标名称
    icon: str = "💬"  # emoji or lucide-react icon name, e.g. "💬", "⭐", "🤖"
    # 排序权重，值越小/大排在越前（具体排序方向由列表查询逻辑决定）
    sort_order: int = 0


# 创建项目的请求体，字段与 ProjectBase 一致；路由层会拦截 type="favorites" 的创建请求。
class ProjectCreate(ProjectBase):
    """Schema for creating a project."""

    pass


# 更新项目的请求体：所有字段均可选（PATCH 语义），且不包含 type，
# 即项目创建后类型不可通过该接口变更。
class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    name: Optional[str] = None
    icon: Optional[str] = None
    sort_order: Optional[int] = None


# 项目的完整模型（数据库实体视图）。
class Project(ProjectBase):
    """Project model."""

    # 项目 ID
    id: str
    # 所属用户 ID
    user_id: str
    # 创建时间
    created_at: datetime = Field(default_factory=utc_now)
    # 最近更新时间
    updated_at: datetime = Field(default_factory=utc_now)

    class Config:
        # 允许从属性对象（如 Mongo 文档转换后的对象）直接构造本模型
        from_attributes = True

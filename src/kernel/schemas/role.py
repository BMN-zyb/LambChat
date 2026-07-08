"""Role-related schemas."""

# 模块说明：定义 RBAC（基于角色的访问控制）中"角色"相关的数据模型。
# 角色（Role）是一组权限（Permission）、可用 Agent 列表与资源限额（RoleLimits）的集合，
# 用户通过被分配角色来获得相应的权限与限额，而不是直接给用户配置权限。
# 主要使用方：src/infra/role/manager.py、src/infra/role/storage.py（角色的增删改查与持久化）、
# src/infra/auth/rbac.py（鉴权时读取角色权限）、src/api/routes/role.py（角色管理接口）。
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.infra.utils.datetime import utc_now
from src.kernel.types import Permission


# 角色专属的资源限额配置，用于限制该角色下用户可使用的资源规模。
# 各字段默认语义均为 "None 表示不限制该角色专属上限，回退到全局默认值"。
class RoleLimits(BaseModel):
    """Role-specific limits configuration."""

    # 允许配置的渠道（Channel，如飞书/企微等）数量上限，null 表示不限制
    max_channels: Optional[int] = Field(
        default=None, description="Maximum number of channels allowed (null = unlimited)"
    )
    # 每用户最大并发聊天任务数，null 表示不限制，默认 5
    max_concurrent_chats: Optional[int] = Field(
        default=5, description="Per-user max concurrent chat tasks (null = unlimited, default: 5)"
    )
    # 每用户最大排队等待聊天任务数，null 表示不限制，默认 10
    max_queued_chats: Optional[int] = Field(
        default=10, description="Per-user max queued chat tasks (null = unlimited, default: 10)"
    )
    # 图片类文件的单文件上传大小上限（单位 MB），null 表示使用全局默认值
    max_file_size_image: Optional[int] = Field(
        default=None,
        description="Max file upload size for images in MB (null = use global default)",
    )
    # 视频类文件的单文件上传大小上限（单位 MB），null 表示使用全局默认值
    max_file_size_video: Optional[int] = Field(
        default=None,
        description="Max file upload size for videos in MB (null = use global default)",
    )
    # 音频类文件的单文件上传大小上限（单位 MB），null 表示使用全局默认值
    max_file_size_audio: Optional[int] = Field(
        default=None, description="Max file upload size for audio in MB (null = use global default)"
    )
    # 文档类文件的单文件上传大小上限（单位 MB），null 表示使用全局默认值
    max_file_size_document: Optional[int] = Field(
        default=None,
        description="Max file upload size for documents in MB (null = use global default)",
    )
    # 单次上传允许的最大文件个数，null 表示使用全局默认值
    max_files: Optional[int] = Field(
        default=None, description="Max number of files per upload (null = use global default)"
    )

    model_config = ConfigDict(extra="allow")  # Allow future extensions


# 角色的公共基础字段，被创建/完整角色模型共同继承。
class RoleBase(BaseModel):
    """Base role schema."""

    # 角色名称，长度限制 2~50
    name: str = Field(..., min_length=2, max_length=50)
    # 角色描述，可选
    description: Optional[str] = None


# 创建角色的请求体。
class RoleCreate(RoleBase):
    """Schema for creating a role."""

    # 授予该角色的权限点列表（取值见 src.kernel.types.Permission）
    permissions: List[Permission] = Field(default_factory=list)
    # 该角色允许使用的 Agent ID 列表；为空列表通常表示不限制或不允许任何 Agent，
    # 具体语义由业务层解释
    allowed_agents: List[str] = Field(default_factory=list, description="List of allowed agent IDs")
    # 角色专属资源限额，为空表示不设置角色级限额（使用全局默认）
    limits: Optional[RoleLimits] = Field(default=None, description="Role-specific limits")


# 更新角色的请求体，所有字段均可选（PATCH 语义，未传字段表示不修改）。
class RoleUpdate(BaseModel):
    """Schema for updating a role."""

    name: Optional[str] = Field(None, min_length=2, max_length=50)
    description: Optional[str] = None
    permissions: Optional[List[Permission]] = None
    allowed_agents: Optional[List[str]] = None
    limits: Optional[RoleLimits] = Field(None, description="Role-specific limits")


# 角色的完整模型（数据库实体视图）。
class Role(RoleBase):
    """Role model."""

    # from_attributes：支持从属性对象（如 Mongo 文档转换后的对象）构造；
    # use_enum_values：枚举字段（如 permissions 中的 Permission）序列化为其值而非枚举成员
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    # 角色 ID
    id: str
    permissions: List[Permission] = Field(default_factory=list)
    allowed_agents: List[str] = Field(default_factory=list, description="List of allowed agent IDs")
    limits: Optional[RoleLimits] = Field(default=None, description="Role-specific limits")
    is_system: bool = False  # System roles cannot be deleted
    # 创建时间
    created_at: datetime = Field(default_factory=utc_now)
    # 最近更新时间
    updated_at: datetime = Field(default_factory=utc_now)


# 角色分页列表的响应体。
class RoleListResponse(BaseModel):
    """Paginated role list."""

    # 当前页的角色列表
    roles: List[Role]
    # 满足条件的总数
    total: int
    # 本次查询跳过的条数（分页偏移量）
    skip: int
    # 本次查询返回的最大条数
    limit: int

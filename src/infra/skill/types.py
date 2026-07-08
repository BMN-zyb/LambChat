"""
技能子系统数据模型

定义技能市场元数据、用户技能、技能文件、技能元信息（__meta__）等 Pydantic 模型，
供 storage/manager/marketplace 等模块共享。
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class InstalledFrom(str, Enum):
    """Skill 安装来源"""

    # 来自技能市场安装
    MARKETPLACE = "marketplace"
    # 用户手动创建/上传
    MANUAL = "manual"


class MarketplaceSkill(BaseModel):
    """商城 Skill 元数据"""

    # skill_name 是市场内技能的唯一标识
    skill_name: str = Field(..., description="Skill 名称（唯一标识）")
    description: str = Field("", description="Skill 描述")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    version: str = Field("1.0.0", description="版本号")
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None
    is_active: bool = True


class MarketplaceSkillCreate(BaseModel):
    """创建商城 Skill 请求"""

    skill_name: str = Field(..., description="Skill 名称")
    description: str = Field("", description="Skill 描述")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    version: str = Field("1.0.0", description="版本号")


class MarketplaceSkillUpdate(BaseModel):
    """更新商城 Skill 请求"""

    # 全部可选：仅更新显式提供的字段
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    version: Optional[str] = None
    is_active: Optional[bool] = None


class SkillMeta(BaseModel):
    """Skill metadata stored as __meta__ doc in skill_files"""

    # 该元信息以 file_path="__meta__" 的特殊文档形式存于 skill_files 集合，
    # 记录技能的安装来源及其对应的市场名
    installed_from: InstalledFrom = InstalledFrom.MANUAL
    published_marketplace_name: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SkillFile(BaseModel):
    """Skill 文件"""

    # 一条记录 = 某用户某技能下的一个文件；(skill_name, user_id, file_path) 唯一
    skill_name: str
    user_id: str
    file_path: str
    content: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class UserSkill(BaseModel):
    """用户 Skill 响应"""

    skill_name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list, description="标签列表")
    files: list[str] = Field(default_factory=list, description="文件路径列表")
    # enabled 取自用户 metadata.disabled_skills 的反向计算（不在此禁用列表即启用）
    enabled: bool = True
    installed_from: Optional[str] = None
    published_marketplace_name: Optional[str] = None
    file_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # 是否已发布到市场，以及市场中是否仍有效
    is_published: bool = False
    marketplace_is_active: bool = True
    # 用户对该技能的展示偏好（收藏/置顶）
    is_favorite: bool = False
    is_pinned: bool = False


class UserSkillPreferenceUpdate(BaseModel):
    """Update the current user's presentation preferences for a skill."""

    # 仅更新收藏/置顶偏好，不影响技能内容
    is_favorite: Optional[bool] = None
    is_pinned: Optional[bool] = None


class UserSkillPreferenceResponse(BaseModel):
    """Current user's presentation preferences for a skill."""

    skill_name: str
    is_favorite: bool = False
    is_pinned: bool = False


class UserSkillListResponse(BaseModel):
    """Paginated user skill list."""

    # 分页返回的用户技能列表，附带启用计数与可选标签集合供前端筛选
    skills: list[UserSkill] = Field(default_factory=list)
    total: int = 0
    enabled_count: int = 0
    skip: int = 0
    limit: int = 100
    available_tags: list[str] = Field(default_factory=list)


class MarketplaceSkillResponse(BaseModel):
    """商城 Skill 响应"""

    skill_name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    version: str = "1.0.0"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None
    created_by_username: Optional[str] = None
    is_active: bool = True
    # is_owner：当前请求用户是否为该市场技能的发布者
    is_owner: bool = False
    file_count: int = 0


class PublishToMarketplaceRequest(BaseModel):
    """发布到商店的请求（可选覆盖 metadata）"""

    # 发布时可选覆盖名称/描述/标签/版本，未提供则沿用用户技能自身信息
    skill_name: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    version: Optional[str] = None

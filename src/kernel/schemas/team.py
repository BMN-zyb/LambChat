"""Team schemas."""

# 本模块定义"团队（Team）"相关的数据模型。团队是由多个人设预设
# （PersonaPreset）组成的多 Agent 协作单元：每个团队成员绑定一个
# persona_preset_id，可选覆盖 agent_id/model_id，团队路由器（见
# src/agents/team_agent/nodes.py）根据用户消息将任务分派给合适的成员，
# default_member_id 指定兜底成员。持久化与业务逻辑见
# src/infra/team/storage.py、src/infra/team/manager.py；
# src/api/routes/team.py 对外暴露团队的增删改查接口。
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.infra.utils.datetime import utc_now
from src.kernel.schemas.persona_preset import PersonaStarterPrompt

# 单个团队最多可设置的标签数量
TEAM_TAGS_MAX = 20
# 单个团队最多可包含的成员数量
TEAM_MEMBERS_MAX = 20
# 单个团队最多可配置的开场白（起始提示词）数量
TEAM_STARTER_PROMPTS_MAX = 20


# 团队的可见范围；目前仅支持私有（创建者本人可见），为将来扩展公开/组织可见等预留枚举
class TeamVisibility(str, Enum):
    PRIVATE = "private"


# 添加/配置团队成员的请求体：团队里的每个"成员"本质上是绑定了
# 某个人设预设的一个 Agent 角色位。
class TeamMemberCreate(BaseModel):
    """Request body for adding a member to a team."""

    # 成员在团队内的唯一标识；不传时由存储层自动生成（如 "m-xxxxxxxx"）
    member_id: Optional[str] = Field(None, min_length=1)
    # 该成员绑定的人设预设 ID（必填，决定该成员的系统提示词/开场白等）
    persona_preset_id: str = Field(..., min_length=1)
    # 该成员使用的 Agent 配置 ID，不设置则使用系统默认 Agent
    agent_id: Optional[str] = Field(None, min_length=1)
    # 该成员使用的模型 ID，不设置则使用系统默认模型
    model_id: Optional[str] = Field(None, min_length=1)
    # 成员的展示名称；创建/查询时通常会被对应人设预设的名称覆盖（见 TeamManager 的 hydrate 逻辑）
    role_name: str = Field(default="", max_length=80)
    # 成员的展示头像；同样通常会被人设预设的头像覆盖
    role_avatar: Optional[str] = None
    # 成员的标签；同样通常会被人设预设的标签覆盖
    role_tags: list[str] = Field(default_factory=list, max_length=TEAM_TAGS_MAX)
    # 附加在该成员系统提示词之后的补充指令，用于在团队场景下微调该成员的行为
    role_instructions: str = Field(default="", max_length=2000)
    # 成员在团队中的展示/排序位置，数值越小越靠前
    position: int = Field(default=0, ge=0)
    # 该成员是否启用；禁用的成员不会出现在 active_members 中，也不会被路由到
    enabled: bool = True


# 更新单个团队成员的请求体：所有字段均为 Optional，仅传入需要修改的字段
class TeamMemberUpdate(BaseModel):
    """Request body for updating a team member."""

    # 更换该成员绑定的人设预设
    persona_preset_id: Optional[str] = Field(None, min_length=1)
    # 更换该成员使用的 Agent 配置
    agent_id: Optional[str] = Field(None, min_length=1)
    # 更换该成员使用的模型
    model_id: Optional[str] = Field(None, min_length=1)
    role_name: Optional[str] = Field(None, max_length=80)
    role_avatar: Optional[str] = None
    role_tags: Optional[list[str]] = Field(None, max_length=TEAM_TAGS_MAX)
    # 更新附加指令
    role_instructions: Optional[str] = Field(None, max_length=2000)
    # 调整排序位置
    position: Optional[int] = Field(None, ge=0)
    # 启用/禁用该成员
    enabled: Optional[bool] = None


# API 响应中的团队成员视图（字段含义与 TeamMemberCreate 对应，
# 区别在于此处是已持久化的成员，member_id 必定存在）
class TeamMemberResponse(BaseModel):
    """Single team member in API responses."""

    # 成员在团队内的唯一标识
    member_id: str
    # 该成员绑定的人设预设 ID
    persona_preset_id: str
    # 该成员使用的 Agent 配置 ID
    agent_id: Optional[str] = None
    # 该成员使用的模型 ID
    model_id: Optional[str] = None
    # 成员展示名称（通常已由对应人设预设的名称"水合/hydrate"覆盖）
    role_name: str = ""
    # 成员展示头像（通常已由对应人设预设的头像覆盖）
    role_avatar: Optional[str] = None
    # 成员标签（通常已由对应人设预设的标签覆盖）
    role_tags: list[str] = Field(default_factory=list)
    # 附加在该成员系统提示词之后的补充指令
    role_instructions: str = ""
    # 成员在团队中的展示/排序位置
    position: int = 0
    # 该成员是否启用
    enabled: bool = True


# 创建团队的请求体
class TeamCreate(BaseModel):
    """Create team request."""

    # 团队名称
    name: str = Field(..., min_length=1, max_length=80)
    # 团队描述
    description: str = Field(default="", max_length=500)
    # 团队头像
    avatar: Optional[str] = None
    # 团队标签（自动去重，见下方校验器）
    tags: list[str] = Field(default_factory=list, max_length=TEAM_TAGS_MAX)
    # 团队成员列表
    members: list[TeamMemberCreate] = Field(default_factory=list, max_length=TEAM_MEMBERS_MAX)
    # 默认成员 ID：当团队路由器无法判断该由哪个成员处理消息时兜底委派给该成员；
    # 若指定的 ID 不在成员列表中，存储层会回退为成员列表的第一个成员
    default_member_id: Optional[str] = None
    # 团队级别的通用指令，会附加到团队内每个成员的系统提示词中
    team_instructions: str = Field(default="", max_length=4000)
    # 团队整体的开场白（起始提示词）建议列表
    starter_prompts: list[PersonaStarterPrompt] = Field(
        default_factory=list,
        max_length=TEAM_STARTER_PROMPTS_MAX,
    )

    # 对 tags 做清洗：去除首尾空白、丢弃空字符串，并按出现顺序去重
    @field_validator("tags")
    @classmethod
    def _dedupe_tags(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            item = value.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result


# 更新团队的请求体：所有字段均为 Optional，仅传入需要修改的字段；
# members 若传入，通常按"整体替换"语义处理（具体见存储层实现）。
class TeamUpdate(BaseModel):
    """Update team request."""

    name: Optional[str] = Field(None, min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    avatar: Optional[str] = None
    tags: Optional[list[str]] = Field(None, max_length=TEAM_TAGS_MAX)
    members: Optional[list[TeamMemberCreate]] = Field(None, max_length=TEAM_MEMBERS_MAX)
    default_member_id: Optional[str] = None
    team_instructions: Optional[str] = Field(None, max_length=4000)
    starter_prompts: Optional[list[PersonaStarterPrompt]] = Field(
        None,
        max_length=TEAM_STARTER_PROMPTS_MAX,
    )

    # 复用 TeamCreate 的去重逻辑；None 表示本次不修改 tags
    @field_validator("tags")
    @classmethod
    def _dedupe_optional_tags(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return TeamCreate._dedupe_tags(values)


# 更新当前用户对某个团队的个性化展示偏好（收藏/置顶），
# 与团队本身的内容/配置无关，因此单独拆出一个轻量请求体
class TeamPreferenceUpdate(BaseModel):
    """Update the current user's presentation preferences for a team."""

    # 是否收藏该团队
    is_favorite: Optional[bool] = None
    # 是否置顶该团队
    is_pinned: Optional[bool] = None


# 团队的完整响应模型：既是 API 返回给前端的团队详情结构，
# 也是团队路由逻辑（src/agents/team_agent/nodes.py）在运行时读取的团队定义。
class TeamResponse(BaseModel):
    """Team response model."""

    # 允许直接从 ORM/对象属性构建模型实例
    model_config = ConfigDict(from_attributes=True)

    # 团队唯一 ID
    id: str
    # 团队创建者/拥有者的用户 ID
    owner_user_id: str
    # 团队名称
    name: str
    # 团队描述
    description: str = ""
    # 团队头像
    avatar: Optional[str] = None
    # 团队标签
    tags: list[str] = Field(default_factory=list, max_length=TEAM_TAGS_MAX)
    # 团队成员列表（含已禁用成员，如需仅获取启用成员请使用 active_members）
    members: list[TeamMemberResponse] = Field(default_factory=list, max_length=TEAM_MEMBERS_MAX)
    # 默认（兜底）成员 ID
    default_member_id: Optional[str] = None
    # 团队级别的通用指令
    team_instructions: str = ""
    # 团队整体的开场白建议列表
    starter_prompts: list[PersonaStarterPrompt] = Field(
        default_factory=list,
        max_length=TEAM_STARTER_PROMPTS_MAX,
    )
    # 团队可见范围，目前恒为 PRIVATE
    visibility: TeamVisibility = TeamVisibility.PRIVATE
    # 当前用户是否收藏了该团队
    is_favorite: bool = False
    # 当前用户是否置顶了该团队
    is_pinned: bool = False
    # 该团队最近一次被使用（发起对话）的时间
    last_used_at: Optional[datetime] = None
    # 创建时间
    created_at: datetime = Field(default_factory=utc_now)
    # 最近更新时间
    updated_at: datetime = Field(default_factory=utc_now)

    # 仅返回已启用的成员，供团队路由逻辑挑选可用成员时使用
    @property
    def active_members(self) -> list[TeamMemberResponse]:
        return [m for m in self.members if m.enabled]


# 分页查询团队列表的响应体
class TeamListResponse(BaseModel):
    """Paginated team list."""

    # 当前页的团队列表
    teams: list[TeamResponse]
    # 满足条件的团队总数
    total: int
    # 本次查询跳过的记录数（分页偏移量）
    skip: int = 0
    # 本次查询返回的最大记录数
    limit: int = 100

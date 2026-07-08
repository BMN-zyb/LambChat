"""Persona preset schemas."""

# 本模块定义"人设预设（Persona Preset）"相关的数据模型。人设预设封装了
# 一份可复用的 Agent 人格设定：系统提示词、开场白建议、可用技能列表等，
# 用户开始对话时选择某个预设即可快速套用这套人格（见 PersonaPresetSnapshot）。
# 业务逻辑与权限判断见 src/infra/persona_preset/manager.py，持久化见
# src/infra/persona_preset/storage.py；src/api/routes/persona_preset.py
# 对外暴露增删改查/复制/使用等接口；team.py 中的团队成员也是绑定到
# 某个人设预设来定义其"人格"。
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.infra.utils.datetime import utc_now


# 预设的归属范围，决定了可见性与编辑权限的判断方式
class PersonaPresetScope(str, Enum):
    """Preset ownership scope."""

    # 全局预设：由管理员创建，面向所有用户；只有管理员可编辑，
    # 普通用户仅当 visibility=PUBLIC 且 status=PUBLISHED 时才可见
    GLOBAL = "global"
    # 个人预设：归属某个用户（owner_user_id），只有该用户本人可见/可编辑
    USER = "user"


# 预设的可见性标记；目前仅对 GLOBAL 预设的"是否允许普通用户查看"生效
class PersonaPresetVisibility(str, Enum):
    """Preset visibility."""

    # 公开：配合 PUBLISHED 状态可被普通用户查看
    PUBLIC = "public"
    # 私有：仅创建者/管理员可见
    PRIVATE = "private"


# 预设的发布状态；目前仅对 GLOBAL 预设的"是否允许普通用户查看"生效
class PersonaPresetStatus(str, Enum):
    """Preset publication status."""

    # 草稿：尚在编辑，未正式发布
    DRAFT = "draft"
    # 已发布：配合 PUBLIC 可见性可被普通用户查看
    PUBLISHED = "published"
    # 已归档：不再对普通用户展示，但记录仍保留
    ARCHIVED = "archived"


# 选择某个人设/团队后展示给用户的一条"开场白建议"（点击即可作为消息发出）。
# 同时被 team.py 的团队开场白字段复用。
class PersonaStarterPrompt(BaseModel):
    """Prompt suggestion shown after selecting a persona."""

    # 展示用的图标（可选）
    icon: Optional[str] = None
    # 提示词文案：可以是单一字符串，也可以是 {语言代码: 文案} 的多语言字典
    text: str | dict[str, str]

    # 去除首尾空白；空字符串归一化为 None
    @field_validator("icon")
    @classmethod
    def _normalize_icon(cls, value: str | None) -> str | None:
        if value is None:
            return None
        item = value.strip()
        return item or None

    # 校验并归一化 text：字符串直接去除首尾空白（不允许为空）；
    # 字典则逐个清洗 key/value 并丢弃空的语言条目，整体清洗后也不允许为空
    @field_validator("text")
    @classmethod
    def _normalize_text(cls, value: str | dict[str, str]) -> str | dict[str, str]:
        if isinstance(value, str):
            item = value.strip()
            if not item:
                raise ValueError("starter_prompt_text_required")
            return item

        result: dict[str, str] = {}
        for lang, text in value.items():
            lang_key = str(lang).strip()
            localized_text = str(text).strip()
            if lang_key and localized_text:
                result[lang_key] = localized_text
        if not result:
            raise ValueError("starter_prompt_text_required")
        return result


# 人设预设的公共字段集合，供 PersonaPresetCreate 等模型继承复用
class PersonaPresetBase(BaseModel):
    """Common persona preset fields."""

    # 预设名称
    name: str = Field(..., min_length=1, max_length=80)
    # 预设描述
    description: str = Field(default="", max_length=500)
    # 预设头像
    avatar: Optional[str] = None
    # 标签（自动去重，见下方校验器）
    tags: list[str] = Field(default_factory=list)
    # 该人设的系统提示词，决定 Agent 的人格/行为
    system_prompt: str = Field(..., min_length=1)
    # 选中该人设后展示的开场白建议列表
    starter_prompts: list[PersonaStarterPrompt] = Field(default_factory=list)
    # 该人设启用时可使用的技能名称列表（对应 Agent 技能系统中的 skill 名）
    skill_names: list[str] = Field(default_factory=list)
    # 归属范围，默认创建为个人预设
    scope: PersonaPresetScope = PersonaPresetScope.USER
    # 可见性，默认私有
    visibility: PersonaPresetVisibility = PersonaPresetVisibility.PRIVATE
    # 发布状态，默认草稿
    status: PersonaPresetStatus = PersonaPresetStatus.DRAFT

    # 对 tags、skill_names 做统一清洗：去除首尾空白、丢弃空字符串，并按出现顺序去重
    @field_validator("tags", "skill_names")
    @classmethod
    def _dedupe_strings(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            item = value.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result


# 创建人设预设的请求体：字段与 PersonaPresetBase 完全一致，未额外增删字段
class PersonaPresetCreate(PersonaPresetBase):
    """Create persona preset request."""


# 更新人设预设的请求体：所有字段均为 Optional，仅传入需要修改的字段。
# 每次成功更新会使 version 自增（见 PersonaPresetManager 的更新逻辑）。
class PersonaPresetUpdate(BaseModel):
    """Update persona preset request."""

    name: Optional[str] = Field(None, min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    avatar: Optional[str] = None
    tags: Optional[list[str]] = None
    system_prompt: Optional[str] = Field(None, min_length=1)
    starter_prompts: Optional[list[PersonaStarterPrompt]] = None
    skill_names: Optional[list[str]] = None
    scope: Optional[PersonaPresetScope] = None
    visibility: Optional[PersonaPresetVisibility] = None
    status: Optional[PersonaPresetStatus] = None

    # 复用 PersonaPresetBase 的去重逻辑；None 表示本次不修改该字段
    @field_validator("tags", "skill_names")
    @classmethod
    def _dedupe_optional_strings(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return PersonaPresetBase._dedupe_strings(values)


# 更新当前用户对某个人设预设的个性化展示偏好（收藏/置顶），
# 与预设本身的内容/配置无关，因此单独拆出一个轻量请求体
class PersonaPresetPreferenceUpdate(BaseModel):
    """Update the current user's presentation preferences for a preset."""

    # 是否收藏该预设
    is_favorite: Optional[bool] = None
    # 是否置顶该预设
    is_pinned: Optional[bool] = None


# 人设预设的完整响应模型，是持久化文档对外的完整视图
class PersonaPreset(BaseModel):
    """Persona preset response model."""

    # 允许直接从 ORM/对象属性构建模型实例
    model_config = ConfigDict(from_attributes=True)

    # 预设唯一 ID
    id: str
    # 归属范围（GLOBAL/USER）
    scope: PersonaPresetScope
    # 拥有者用户 ID；GLOBAL 预设通常为 None
    owner_user_id: Optional[str] = None
    # 预设名称
    name: str
    # 预设描述
    description: str = ""
    # 预设头像
    avatar: Optional[str] = None
    # 标签
    tags: list[str] = Field(default_factory=list)
    # 系统提示词
    system_prompt: str
    # 开场白建议列表
    starter_prompts: list[PersonaStarterPrompt] = Field(default_factory=list)
    # 可用技能名称列表
    skill_names: list[str] = Field(default_factory=list)
    # 可见性
    visibility: PersonaPresetVisibility
    # 发布状态
    status: PersonaPresetStatus
    # 若该预设是由其他预设复制而来，记录源预设的 ID；原创预设为 None
    source_preset_id: Optional[str] = None
    # 复制时源预设当时的版本号，用于追溯"复制自哪个版本"
    copied_from_version: Optional[int] = None
    # 预设内容版本号，每次更新内容会自增
    version: int = 1
    # 该预设被使用（发起对话）的累计次数
    usage_count: int = 0
    # 当前用户是否收藏了该预设
    is_favorite: bool = False
    # 当前用户是否置顶了该预设
    is_pinned: bool = False
    # 最近一次被使用的时间
    last_used_at: Optional[datetime] = None
    # 创建者用户 ID
    created_by: Optional[str] = None
    # 最近一次更新者的用户 ID
    updated_by: Optional[str] = None
    # 创建时间
    created_at: datetime = Field(default_factory=utc_now)
    # 最近更新时间
    updated_at: datetime = Field(default_factory=utc_now)


# 用户"使用"某个人设预设发起会话时生成的不可变运行时快照：
# 将当时的人设内容固化下来并随会话保存，即便之后原预设被修改/删除，
# 已发起的会话仍保持当初选定的人格设定不变。
class PersonaPresetSnapshot(BaseModel):
    """Immutable runtime snapshot saved with a chat session."""

    # 来源预设 ID
    preset_id: str
    # 预设名称（快照时刻）
    name: str
    # 系统提示词（快照时刻）
    system_prompt: str
    # 开场白建议（快照时刻）
    starter_prompts: list[PersonaStarterPrompt] = Field(default_factory=list)
    # 实际可加载的技能名称列表（已按当前用户可用的技能集合过滤）
    skill_names: list[str] = Field(default_factory=list)
    # 预设中配置了但当前不可用/已不存在的技能名称（用于提示用户或排查问题）
    missing_skill_names: list[str] = Field(default_factory=list)
    # 来源预设在快照时刻的版本号
    version: int = 1
    # 预设头像（快照时刻）
    avatar: Optional[str] = None


# 分页查询人设预设列表的响应体
class PersonaPresetListResponse(BaseModel):
    """Paginated persona preset list."""

    # 当前页的预设列表
    presets: list[PersonaPreset]
    # 满足条件的预设总数
    total: int
    # 本次查询跳过的记录数（分页偏移量）
    skip: int = 0
    # 本次查询返回的最大记录数
    limit: int = 100

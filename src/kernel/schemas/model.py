"""Model-related schemas."""

# 本模块定义"模型配置"（LLM 模型接入配置）相关的数据模型。
# ModelConfig 由管理员在设置页面维护，持久化在数据库中（参见
# src/infra/agent/model_storage.py）；运行时 src/infra/llm/client.py
# 根据 ModelConfig.value 解析 provider 并创建实际的 LLM 客户端；
# src/api/routes/agent/model.py 对外暴露模型的增删改查接口；
# AvailableModel/AvailableModelListResponse 用于向非管理员用户暴露
# 经过脱敏、过滤内部字段后的模型选择列表。
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# 模型的能力/行为画像配置，用于描述某个模型在调用层面的特殊约束或能力，
# 而非鉴权/路由信息。extra="ignore" 表示未识别的多余字段会被忽略而非报错，
# 便于向前兼容未来新增的字段。
class ModelProfile(BaseModel):
    """Per-model profile configuration."""

    model_config = ConfigDict(extra="ignore")

    # 该模型允许的最大输入 token 数，None 表示不做额外限制（使用模型默认值）
    max_input_tokens: Optional[int] = Field(None, description="Max input tokens for this model")
    # 该模型是否支持图片（视觉）输入
    supports_vision: Optional[bool] = Field(
        False,
        description="Whether this model accepts image input",
    )
    # 是否需要在调用模型前，将消息中的 image_url 图片块转换为 base64 data URL
    # （部分模型/网关只接受 base64，不支持直接传入图片 URL）
    image_url_to_base64: Optional[bool] = Field(
        False,
        description="Whether image_url blocks should be converted to base64 data URLs before model calls",
    )


# 存储在数据库中的模型配置——即管理员在"模型管理"中新增的一条可用 LLM 配置，
# 是 ModelConfigCreate/ModelConfigUpdate 请求落库后的完整视图，
# 同时也是 ModelResponse、ModelListResponse 等响应体的载荷类型。
class ModelConfig(BaseModel):
    """Model configuration stored in database."""

    # populate_by_name=True：允许同时按字段名或 alias 赋值/序列化（为未来扩展预留）
    model_config = ConfigDict(populate_by_name=True)

    # 模型配置的唯一 ID；创建时若不提供则由存储层自动生成
    id: Optional[str] = Field(None, description="Model ID (auto-generated if not provided)")
    # 模型标识，通常形如 "provider/model-name"（如 anthropic/claude-3-5-sonnet），
    # 是调用 LLM 时真正使用的模型名
    value: str = Field(..., description="Model identifier (e.g., anthropic/claude-3-5-sonnet)")
    # 显式指定的 LLM 提供商；未设置时由 value 中的前缀自动推断
    provider: Optional[str] = Field(
        None,
        description="Explicit LLM provider (e.g. openai/anthropic/google/deepseek). Auto-detected from value if not set.",
    )
    # 显式指定前端展示用的图标 slug；未设置时按 provider/模型名推断默认图标
    icon: Optional[str] = Field(
        None,
        description="Explicit display icon slug. Falls back to provider/model inference when not set.",
    )
    # 前端展示的模型名称（如 "Claude 3.5 Sonnet"）
    label: str = Field(..., description="Display name for the model")
    # 模型的补充说明文字
    description: Optional[str] = Field(None, description="Model description")
    # 针对该模型单独设置的 API Key，覆盖全局默认配置
    api_key: Optional[str] = Field(None, description="Per-model API key override")
    # 针对该模型单独设置的 API Base URL，覆盖全局默认配置
    api_base: Optional[str] = Field(None, description="Per-model API base URL override")
    # 针对该模型单独设置的 temperature，覆盖全局默认配置
    temperature: Optional[float] = Field(None, description="Per-model temperature override")
    # 针对该模型单独设置的最大生成 token 数，覆盖全局默认配置
    max_tokens: Optional[int] = Field(None, description="Per-model max tokens override")
    # 该模型的能力画像配置（见 ModelProfile）
    profile: Optional[ModelProfile] = Field(None, description="Per-model profile settings")
    # 当该模型调用失败时用于重试的备用模型 ID（引用另一条 ModelConfig 的 id），
    # 运行时由 resolve_fallback_model 解析为实际的模型 value
    fallback_model: Optional[str] = Field(
        None, description="Fallback model ID (UUID) when this model fails"
    )
    # 该模型是否启用；禁用后不会出现在可选模型列表中
    enabled: bool = Field(True, description="Whether this model is enabled")
    # 在模型列表中的展示顺序，数值越小越靠前
    order: int = Field(0, description="Display order")
    # 创建时间
    created_at: Optional[datetime] = Field(None, description="Creation timestamp")
    # 最后更新时间
    updated_at: Optional[datetime] = Field(None, description="Last update timestamp")


# 创建模型配置的请求体：管理员新增一条 LLM 模型配置时提交的字段，
# 不包含 id/created_at/updated_at（由服务端生成）。
class ModelConfigCreate(BaseModel):
    """Create a new model configuration."""

    # 模型标识，通常形如 "provider/model-name"（如 anthropic/claude-3-5-sonnet）
    value: str = Field(..., description="Model identifier (e.g., anthropic/claude-3-5-sonnet)")
    # 显式指定的 LLM 提供商；未设置时由 value 中的前缀自动推断
    provider: Optional[str] = Field(
        None,
        description="Explicit LLM provider (e.g. openai/anthropic/google/deepseek). Auto-detected from value if not set.",
    )
    # 显式指定前端展示用的图标 slug；未设置时按 provider/模型名推断默认图标
    icon: Optional[str] = Field(
        None,
        description="Explicit display icon slug. Falls back to provider/model inference when not set.",
    )
    # 前端展示的模型名称
    label: str = Field(..., description="Display name for the model")
    # 模型的补充说明文字
    description: Optional[str] = Field(None, description="Model description")
    # 针对该模型单独设置的 API Key，覆盖全局默认配置
    api_key: Optional[str] = Field(None, description="Per-model API key override")
    # 针对该模型单独设置的 API Base URL，覆盖全局默认配置
    api_base: Optional[str] = Field(None, description="Per-model API base URL override")
    # 针对该模型单独设置的 temperature，覆盖全局默认配置
    temperature: Optional[float] = Field(None, description="Per-model temperature override")
    # 针对该模型单独设置的最大生成 token 数，覆盖全局默认配置
    max_tokens: Optional[int] = Field(None, description="Per-model max tokens override")
    # 该模型的能力画像配置（见 ModelProfile）
    profile: Optional[ModelProfile] = Field(None, description="Per-model profile settings")
    # 当该模型调用失败时用于重试的备用模型 ID（引用另一条 ModelConfig 的 id）
    fallback_model: Optional[str] = Field(
        None, description="Fallback model ID (UUID) when this model fails"
    )
    # 该模型是否启用，默认创建即启用
    enabled: bool = Field(True, description="Whether this model is enabled")
    # 在模型列表中的展示顺序，数值越小越靠前
    order: Optional[int] = Field(0, description="Display order")


# 更新模型配置的请求体：所有字段均为 Optional，仅传入需要修改的字段
# （None 表示"不修改"，而非"清空"），value 字段不允许通过该接口修改。
class ModelConfigUpdate(BaseModel):
    """Update an existing model configuration."""

    # 显式指定/覆盖 LLM 提供商
    provider: Optional[str] = Field(None, description="Explicit LLM provider override")
    # 显式指定/覆盖展示图标 slug
    icon: Optional[str] = Field(None, description="Explicit display icon slug override")
    # 前端展示的模型名称
    label: Optional[str] = Field(None, description="Display name for the model")
    # 模型的补充说明文字
    description: Optional[str] = Field(None, description="Model description")
    # 针对该模型单独设置的 API Key，覆盖全局默认配置
    api_key: Optional[str] = Field(None, description="Per-model API key override")
    # 针对该模型单独设置的 API Base URL，覆盖全局默认配置
    api_base: Optional[str] = Field(None, description="Per-model API base URL override")
    # 针对该模型单独设置的 temperature，覆盖全局默认配置
    temperature: Optional[float] = Field(None, description="Per-model temperature override")
    # 针对该模型单独设置的最大生成 token 数，覆盖全局默认配置
    max_tokens: Optional[int] = Field(None, description="Per-model max tokens override")
    # 该模型的能力画像配置（见 ModelProfile）
    profile: Optional[ModelProfile] = Field(None, description="Per-model profile settings")
    # 当该模型调用失败时用于重试的备用模型 ID（引用另一条 ModelConfig 的 id）
    fallback_model: Optional[str] = Field(
        None, description="Fallback model ID (UUID) when this model fails"
    )
    # 该模型是否启用
    enabled: Optional[bool] = Field(None, description="Whether this model is enabled")
    # 在模型列表中的展示顺序，数值越小越靠前
    order: Optional[int] = Field(None, description="Display order")


# 管理端"列出所有模型"接口的响应体，包含完整的 ModelConfig
# （含 api_key 等敏感字段，实际返回前应配合 mask_api_key 脱敏）。
class ModelListResponse(BaseModel):
    """Response for listing all models."""

    # 模型配置完整列表
    models: list[ModelConfig] = Field(
        default_factory=list, description="List of model configurations"
    )
    # 模型总数
    count: int = Field(0, description="Total number of models")
    # 已启用的模型数量
    enabled_count: int = Field(0, description="Number of enabled models")


# 面向普通（非管理员）用户的公开模型信息，不包含 api_key/api_base/
# temperature/max_tokens/fallback_model 等鉴权与路由内部细节，
# 由 to_available_model() 从 ModelConfig 转换而来。
class AvailableModel(BaseModel):
    """Public model information safe for non-admin model selectors."""

    # 模型配置 ID
    id: Optional[str] = Field(None, description="Model ID")
    # 模型标识（如 anthropic/claude-3-5-sonnet）
    value: str = Field(..., description="Model identifier")
    # LLM 提供商
    provider: Optional[str] = Field(None, description="LLM provider")
    # 展示图标 slug
    icon: Optional[str] = Field(None, description="Explicit display icon slug")
    # 前端展示的模型名称
    label: str = Field(..., description="Display name for the model")
    # 模型的补充说明文字
    description: Optional[str] = Field(None, description="Model description")
    # 该模型的能力画像配置（见 ModelProfile），前端据此判断是否展示图片上传等能力
    profile: Optional[ModelProfile] = Field(None, description="Per-model profile settings")


# 面向当前登录用户的"可见模型列表"响应体：根据该用户的角色/权限过滤后
# 的模型集合（普通用户可能看不到全部模型），用于聊天界面的模型选择器。
class AvailableModelListResponse(BaseModel):
    """Response for listing models visible to the current user."""

    # 当前用户可见的公开模型列表
    models: list[AvailableModel] = Field(
        default_factory=list, description="List of public model entries"
    )
    # 可见模型总数
    count: int = Field(0, description="Number of visible models")
    # 可见且已启用的模型数量
    enabled_count: int = Field(0, description="Number of visible enabled models")
    # 在当前用户可见的模型集合范围内，实际生效的默认模型 ID
    default_model_id: Optional[str] = Field(
        None,
        description="Effective default model ID for this user's visible model set",
    )


# 将内部 ModelConfig 转换为对外安全的 AvailableModel（供模型选择器等
# 非管理端场景使用），仅挑选允许暴露的字段，丢弃 api_key 等敏感/路由字段。
def to_available_model(model: ModelConfig) -> AvailableModel:
    """Return a public model view without credentials or routing internals."""
    return AvailableModel(
        id=model.id,
        value=model.value,
        provider=model.provider,
        icon=model.icon,
        label=model.label,
        description=model.description,
        profile=model.profile,
    )


# 管理端展示模型配置列表时，避免把完整 API Key 明文返回给前端，
# 用于生成 ModelConfig.api_key 的脱敏展示副本。
def mask_api_key(model: ModelConfig) -> ModelConfig:
    """Return a copy of the model with the API key masked for safe display."""
    if model.api_key:
        key = model.api_key
        # 长度大于 8 时保留首尾各 4 位、中间用 "..." 代替；否则整体显示为 "****"
        masked = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "****"
        return model.model_copy(update={"api_key": masked})
    return model


# 单个模型操作（创建/更新/查询单条）的通用响应体
class ModelResponse(BaseModel):
    """Response for a single model operation."""

    # 操作后的模型配置
    model: ModelConfig = Field(..., description="The model configuration")
    # 可选的提示信息
    message: Optional[str] = Field(None, description="Optional success message")

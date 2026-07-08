"""Feishu/Lark channel configuration schemas."""

# 模块说明：定义飞书/Lark 消息渠道（Channel）的配置相关数据模型。
# 涵盖创建/更新配置的请求体、数据库存储视图（含明文密钥）、
# 对外响应（脱敏，不回传 app_secret 等敏感信息）以及渠道连接状态模型。
# 主要使用方：src/infra/channel/feishu/*（配置存取、事件处理、消息收发等）、
# 对应的飞书渠道管理 HTTP 接口。
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from src.infra.utils.datetime import utc_now

# 默认的语音转写提示词：当用户在飞书发送语音消息且开启了 auto_transcribe_audio 时，
# 会将该提示词发给 Agent，引导其调用 audio_transcribe 工具处理语音附件。
DEFAULT_AUDIO_TRANSCRIBE_PROMPT = (
    "Please transcribe and understand this voice message. "
    "Use the audio_transcribe tool for the attached audio when needed."
)


# 群聊消息处理策略枚举。
class FeishuGroupPolicy(str, Enum):
    """Group message handling policy."""

    # 开放模式：响应群聊中的所有消息
    OPEN = "open"  # Respond to all group messages
    # 提及模式（默认）：仅当消息中 @ 了机器人时才响应
    MENTION = "mention"  # Respond only when @mentioned


# 飞书配置的公共字段集合，被创建请求与数据库视图共同继承，避免重复定义。
class FeishuConfigBase(BaseModel):
    """Base Feishu configuration schema."""

    # 实例标识，用于支持同一用户下配置多个飞书应用实例（多机器人场景）
    instance_id: str = Field("", description="Instance ID for multi-instance support")
    # 飞书/Lark 应用的 App ID
    app_id: str = Field(..., description="Feishu/Lark App ID")
    # 飞书/Lark 应用的 App Secret（敏感信息，仅内部存储，不对外返回明文）
    app_secret: str = Field(..., description="Feishu/Lark App Secret")
    # 事件订阅回调的加密密钥（可选，飞书开启加密时使用）
    encrypt_key: str = Field("", description="Encrypt key for event encryption (optional)")
    # 事件订阅回调的验证 Token（可选）
    verification_token: str = Field(
        "", description="Verification token for event verification (optional)"
    )
    # 收到消息后自动添加的表情回应（飞书表情名称，如 THUMBSUP）
    react_emoji: str = Field("THUMBSUP", description="Emoji reaction when receiving a message")
    # 群聊消息处理策略，默认仅 @ 机器人时才响应
    group_policy: FeishuGroupPolicy = Field(
        FeishuGroupPolicy.MENTION, description="Group message policy"
    )
    # 是否通过飞书 CardKit 卡片实现流式更新回复（边生成边刷新卡片内容）
    stream_reply: bool = Field(True, description="Stream replies through Feishu CardKit")
    # 是否自动请求 Agent 转写收到的语音附件
    auto_transcribe_audio: bool = Field(
        True, description="Ask the agent to transcribe incoming audio attachments"
    )
    # 收到语音消息时发送给 Agent 的提示词，默认见 DEFAULT_AUDIO_TRANSCRIBE_PROMPT
    audio_transcribe_prompt: str = Field(
        DEFAULT_AUDIO_TRANSCRIBE_PROMPT,
        description="Prompt sent to the agent when an audio message arrives",
    )
    # 该飞书渠道是否启用
    enabled: bool = Field(True, description="Whether the channel is enabled")


# 创建飞书配置的请求体，字段与 FeishuConfigBase 完全一致（不新增字段）。
class FeishuConfigCreate(FeishuConfigBase):
    """Schema for creating Feishu configuration."""

    pass


# 更新飞书配置的请求体：所有字段均可选（PATCH 语义，未传字段表示不修改），
# 字段含义同 FeishuConfigBase。
class FeishuConfigUpdate(BaseModel):
    """Schema for updating Feishu configuration."""

    # extra="forbid"：禁止传入未定义的额外字段，避免误传被静默忽略
    model_config = ConfigDict(extra="forbid")

    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    encrypt_key: Optional[str] = None
    verification_token: Optional[str] = None
    react_emoji: Optional[str] = None
    group_policy: Optional[FeishuGroupPolicy] = None
    stream_reply: Optional[bool] = None
    auto_transcribe_audio: Optional[bool] = None
    audio_transcribe_prompt: Optional[str] = None
    enabled: Optional[bool] = None


# 数据库存储/内部使用的完整配置视图：在 Base 基础上补充归属用户与时间戳。
# 注意该模型包含 app_secret 等敏感字段明文，仅供内部逻辑使用，不能直接对外返回。
class FeishuConfig(FeishuConfigBase):
    """Feishu configuration model (database view)."""

    # 配置所属用户 ID
    user_id: str
    # 创建时间
    created_at: datetime = Field(default_factory=utc_now)
    # 最近更新时间
    updated_at: datetime = Field(default_factory=utc_now)

    class Config:
        # 允许从属性对象（如 Mongo 文档转换后的对象）直接构造本模型
        from_attributes = True


# 对外返回的飞书配置响应：对敏感字段做脱敏/掩码处理。
class FeishuConfigResponse(BaseModel):
    """Feishu configuration response (masked sensitive fields)."""

    user_id: str
    app_id: str  # Can show app_id (not sensitive)
    # 是否已配置 app_secret；仅返回布尔标记，不回传密钥内容本身
    has_app_secret: bool  # Only show if secret is set
    encrypt_key: str = ""  # Masked
    verification_token: str = ""  # Masked
    react_emoji: str = "THUMBSUP"
    group_policy: FeishuGroupPolicy = FeishuGroupPolicy.MENTION
    stream_reply: bool = True
    auto_transcribe_audio: bool = True
    audio_transcribe_prompt: str = DEFAULT_AUDIO_TRANSCRIBE_PROMPT
    enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# 飞书渠道的连接状态，供前端展示"已连接/未连接"及错误详情。
class FeishuConfigStatus(BaseModel):
    """Feishu connection status."""

    # 该渠道是否启用（对应配置中的 enabled）
    enabled: bool
    # 是否已成功建立连接（如长连接/事件订阅是否处于活跃状态）
    connected: bool = False
    # 最近一次连接失败的错误信息
    error_message: Optional[str] = None
    # 最近一次成功连接的时间
    last_connected_at: Optional[datetime] = None

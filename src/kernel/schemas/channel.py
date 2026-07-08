"""Generic channel configuration schemas.

Supports multiple chat platforms (Feishu, WeChat, DingTalk, Slack, etc.)
with a unified interface.
"""

# 本模块定义"外部聊天平台接入（Channel）"相关的数据模型：一个 Channel
# 代表用户配置的一个外部平台机器人实例（如某个飞书应用），可绑定
# agent_id/model_id/project_id/team_id/persona_preset_id 来控制该实例
# 收到消息后具体如何被路由处理。当前仅飞书（Feishu）已落地实现，
# 其余渠道类型（WeChat/DingTalk/Slack/Telegram/Discord）在 ChannelType
# 中以注释占位，尚未启用。主要调用方：src/infra/channel/manager.py、
# registry.py、pubsub.py、channel_storage.py、feishu/ 目录下的具体实现，
# 以及 src/api/routes/channels.py 对外暴露的渠道管理接口。
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field


# 支持接入的外部聊天平台类型枚举
class ChannelType(str, Enum):
    """Supported channel types."""

    # 飞书（目前唯一已实现的渠道类型）
    FEISHU = "feishu"
    # Future channels:
    # WECHAT = "wechat"
    # DINGTALK = "dingtalk"
    # SLACK = "slack"
    # TELEGRAM = "telegram"
    # DISCORD = "discord"


# 渠道能力枚举：描述某个渠道类型支持哪些交互能力，
# 由各渠道实现的 get_capabilities() 返回，并汇总展示在 ChannelMetadata.capabilities 中
class ChannelCapability(str, Enum):
    """Channel capabilities."""

    WEBSOCKET = "websocket"  # Supports WebSocket long connection
    WEBHOOK = "webhook"  # Supports webhook callbacks
    SEND_MESSAGE = "send_message"  # Can send messages
    SEND_IMAGE = "send_image"  # Can send images
    SEND_FILE = "send_file"  # Can send files
    REACTIONS = "reactions"  # Supports message reactions
    GROUP_CHAT = "group_chat"  # Supports group chats
    DIRECT_MESSAGE = "direct_message"  # Supports direct messages


# 群聊消息响应策略：控制机器人在群聊场景下是否需要被 @ 才响应，
# 通常作为某个渠道实例配置（config）中的一个字段使用
class GroupPolicy(str, Enum):
    """Group message handling policy."""

    OPEN = "open"  # Respond to all group messages
    MENTION = "mention"  # Respond only when mentioned


# ============================================
# Channel Configuration Base
# ============================================


# 渠道配置模型的抽象基类：每一种渠道类型（如飞书）都应该实现自己的
# 具体配置子类，用于声明该渠道特有的配置字段与静态能力信息。
# 注意：目前代码中暂未发现直接以类继承方式声明的具体子类，
# 各渠道更多通过独立的 Pydantic 模型 + get_capabilities()/get_schema_name()
# 的实现来对接（参见 src/infra/channel/feishu/channel.py）。
class ChannelConfigBase(BaseModel, ABC):
    """Base class for channel configurations.

    Each channel type should implement its own config model.
    """

    # 该配置对应的渠道类型，子类必须覆盖此类变量（非实例字段）
    channel_type: ClassVar[ChannelType]
    # 该渠道实例是否启用
    enabled: bool = Field(default=True, description="Whether the channel is enabled")

    # 子类需返回用于前端展示/校验的 schema 名称标识
    @classmethod
    @abstractmethod
    def get_schema_name(cls) -> str:
        """Get the schema name for this channel type."""
        pass

    # 子类需返回该渠道类型实际支持的能力列表，用于填充 ChannelMetadata.capabilities
    @classmethod
    @abstractmethod
    def get_capabilities(cls) -> list[ChannelCapability]:
        """Get the capabilities of this channel type."""
        pass


# ============================================
# Channel Configuration - Database Models
# ============================================


class ChannelConfigCreate(BaseModel):
    """Schema for creating a channel configuration.

    This is a generic wrapper that accepts different channel configs.
    """

    # 要创建的渠道类型（如 feishu）
    channel_type: ChannelType
    # 用户为该渠道实例自定义的名称，用于在多个同类型渠道实例间区分
    name: str = Field(description="User-defined name for this channel instance")
    # 具体渠道类型的专属配置（结构随 channel_type 不同而不同，
    # 通常对应该渠道 ChannelMetadata.config_schema 描述的 JSON Schema）
    config: dict[str, Any]  # Channel-specific config as dict
    # 该渠道实例收到消息后使用的 Agent 配置 ID；不设置则使用系统默认 Agent
    agent_id: Optional[str] = Field(None, description="Agent ID to use for this channel instance")
    # 该渠道实例使用的模型 ID；不设置则使用系统默认模型
    model_id: Optional[str] = Field(
        None, description="Model config ID to use for this channel instance"
    )
    # 该渠道实例产生的会话归属的项目 ID
    project_id: Optional[str] = Field(None, description="Project ID to assign sessions to")
    # 若该渠道由团队 Agent 处理消息，指定使用的团队 ID
    team_id: Optional[str] = Field(None, description="Team ID to use for team agent channel runs")
    # 该渠道实例使用的人设预设 ID，决定该渠道机器人的系统提示词/人格
    persona_preset_id: Optional[str] = Field(
        None, description="Persona preset ID to use for this channel instance"
    )


class ChannelConfigUpdate(BaseModel):
    """Schema for updating a channel configuration."""

    # extra="forbid"：传入未声明字段会直接报错，防止拼写错误的字段被静默忽略
    model_config = ConfigDict(extra="forbid")

    # 更新后的渠道专属配置；此处未设为 Optional，意味着更新配置时需要传入完整配置
    config: dict[str, Any]
    # 是否启用该渠道实例
    enabled: Optional[bool] = None
    # 更换该渠道实例使用的 Agent 配置
    agent_id: Optional[str] = Field(None, description="Agent ID to use for this channel instance")
    # 更换该渠道实例使用的模型
    model_id: Optional[str] = Field(
        None, description="Model config ID to use for this channel instance"
    )
    # 更换该渠道实例会话归属的项目
    project_id: Optional[str] = Field(None, description="Project ID to assign sessions to")
    # 更换团队 Agent 处理时使用的团队 ID
    team_id: Optional[str] = Field(None, description="Team ID to use for team agent channel runs")
    # 更换该渠道实例使用的人设预设
    persona_preset_id: Optional[str] = Field(
        None, description="Persona preset ID to use for this channel instance"
    )


# 渠道配置的对外响应视图：屏蔽/脱敏敏感字段（如具体密钥等），
# 是 GET/列表接口返回给前端的渠道实例结构
class ChannelConfigResponse(BaseModel):
    """Channel configuration response (sensitive fields masked)."""

    # 渠道实例唯一 ID；通过 alias="instance_id" 与内部存储字段名对应
    id: str = Field(alias="instance_id", description="Unique instance identifier")
    # 渠道类型
    channel_type: ChannelType
    # 用户为该渠道实例自定义的名称
    name: str = Field(description="User-defined name for this channel instance")
    # 该渠道实例所属用户 ID
    user_id: str
    # 是否启用
    enabled: bool
    config: dict[str, Any]  # Masked config for display
    # 该渠道类型实际支持的能力列表
    capabilities: list[ChannelCapability]
    # 该渠道实例使用的 Agent 配置 ID
    agent_id: Optional[str] = Field(None, description="Agent ID used by this channel instance")
    # 该渠道实例使用的模型 ID
    model_id: Optional[str] = Field(
        None, description="Model config ID used by this channel instance"
    )
    # 该渠道实例会话归属的项目 ID
    project_id: Optional[str] = Field(
        None, description="Project ID assigned to this channel's sessions"
    )
    # 团队 Agent 处理时使用的团队 ID
    team_id: Optional[str] = Field(None, description="Team ID used by team agent channel runs")
    # 该渠道实例使用的人设预设 ID
    persona_preset_id: Optional[str] = Field(
        None, description="Persona preset ID used by this channel instance"
    )
    # 创建时间
    created_at: Optional[datetime] = None
    # 最近更新时间
    updated_at: Optional[datetime] = None

    # 允许同时按字段名（id）或别名（instance_id）填充，兼容内部数据的字段命名
    model_config = ConfigDict(populate_by_name=True)


# 某个渠道实例的实时连接状态，用于渠道管理页面展示健康状况
class ChannelConfigStatus(BaseModel):
    """Channel connection status."""

    # 渠道类型
    channel_type: ChannelType
    # 是否启用
    enabled: bool
    # 当前是否已成功建立连接（如飞书 WebSocket 长连接是否在线）
    connected: bool = False
    # 连接异常时的错误信息
    error_message: Optional[str] = None
    # 最近一次成功建立连接的时间
    last_connected_at: Optional[datetime] = None


# ============================================
# Channel Registry Entry
# ============================================


# 渠道类型的静态元信息：描述某一种渠道类型（而非某个具体实例）
# 本身的展示信息与配置结构，用于渠道类型选择/新建渠道时的前端渲染
class ChannelMetadata(BaseModel):
    """Metadata for a channel type."""

    # 渠道类型
    channel_type: ChannelType
    # 展示名称（如"飞书"）
    display_name: str
    # 渠道类型说明文字
    description: str
    icon: str  # Lucide icon name
    # 该渠道类型支持的能力列表
    capabilities: list[ChannelCapability]
    config_schema: dict[str, Any]  # JSON Schema for config
    # 该渠道类型是否需要配置 Webhook 回调地址
    requires_webhook: bool = False
    # 该渠道类型是否需要建立 WebSocket 长连接
    requires_websocket: bool = False
    # 接入该渠道的分步引导说明（展示给用户的配置向导文案）
    setup_guide: list[str] = Field(default_factory=list)
    # 前端渲染配置表单所需的字段描述列表
    config_fields: list[dict[str, Any]] = Field(default_factory=list)


# ============================================
# Channel List Response
# ============================================


# 查询"当前用户已配置的渠道实例列表"的响应体
class ChannelListResponse(BaseModel):
    """List of available channels with their configurations."""

    # 渠道实例列表
    channels: list[ChannelConfigResponse]


# 查询"系统支持哪些渠道类型"的响应体，用于新建渠道时的类型选择器
class ChannelTypeListResponse(BaseModel):
    """List of available channel types with metadata."""

    # 支持的渠道类型元信息列表
    types: list[ChannelMetadata]

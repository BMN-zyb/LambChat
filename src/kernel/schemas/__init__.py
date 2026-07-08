"""
Pydantic 模型导出

包含所有数据传输对象 (DTO)。
"""

# 本文件是 src.kernel.schemas 包的统一导出入口（barrel module）。
# 上层代码（API 路由层 src/api、基础设施/领域服务层 src/infra 等）
# 通常直接 `from src.kernel.schemas import XxxModel` 获取数据模型，
# 不需要关心具体定义在哪个子模块文件中。
# 注意：并非所有 schema 子模块都会在此汇总导出——例如
# channel.py / model.py / team.py / scheduled_task.py 等模块的调用方
# 需要直接从对应子模块导入（如 `from src.kernel.schemas.model import ModelConfig`）。
# Agent 对话相关的请求/响应/流式事件等模型（定义于 schemas/agent.py）。
from src.kernel.schemas.agent import (
    AgentRequest,
    AgentResponse,
    AgentStep,
    HealthResponse,
    StreamEvent,
    ToolsListResponse,
)
# MCP（Model Context Protocol）服务器配置相关模型：MCP 服务器的增删改查、
# 导入导出、工具开关状态等（定义于 schemas/mcp.py）。
from src.kernel.schemas.mcp import (
    MCPExportResponse,
    MCPImportRequest,
    MCPImportResponse,
    MCPServerBase,
    MCPServerCreate,
    MCPServerResponse,
    MCPServersResponse,
    MCPServerToggleResponse,
    MCPServerUpdate,
    MCPTransport,
    SystemMCPServer,
    UserMCPServer,
)
# 对话消息相关模型：消息类型枚举、工具调用与工具结果等
# （定义于 schemas/message.py）。
from src.kernel.schemas.message import (
    Message,
    MessageType,
    ToolCall,
    ToolResult,
)
# 权限元数据相关模型：权限分组、权限项信息，以及构建权限响应
# 的辅助函数（定义于 schemas/permission.py）。
from src.kernel.schemas.permission import (
    PermissionGroup,
    PermissionInfo,
    PermissionsResponse,
    get_permissions_response,
)
# 人设预设相关模型：预设的创建/更新请求、运行时快照
# 及开场白建议（定义于 schemas/persona_preset.py）。
from src.kernel.schemas.persona_preset import (
    PersonaPreset,
    PersonaPresetCreate,
    PersonaPresetSnapshot,
    PersonaPresetUpdate,
    PersonaStarterPrompt,
)
# 角色（Role，权限角色而非 Agent 人设）相关模型：角色的创建与更新
# （定义于 schemas/role.py）。
from src.kernel.schemas.role import (
    Role,
    RoleCreate,
    RoleUpdate,
)
# 会话（Session，即一次对话）相关模型：会话的创建与更新
# （定义于 schemas/session.py）。
from src.kernel.schemas.session import (
    Session,
    SessionCreate,
    SessionUpdate,
)
# 系统设置相关模型：设置项类型/分类枚举、设置更新请求
# 与重置响应（定义于 schemas/setting.py）。
from src.kernel.schemas.setting import (
    SettingCategory,
    SettingItem,
    SettingResetResponse,
    SettingsResponse,
    SettingType,
    SettingUpdate,
)
# 用户相关模型：用户的增删改查视图、数据库内部视图（含密码哈希）
# 及 JWT Token payload（定义于 schemas/user.py）。
from src.kernel.schemas.user import (
    TokenPayload,
    User,
    UserCreate,
    UserInDB,
    UserUpdate,
)

# 显式声明本包对外导出的公共符号列表，便于静态检查工具识别导出边界，
# 并支持 `from src.kernel.schemas import *` 的使用方式。
# 下方按来源子模块分组，每组前的注释标明来源（分组注释为原有代码）。
__all__ = [
    # Message
    "Message",
    "MessageType",
    "ToolCall",
    "ToolResult",
    # Session
    "Session",
    "SessionCreate",
    "SessionUpdate",
    # User
    "User",
    "UserCreate",
    "UserUpdate",
    "UserInDB",
    "TokenPayload",
    # Role
    "Role",
    "RoleCreate",
    "RoleUpdate",
    # Setting
    "SettingType",
    "SettingCategory",
    "SettingItem",
    "SettingUpdate",
    "SettingsResponse",
    "SettingResetResponse",
    # MCP
    "MCPTransport",
    "MCPServerBase",
    "MCPServerCreate",
    "MCPServerUpdate",
    "SystemMCPServer",
    "UserMCPServer",
    "MCPServerResponse",
    "MCPServersResponse",
    "MCPServerToggleResponse",
    "MCPImportRequest",
    "MCPImportResponse",
    "MCPExportResponse",
    # Permission
    "PermissionGroup",
    "PermissionInfo",
    "PermissionsResponse",
    "get_permissions_response",
    # Persona Preset
    "PersonaPreset",
    "PersonaPresetCreate",
    "PersonaPresetUpdate",
    "PersonaPresetSnapshot",
    "PersonaStarterPrompt",
    # Agent
    "AgentRequest",
    "AgentResponse",
    "AgentStep",
    "StreamEvent",
    "HealthResponse",
    "ToolsListResponse",
]

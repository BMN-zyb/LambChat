"""
MCP (Model Context Protocol) schemas for API request/response
"""
# 本模块定义与 MCP（Model Context Protocol，模型上下文协议）服务集成相关的数据模型，主要包括：
# 1. MCP 服务器配置本身：区分"系统级"（管理员配置、全员可见/受角色配额约束）与
#    "用户级"（用户自己配置、仅自己可用）两种归属，共用 MCPServerBase 描述连接方式；
# 2. 面向工具粒度的访问控制与配额策略（MCPToolPolicy/MCPRoleQuota），支持按角色限流；
# 3. 导入/导出、启停切换、用户与系统间迁移等管理操作的请求/响应模型；
# 4. 工具发现（对某个 MCP 服务器探测其提供了哪些工具）相关模型。
# 主要被 MCP 管理相关的 FastAPI 路由、以及智能体运行时加载/过滤 MCP 工具的逻辑所使用。

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# MCP 服务器的连接/传输方式枚举
class MCPTransport(str, Enum):
    """MCP transport type"""

    # Server-Sent Events：基于 HTTP 长连接的流式传输
    SSE = "sse"
    # 可流式的 HTTP 传输（MCP 较新的标准传输方式）
    STREAMABLE_HTTP = "streamable_http"
    # 沙箱内以子进程/stdio 方式启动并通信的传输（运行在隔离的沙箱环境中）
    SANDBOX = "sandbox"


# MCP 服务器配置的公共基类，被系统级/用户级/响应等多种具体模型继承，
# 字段按用途分为通用字段、HTTP 类传输专用字段、沙箱类传输专用字段三组
class MCPServerBase(BaseModel):
    """Base MCP server configuration"""

    # 服务器名称，作为该 MCP 服务的唯一标识
    name: str = Field(..., description="Server name (unique identifier)")
    # 采用的传输方式（sse/streamable_http/sandbox）
    transport: MCPTransport = Field(..., description="Transport type")
    # 该服务器是否启用；禁用后其工具不会暴露给智能体
    enabled: bool = Field(True, description="Whether server is enabled")

    # 以下两个字段仅在 transport 为 http 类（SSE/STREAMABLE_HTTP）时使用
    # http configuration
    # HTTP 类传输的服务地址
    url: Optional[str] = Field(None, description="URL for http transport")
    # 请求 MCP 服务时附带的 HTTP 头（如鉴权 Token）
    headers: Optional[dict[str, str]] = Field(None, description="HTTP headers")

    # 以下两个字段仅在 transport 为 sandbox 时使用
    # sandbox configuration
    # 沙箱内启动 MCP 服务所执行的 stdio 命令
    command: Optional[str] = Field(None, description="stdio command for sandbox transport")
    # 需要注入到沙箱环境中的环境变量键名列表（值从用户/系统环境变量存储中取）
    env_keys: Optional[list[str]] = Field(
        None, description="Environment variable keys to inject into sandbox MCP"
    )


# 某个角色针对一个系统级 MCP 服务器（或某个工具）的用量配额限制
class MCPRoleQuota(BaseModel):
    """Per-role MCP usage quota for a system server."""

    # 该角色下每个用户每天可调用工具的次数上限；None 表示不限制
    daily_limit: Optional[int] = Field(
        None,
        ge=0,
        description="Daily tool-call limit per user for this role. None = unlimited.",
    )
    # 该角色下每个用户每周可调用工具的次数上限；None 表示不限制
    weekly_limit: Optional[int] = Field(
        None,
        ge=0,
        description="Weekly tool-call limit per user for this role. None = unlimited.",
    )


# 单个工具级别的访问与用量策略，持久化存储，用于比"服务器级"更细粒度地控制某个具体工具
class MCPToolPolicy(BaseModel):
    """Per-tool MCP access and usage policy."""

    # 该工具所属的 MCP 服务器名称
    server_name: Optional[str] = Field(None, description="Owning MCP server name")
    # 工具名称（不含服务器前缀）
    tool_name: Optional[str] = Field(None, description="Tool name without server prefix")
    # 是否在全局范围内禁用该工具
    disabled: bool = Field(False, description="Whether this tool is disabled globally")
    # 是否将该工具直接暴露给模型（而不是延迟到通过 search_tools 搜索后才可见），
    # 用于让高频/重要工具跳过"按需检索工具"的机制
    inline_exposure: bool = Field(
        False,
        description=(
            "Whether this tool should be exposed directly to the model instead of deferred "
            "behind search_tools."
        ),
    )
    # 允许使用该工具的角色列表；空列表表示不限制角色，所有角色均可用
    allowed_roles: list[str] = Field(
        default_factory=list,
        description="Roles allowed to use this tool. Empty list = all roles.",
    )
    # 按角色配置的该工具用量配额
    role_quotas: dict[str, MCPRoleQuota] = Field(
        default_factory=dict,
        description="Per-role usage quotas for this tool.",
    )
    # 该策略的创建时间
    created_at: Optional[str] = Field(None, description="Creation timestamp")
    # 该策略的最后更新时间
    updated_at: Optional[str] = Field(None, description="Last update timestamp")
    # 最后一次更新该策略的管理员用户 ID
    updated_by: Optional[str] = Field(None, description="Admin user ID who last updated")


# 更新单个工具策略的请求体；所有字段均可选，未提供的字段保持原值不变（PATCH 语义）
class MCPToolPolicyUpdate(BaseModel):
    """Request to update one tool's access and quota policy."""

    # 新的禁用状态
    disabled: Optional[bool] = None
    # 新的"直接暴露"设置
    inline_exposure: Optional[bool] = None
    # 新的允许角色列表
    allowed_roles: Optional[list[str]] = None
    # 新的按角色配额设置
    role_quotas: Optional[dict[str, MCPRoleQuota]] = None


# 创建新 MCP 服务器的请求体；在基础连接配置之上，额外携带该服务器的角色可见性/配额设置
class MCPServerCreate(MCPServerBase):
    """Schema for creating a new MCP server"""

    # 允许看到并使用该服务器的角色列表；空列表表示不限制，所有角色均可用
    allowed_roles: list[str] = Field(
        default_factory=list,
        description="Roles allowed to see and use this server. Empty list = all roles.",
    )
    # 按角色配置的该服务器（整体）用量配额
    role_quotas: dict[str, MCPRoleQuota] = Field(
        default_factory=dict,
        description="Per-role usage quotas for this system server.",
    )


# 更新已有 MCP 服务器的请求体；所有字段均可选，未提供的字段保持原值不变（PATCH 语义）
class MCPServerUpdate(BaseModel):
    """Schema for updating an MCP server"""

    # 新的传输方式
    transport: Optional[MCPTransport] = None
    # 新的启用状态
    enabled: Optional[bool] = None
    # 新的 HTTP 类传输地址
    url: Optional[str] = None
    # 新的 HTTP 头
    headers: Optional[dict[str, str]] = None
    # 新的沙箱启动命令
    command: Optional[str] = None
    # 新的需注入环境变量键名列表
    env_keys: Optional[list[str]] = None
    # 新的允许角色列表
    allowed_roles: Optional[list[str]] = None
    # 新的按角色配额设置
    role_quotas: Optional[dict[str, MCPRoleQuota]] = None


# 系统级 MCP 服务器的持久化配置（由管理员统一配置，可被多个/所有用户共享使用）
class SystemMCPServer(MCPServerBase):
    """System-level MCP server configuration (admin managed)"""

    # 系统级服务器该字段恒为 True，用于和用户级服务器区分
    is_system: bool = Field(True, description="Always True for system servers")
    # 是否为"虚拟内部服务器"（非真实外部 MCP 连接，可能是内置功能包装出来的虚拟服务）
    is_internal: bool = Field(False, description="Whether this is a virtual internal server")
    # 在系统级别被禁用的工具名称列表（禁用后对所有用户都不可见）
    disabled_tools: list[str] = Field(
        default_factory=list, description="List of tool names disabled at system level"
    )
    # 允许看到并使用该服务器的角色列表；空列表表示所有角色均可用
    allowed_roles: list[str] = Field(
        default_factory=list,
        description="Roles allowed to see and use this server. Empty list = all roles.",
    )
    # 按角色配置的该服务器用量配额
    role_quotas: dict[str, MCPRoleQuota] = Field(
        default_factory=dict,
        description="Per-role usage quotas for this system server.",
    )
    # 创建时间
    created_at: Optional[str] = Field(None, description="Creation timestamp")
    # 最后更新时间
    updated_at: Optional[str] = Field(None, description="Last update timestamp")
    # 最后一次更新该配置的管理员用户 ID
    updated_by: Optional[str] = Field(None, description="Admin user ID who last updated")
    # 创建该配置的管理员用户 ID
    created_by: Optional[str] = Field(None, description="Admin user ID who created the server")


# 用户级 MCP 服务器的持久化配置（由用户自行配置，仅该用户本人可见可用）
class UserMCPServer(MCPServerBase):
    """User-level MCP server configuration"""

    # 该服务器的所有者用户 ID
    user_id: str = Field(..., description="Owner user ID")
    # 用户级服务器该字段恒为 False
    is_system: bool = Field(False, description="Always False for user servers")
    # 该用户在此服务器上禁用的工具名称列表
    disabled_tools: list[str] = Field(
        default_factory=list, description="List of tool names disabled on this server"
    )
    # 创建时间
    created_at: Optional[str] = Field(None, description="Creation timestamp")
    # 最后更新时间
    updated_at: Optional[str] = Field(None, description="Last update timestamp")


# 统一对外返回的 MCP 服务器信息（无论其来源是系统级还是用户级，都会被规整为此形状再返回给前端）
class MCPServerResponse(MCPServerBase):
    """MCP server response with additional metadata"""

    # 是否为系统级服务器
    is_system: bool = Field(..., description="Whether this is a system server")
    # 是否为虚拟内部服务器
    is_internal: bool = Field(False, description="Whether this is a virtual internal server")
    # 当前请求用户是否有权编辑该服务器（由后端结合权限/归属关系计算得出，非持久化字段）
    can_edit: bool = Field(..., description="Whether current user can edit this server")
    # 允许看到并使用该服务器的角色列表
    allowed_roles: list[str] = Field(
        default_factory=list,
        description="Roles allowed to see and use this server. Empty list = all roles.",
    )
    # 按角色配置的该服务器用量配额
    role_quotas: dict[str, MCPRoleQuota] = Field(
        default_factory=dict,
        description="Per-role usage quotas for this system server.",
    )
    # 创建时间
    created_at: Optional[str] = Field(None, description="Creation timestamp")
    # 最后更新时间
    updated_at: Optional[str] = Field(None, description="Last update timestamp")


# MCP 服务器列表查询接口的分页响应体
class MCPServersResponse(BaseModel):
    """Response containing list of MCP servers"""

    # 当前页的服务器列表
    servers: list[MCPServerResponse] = Field(default_factory=list)
    # 满足条件的服务器总数
    total: int = 0
    # 本次查询跳过的条数（分页偏移）
    skip: int = 0
    # 本次查询返回的最大条数
    limit: int = 100


# 切换服务器启用/禁用状态后的响应体
class MCPServerToggleResponse(BaseModel):
    """Response after toggling server enabled status"""

    # 切换后的最新服务器信息
    server: MCPServerResponse
    # 操作结果提示信息
    message: str


# 批量导入 MCP 服务器配置的请求体；兼容两种 JSON 结构：
# 项目原生格式（servers 字段）与 Claude Desktop/MCP Studio 常见格式（mcp_servers 字段）
class MCPImportRequest(BaseModel):
    """Request to import MCP servers from JSON (supports both native and studio format)"""

    # 原生格式的服务器配置：{server_name: 配置字典}
    servers: Optional[dict[str, dict[str, Any]]] = Field(
        None, description="MCP servers config (native format)"
    )
    # Studio/Claude Desktop 格式的服务器配置：{server_name: 配置字典}
    mcp_servers: Optional[dict[str, dict[str, Any]]] = Field(
        None, description="MCP servers config (studio/Claude Desktop format)"
    )
    # 遇到同名服务器时是否覆盖已存在的配置
    overwrite: bool = Field(False, description="Overwrite existing servers with same name")

    # 兼容两种输入格式：优先取 mcp_servers，取不到再取 servers，都没有则返回空字典
    def get_servers(self) -> dict[str, dict[str, Any]]:
        """Return servers from whichever key was provided, preferring mcp_servers"""
        return self.mcp_servers or self.servers or {}


# 批量导入操作完成后的汇总响应
class MCPImportResponse(BaseModel):
    """Response after importing MCP servers"""

    # 操作结果提示信息
    message: str
    # 成功导入的服务器数量
    imported_count: int
    # 因已存在/校验失败等原因被跳过的数量
    skipped_count: int
    # 导入过程中产生的错误信息列表
    errors: list[str] = Field(default_factory=list)


# 导出当前 MCP 配置的响应体，供备份或迁移到其它环境后重新导入
class MCPExportResponse(BaseModel):
    """Response for exporting MCP configuration"""

    # 导出的服务器配置：{server_name: 配置字典}
    servers: dict[str, dict[str, Any]] = Field(default_factory=dict)


# 在"用户级"与"系统级"之间迁移某个 MCP 服务器归属的请求体
# （用户级→系统级为"提升"，系统级→用户级为"降级"）
class MCPServerMoveRequest(BaseModel):
    """Request to move a server between user and system"""

    # 降级（系统级→用户级）时，需要指定该服务器新归属的目标用户 ID；提升场景通常无需此字段
    target_user_id: Optional[str] = Field(
        None, description="Target user ID when demoting system server to user server"
    )


# 迁移操作完成后的响应体
class MCPServerMoveResponse(BaseModel):
    """Response after moving a server"""

    # 迁移后的服务器最新信息
    server: MCPServerResponse
    # 操作结果提示信息
    message: str
    # 迁移前的服务器类型（user/system）
    from_type: str = Field(..., description="Original server type (user/system)")
    # 迁移后的服务器类型（user/system）
    to_type: str = Field(..., description="New server type (user/system)")


# ============================================
# MCP Tool Discovery & Toggle Schemas
# ============================================
# 本节用于"连接到某个 MCP 服务器后探测其提供的工具列表"，以及对单个工具做启停切换。


# 从某个 MCP 服务器探测（发现）到的单个工具的信息，融合了工具本身的元数据与
# 系统在其之上叠加的访问控制状态（是否禁用/允许的角色/配额等）
class MCPToolInfo(BaseModel):
    """Information about a tool discovered from an MCP server"""

    # 工具名称
    name: str = Field(..., description="Tool name")
    # 工具描述
    description: str = Field(default="", description="Tool description")
    # 工具参数的原始 JSON Schema 列表（未做进一步结构化解析）
    parameters: list[dict[str, Any]] = Field(default_factory=list, description="Tool parameters")
    # 是否在系统级别被禁用
    system_disabled: bool = Field(
        default=False, description="Whether this tool is disabled at system level"
    )
    # 是否被当前用户个人禁用
    user_disabled: bool = Field(
        default=False, description="Whether this tool is disabled by the current user"
    )
    # 允许使用该工具的角色列表；空列表表示所有角色均可用
    allowed_roles: list[str] = Field(
        default_factory=list,
        description="Roles allowed to use this tool. Empty list = all roles.",
    )
    # 按角色配置的该工具用量配额
    role_quotas: dict[str, MCPRoleQuota] = Field(
        default_factory=dict,
        description="Per-role usage quotas for this tool.",
    )
    # 该工具是否已存在显式的工具级策略（MCPToolPolicy）记录，
    # 为 False 时表示当前展示的仅是继承自服务器级别的默认状态
    policy_configured: bool = Field(
        default=False,
        description="Whether this tool has an explicit tool-level policy.",
    )
    # 该工具是否直接暴露给模型，而非通过延迟检索（search_tools）机制按需展示
    inline_exposure: bool = Field(
        default=False,
        description="Whether this tool is exposed directly instead of through deferred search.",
    )


# 对某个 MCP 服务器执行一次"工具发现"操作后的响应体
class MCPToolDiscoveryResponse(BaseModel):
    """Response for tool discovery from an MCP server"""

    # 被探测的 MCP 服务器名称
    server_name: str = Field(..., description="MCP server name")
    # 探测到的工具列表
    tools: list[MCPToolInfo] = Field(default_factory=list, description="Discovered tools")
    # 探测到的工具数量
    count: int = Field(0, description="Number of discovered tools")
    # 若探测失败（如连接不上该服务器），记录错误信息；成功时为 None
    error: Optional[str] = Field(None, description="Error message if discovery failed")


# 切换某个工具启用状态的请求体
class MCPToolToggleRequest(BaseModel):
    """Request to toggle a specific tool's enabled status"""

    # 新的启用状态
    enabled: bool = Field(..., description="Whether the tool is enabled")
    # 切换的作用级别："system" 表示服务器级/全局生效（影响所有用户），
    # "user" 表示仅作为当前用户的个人偏好设置
    level: str = Field(
        "system",
        description="Toggle level: 'system' for server-level (affects all users), 'user' for per-user preference",
    )


# 切换工具启用状态操作完成后的响应体
class MCPToolToggleResponse(BaseModel):
    """Response after toggling a tool's enabled status"""

    # 工具所属的 MCP 服务器名称
    server_name: str = Field(..., description="MCP server name")
    # 工具名称
    tool_name: str = Field(..., description="Tool name")
    # 切换后的最新启用状态
    enabled: bool = Field(..., description="New enabled status")
    # 操作结果提示信息
    message: str

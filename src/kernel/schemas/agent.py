"""Agent-related schemas."""
# 本模块集中定义了 Agent（智能体）相关的 Pydantic 模型，大致分为几类：
# 1. 运行时请求/响应模型：AgentRequest（触发一次智能体运行的请求体）、
#    AgentStep/AgentResponse（非流式运行的步骤记录与最终响应）、StreamEvent（流式运行的事件）；
# 2. 辅助/周边模型：AttachmentSchema（附件）、HealthResponse/MemoryHealthSummary（健康检查与内存诊断）、
#    ToolInfo/ToolParamInfo/ToolsListResponse（可用工具清单）、ReleaseAsset/VersionResponse（版本与更新检查）；
# 3. 管理侧配置模型：AgentConfig 系列（全局智能体启用/展示配置）、
#    RoleAgentAssignment 系列（角色可访问的智能体）、RoleModelAssignment 系列（角色可访问的模型）、
#    UserAgentPreference 系列（用户默认智能体偏好）。
# 主要被 FastAPI 路由层（对话/智能体运行接口、管理后台接口）以及智能体执行引擎所引用。

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

# GoalSpec：带评分标准（rubric）的目标规格，用于"目标导向执行"模式
from src.infra.goal import GoalSpec
# utc_now：统一的 UTC 当前时间工具函数，用作时间字段的默认值工厂
from src.infra.utils.datetime import utc_now
# ToolCall：单次工具调用的结构（工具名+参数），复用消息模块中的定义
from src.kernel.schemas.message import ToolCall
# PersonaPresetSnapshot：会话创建时固化下来的角色预设快照，保证后续预设变更不影响历史会话
from src.kernel.schemas.persona_preset import PersonaPresetSnapshot


# 表示一个已上传文件的附件元信息，用于挂载在 AgentRequest.attachments 上随消息一起发送给智能体
class AttachmentSchema(BaseModel):
    """Attachment schema for file uploads."""

    # 允许既可用字段名 mime_type，也可用别名 mimeType 来构造/赋值该模型（兼容前端camelCase传参）
    model_config = ConfigDict(populate_by_name=True)

    # 附件的唯一标识 ID
    id: str = Field(..., description="Unique attachment ID")
    # 附件在存储系统（如对象存储）中的 key，用于定位实际文件
    key: str = Field(..., description="Storage key")
    # 用户上传时的原始文件名
    name: str = Field(..., description="Original filename")
    # 文件大类：image / video / audio / document
    type: str = Field(..., description="File category: image, video, audio, document")
    # 文件的 MIME 类型；对外别名为 mimeType（前端习惯用小驼峰命名）
    mime_type: str = Field(..., description="MIME type", alias="mimeType")
    # 文件大小，单位字节
    size: int = Field(..., description="File size in bytes")
    # 可直接访问该文件的 URL
    url: str = Field(..., description="Accessible URL")


# 触发一次智能体运行的请求体（对话/运行接口的核心入参），涵盖用户输入、会话归属、
# 工具/技能开关、角色预设、附件、目标等运行时可配置项
class AgentRequest(BaseModel):
    """Request to run the agent."""

    # 用户本轮发送的消息内容或任务描述
    message: str = Field(..., description="User message or task description")
    # 会话 ID；传入已存在的会话可实现多轮对话上下文延续，不传则视为新会话
    session_id: Optional[str] = Field(None, description="Session ID for conversation continuity")
    # 智能体执行文件操作时使用的工作目录，默认使用相对路径 ./workspace
    workspace_dir: str = Field("./workspace", description="Working directory for file operations")
    # 智能体单次运行允许的最大步数上限，防止死循环/失控消耗
    max_steps: int = Field(50, description="Maximum number of agent steps")
    # 本次运行要禁用的工具名称列表；默认 None 表示不额外禁用任何工具
    disabled_tools: Optional[list[str]] = Field(
        None, description="Tools to disable (default: none)"
    )
    # 智能体的额外可选项（如是否开启思维链 enable_thinking），以自由字典形式传递
    agent_options: Optional[dict[str, Any]] = Field(
        None, description="Agent options (e.g., enable_thinking)"
    )
    # 本次对话要禁用的技能（Skill）名称列表
    disabled_skills: Optional[list[str]] = Field(
        None, description="Skills to disable for this conversation"
    )
    # 本次对话要显式启用的技能名称列表（用于覆盖默认不启用的技能）
    enabled_skills: Optional[list[str]] = Field(
        None, description="Skills to explicitly enable for this conversation"
    )
    # 选用的角色预设（Persona Preset）ID
    persona_preset_id: Optional[str] = Field(None, description="Persona preset ID")
    # 已解析好的角色预设不可变快照，通常由后端根据 persona_preset_id 解析后回填，
    # 用于固化到会话中，避免预设后续变更影响历史会话行为
    persona_snapshot: Optional[PersonaPresetSnapshot] = Field(
        None, description="Resolved persona preset snapshot"
    )
    # 已解析好的角色系统提示词文本，用于在运行时注入到系统 Prompt 中
    persona_system_prompt: Optional[str] = Field(
        None, description="Resolved persona system prompt for runtime injection"
    )
    # 本次对话要禁用的 MCP 工具列表（工具名一般带有服务前缀）
    disabled_mcp_tools: Optional[list[str]] = Field(
        None, description="MCP tools to disable for this conversation"
    )
    # 用户所在的 IANA 时区（如 Asia/Shanghai），用于聊天消息时间戳的本地化展示
    user_timezone: Optional[str] = Field(
        None, description="User IANA timezone for timestamping chat messages"
    )
    # 随本次消息一起提交的文件附件列表
    attachments: Optional[list[AttachmentSchema]] = Field(None, description="File attachments")
    # 额外的自由格式上下文信息，供上层调用方传递非结构化的附加数据
    context: dict[str, Any] = Field(default_factory=dict, description="Additional context")
    # 若本次请求会创建新会话，指定要归属的项目 ID
    project_id: Optional[str] = Field(None, description="Project ID to assign to new session")
    # 团队智能体模式下使用的团队 ID
    team_id: Optional[str] = Field(None, description="Team ID for team agent mode")
    # 本次运行要追求的目标（含评分标准 rubric），用于"目标导向执行"场景下的迭代评估
    goal: Optional[GoalSpec] = Field(None, description="Active goal for rubric-guided execution")
    # 自动模式：开启后跳过向人类提问（ask_human）等交互，让智能体自主完成任务
    auto_mode: bool = Field(False, description="Auto mode: skip ask_human, autonomous execution")


# 非流式执行模式下记录的单个执行步骤，用于回溯智能体的思考与工具调用过程
class AgentStep(BaseModel):
    """Single step in agent execution."""

    # 步骤序号（从几开始、是否连续由调用方约定）
    step: int
    # 该步骤的思考/推理文本（可选，未必每步都有）
    thought: Optional[str] = None
    # 该步骤中发起的工具调用列表
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # 该步骤中工具调用返回的结果列表（原始字典形式）
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    # 该步骤产出的文本回复（如果有）
    response: Optional[str] = None


# 智能体一次完整（非流式）运行结束后的汇总响应
class AgentResponse(BaseModel):
    """Agent execution response."""

    # 本次运行是否成功
    success: bool
    # 最终返回给用户的消息文本
    message: str
    # 本次运行总共执行了多少步
    steps: int
    # 详细的分步执行记录
    logs: list[AgentStep] = Field(default_factory=list)
    # 本次运行所属的会话 ID
    session_id: str
    # 可选的可观测性追踪链接，便于排查问题
    trace_url: Optional[str] = None  # LangSmith trace URL


# 流式运行模式下通过 SSE/WebSocket 逐个推送给前端的事件
class StreamEvent(BaseModel):
    """Streaming event."""

    # 事件类型标识
    event_type: str  # thinking, content, tool_call, tool_result, step, complete, error
    # 事件承载的文本内容（依事件类型而定，如思考片段/正文片段/错误信息）
    content: str
    # 事件的附加元数据（如工具名、步骤号等），自由字典结构
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 事件产生的时间戳，默认取当前 UTC 时间
    timestamp: datetime = Field(default_factory=utc_now)


# 健康检查接口（如 /health）的响应体
class HealthResponse(BaseModel):
    """Health check response."""

    # 健康状态标识，默认 "ok"
    status: str = "ok"
    # 当前应用版本号
    version: str
    # 本次健康检查的时间戳
    timestamp: datetime = Field(default_factory=utc_now)
    # 可选的内存诊断信息；未启用内存监控时为 None
    memory: Optional["MemoryHealthSummary"] = None


# 精简版的进程内存诊断信息，嵌入健康检查响应中，用于监控是否存在内存泄漏
class MemoryHealthSummary(BaseModel):
    """Compact memory diagnostics for health checks."""

    # 本次采样是否可用（如依赖的 psutil 不可用时为 False）
    available: bool = False
    # 不可用/异常时的原因说明
    reason: Optional[str] = None
    # 常驻内存大小（Resident Set Size），单位字节
    rss_bytes: Optional[int] = None
    # 虚拟内存大小（Virtual Memory Size），单位字节
    vms_bytes: Optional[int] = None
    # 当前进程线程数
    thread_count: Optional[int] = None
    # 当前进程打开的文件描述符数量
    open_file_count: Optional[int] = None
    # 内部维护的历史采样点数量
    history_size: Optional[int] = None
    # 相对基线的内存增长量，单位字节
    growth_bytes: int = 0
    # 是否怀疑存在内存泄漏（基于增长趋势判断）
    suspected_leak: bool = False
    # 采样间隔，单位秒
    sample_interval_seconds: Optional[float] = None
    # 上次重置基线的时间
    baseline_reset_at: Optional[datetime] = None
    # 最近一次采样的时间
    last_sample_at: Optional[datetime] = None
    # 最近一次采样出错时的错误信息
    last_error: Optional[str] = None
    # LangGraph checkpointer（会话状态持久化组件）相关的诊断信息
    checkpointer: Optional[dict[str, Any]] = None


# 描述某个工具的一个参数，用于工具清单接口向前端/管理后台展示参数详情
class ToolParamInfo(BaseModel):
    """Information about a tool parameter."""

    # 忽略未定义的多余字段（兼容不同工具来源可能附带的额外元数据）
    model_config = ConfigDict(extra="ignore")

    # 参数名称
    name: str = Field(..., description="Parameter name")
    # 参数类型，默认视为字符串
    type: str = Field(default="string", description="Parameter type")
    # 参数说明文案
    description: str = Field(default="", description="Parameter description")
    # 该参数是否为必填
    required: bool = Field(default=False, description="Whether the parameter is required")
    # 参数默认值（如果有）
    default: Optional[Any] = Field(None, description="Default value if any")


# 描述一个可供智能体调用的工具，用于工具清单接口（如"可用工具"面板）
class ToolInfo(BaseModel):
    """Information about a single tool."""

    # 忽略未定义的多余字段
    model_config = ConfigDict(extra="ignore")

    # 工具名称
    name: str = Field(..., description="Tool name")
    # 工具说明文案
    description: str = Field(default="", description="Tool description")
    # 工具类别：builtin(内置)/skill(技能)/human(人工介入)/mcp(MCP服务提供)
    category: str = Field(..., description="Tool category: builtin, skill, human, mcp")
    # 当工具来自 MCP 时，记录其所属的 MCP 服务名
    server: Optional[str] = Field(None, description="MCP server name for MCP tools")
    # 该工具的参数列表
    parameters: list[ToolParamInfo] = Field(default_factory=list, description="Tool parameters")
    # 该工具是否在系统级被禁用（管理员统一控制，对所有用户生效）
    system_disabled: bool = Field(
        default=False,
        description="Whether this tool is disabled at the system level (admin controlled)",
    )
    # 该工具是否被当前用户个人禁用
    user_disabled: bool = Field(
        default=False,
        description="Whether this tool is disabled by the user",
    )


# 工具清单接口的响应体
class ToolsListResponse(BaseModel):
    """Tools list response."""

    # 工具信息列表
    tools: list[ToolInfo]
    # 工具总数（一般等于 len(tools)，冗余字段便于前端直接展示）
    count: int


# GitHub Release 中的单个资产文件（如移动端安装包），供客户端下载更新时使用
class ReleaseAsset(BaseModel):
    """GitHub release asset for mobile download."""

    # 资产文件名
    name: str = Field(..., description="Asset filename")
    # 资产下载地址
    url: str = Field(..., description="Asset download URL")
    # 文件大小，单位字节
    size: Optional[int] = Field(None, description="File size in bytes")
    # 文件的 MIME 类型，默认按通用二进制流处理
    content_type: str = Field("application/octet-stream", description="MIME type")


# 版本信息与更新检查接口的响应体：既包含当前构建信息，也包含从 GitHub 拉取到的最新版本信息
class VersionResponse(BaseModel):
    """Version information response."""

    # 当前应用版本号
    app_version: str = Field(..., description="Application version")
    # 当前构建对应的 Git tag（如 v1.0.0）
    git_tag: Optional[str] = Field(None, description="Git tag (e.g., v1.0.0)")
    # 当前构建对应的 Git commit 短哈希
    commit_hash: Optional[str] = Field(None, description="Git commit short hash")
    # 构建时间戳
    build_time: Optional[str] = Field(None, description="Build timestamp")
    # 从 GitHub 查询到的最新版本号
    latest_version: Optional[str] = Field(None, description="Latest version from GitHub")
    # 最新版本对应的 GitHub Release 页面地址
    release_url: Optional[str] = Field(None, description="GitHub release URL")
    # GitHub 仓库地址
    github_url: Optional[str] = Field(None, description="GitHub repository URL")
    # 是否存在比当前版本更新的版本（由服务端比较得出）
    has_update: Optional[bool] = Field(None, description="Whether a newer version is available")
    # 最新版本的发布时间
    published_at: Optional[str] = Field(None, description="Latest release publish date")
    # 最新版本的发布说明/更新日志正文
    release_notes: Optional[str] = Field(None, description="Release body/notes")
    # 最新版本关联的可下载资产列表（主要用于移动端）
    release_assets: Optional[list[ReleaseAsset]] = Field(
        None, description="Release assets for mobile"
    )


# ============================================
# Agent Config Schemas
# ============================================
# 本节定义"智能体全局配置/目录"相关的模型：管理员可以在后台控制每个智能体是否全局启用、
# 展示图标、排序及多语言展示文案；AgentConfig 面向内部/历史逻辑，
# AgentCatalogConfig 系列是更完整的、面向管理后台目录页的模型（带默认值、支持批量更新）。


# 单个智能体的全局配置（是否启用、展示信息），通常持久化在配置存储中
class AgentConfig(BaseModel):
    """Agent configuration (global)."""

    # 智能体的唯一标识 ID
    id: str = Field(..., description="Agent ID")
    # 智能体名称
    name: str = Field(..., description="Agent name")
    # 智能体描述
    description: str = Field(..., description="Agent description")
    # 是否在全局范围内启用该智能体（关闭后所有用户都不可见/不可用）
    enabled: bool = Field(True, description="Whether the agent is enabled globally")
    # 展示图标名称或 emoji
    icon: Optional[str] = Field(None, description="Display icon name or emoji")
    # 展示排序值，越小/越大排序规则由前端约定
    sort_order: Optional[int] = Field(None, description="Display sort order")
    # 按语言代码（locale）索引的本地化展示文案
    labels: dict[str, "AgentCatalogLocale"] = Field(
        default_factory=dict,
        description="Localized display labels keyed by locale",
    )


# 某个语言下智能体的本地化展示名称/描述，作为 labels 字典的 value 类型
class AgentCatalogLocale(BaseModel):
    """Localized display metadata for an agent."""

    # 该语言下的展示名称，默认空字符串（表示未配置，回退到默认名称）
    name: str = Field(default="", description="Localized agent display name")
    # 该语言下的展示描述，默认空字符串
    description: str = Field(default="", description="Localized agent description")


# 管理后台维护的智能体目录条目：比 AgentConfig 多了默认值兜底（如默认图标"Bot"、
# 默认排序 100），name/description 允许是 i18n key，配合 labels 做多语言展示
class AgentCatalogConfig(BaseModel):
    """Admin-managed agent catalog entry."""

    # 智能体唯一标识 ID
    id: str = Field(..., description="Agent ID")
    # 兜底名称，或多语言 key（找不到对应 locale 的 labels 时使用）
    name: str = Field(..., description="Fallback agent name or i18n key")
    # 兜底描述，或多语言 key
    description: str = Field(..., description="Fallback agent description or i18n key")
    # 是否全局启用
    enabled: bool = Field(True, description="Whether the agent is enabled globally")
    # 展示图标，默认使用 "Bot" 图标
    icon: str = Field("Bot", description="Display icon name or emoji")
    # 展示排序值，默认 100
    sort_order: int = Field(100, description="Display sort order")
    # 按 locale 索引的多语言展示文案
    labels: dict[str, AgentCatalogLocale] = Field(
        default_factory=dict,
        description="Localized display labels keyed by locale",
    )


# 批量更新管理后台智能体目录的请求体（整体覆盖式更新）
class AgentCatalogConfigUpdate(BaseModel):
    """Update admin-managed agent catalog."""

    # 新的目录条目全量列表
    agents: list[AgentCatalogConfig] = Field(..., description="List of catalog entries")


# 查询管理后台智能体目录的响应体
class AgentCatalogConfigResponse(BaseModel):
    """Response for admin-managed agent catalog."""

    # 全部目录条目（含已禁用的）
    agents: list[AgentCatalogConfig] = Field(..., description="All catalog entries")
    # 当前处于启用状态的智能体 ID 列表（供前端快速判断可用性，无需再逐条过滤 enabled 字段）
    available_agents: list[str] = Field(..., description="List of enabled agent IDs")


# 批量更新全局智能体配置（AgentConfig 版本）的请求体
class AgentConfigUpdate(BaseModel):
    """Update global agent configuration."""

    # 新的智能体配置全量列表
    agents: list[AgentConfig] = Field(..., description="List of agent configurations")


# 查询全局智能体配置的响应体
class GlobalAgentConfigResponse(BaseModel):
    """Response for global agent config."""

    # 所有已注册的智能体及其启用状态
    agents: list[AgentConfig] = Field(
        ..., description="All registered agents with their enabled status"
    )
    # 当前启用的智能体 ID 列表
    available_agents: list[str] = Field(..., description="List of enabled agent IDs")


# ============================================
# Role Agent Schemas
# ============================================
# 本节控制"角色（Role）可以访问哪些智能体"的配置，用于权限体系中按角色限制智能体可见范围。


# 某个角色当前可访问的智能体列表（查询结果）
class RoleAgentAssignment(BaseModel):
    """Role's accessible agents."""

    # 角色 ID
    role_id: str = Field(..., description="Role ID")
    # 角色名称
    role_name: str = Field(..., description="Role name")
    # 该角色被允许使用的智能体 ID 列表
    allowed_agents: list[str] = Field(default_factory=list, description="List of allowed agent IDs")


# 更新某角色可访问智能体列表的请求体（整体覆盖）
class RoleAgentAssignmentUpdate(BaseModel):
    """Update role's accessible agents."""

    # 新的允许智能体 ID 列表
    allowed_agents: list[str] = Field(..., description="List of allowed agent IDs")


# 更新角色可访问智能体后的响应体
class RoleAgentAssignmentResponse(BaseModel):
    """Response after updating role's accessible agents."""

    # 角色 ID
    role_id: str = Field(..., description="Role ID")
    # 角色名称
    role_name: str = Field(..., description="Role name")
    # 更新后该角色允许使用的智能体 ID 列表
    allowed_agents: list[str] = Field(default_factory=list, description="List of allowed agent IDs")


# ============================================
# Role Model Schemas
# ============================================
# 本节控制"角色（Role）可以访问哪些底层模型（如具体的 LLM）"的配置。


# 某个角色当前可访问的模型列表（查询结果）
class RoleModelAssignment(BaseModel):
    """Role's accessible models."""

    # 角色 ID
    role_id: str = Field(..., description="Role ID")
    # 角色名称
    role_name: str = Field(..., description="Role name")
    # 该角色被允许使用的模型标识列表
    allowed_models: list[str] = Field(
        default_factory=list, description="List of allowed model values"
    )
    # 该角色是否存在显式的模型分配配置；为 False 时通常表示走系统默认策略（如允许全部/默认集合）
    configured: bool = Field(True, description="Whether this role has an explicit model assignment")


# 更新某角色可访问模型列表的请求体（整体覆盖）
class RoleModelAssignmentUpdate(BaseModel):
    """Update role's accessible models."""

    # 新的允许模型标识列表
    allowed_models: list[str] = Field(..., description="List of allowed model values")


# ============================================
# User Agent Preference Schemas
# ============================================
# 本节记录用户个人的"默认智能体"偏好设置（例如新建会话时默认选中哪个智能体）。


# 用户当前的默认智能体偏好（查询结果）
class UserAgentPreference(BaseModel):
    """User's default agent preference."""

    # 用户设置的默认智能体 ID；未设置时为 None（走系统默认）
    default_agent_id: Optional[str] = Field(None, description="Default agent ID for the user")


# 更新用户默认智能体偏好的请求体
class UserAgentPreferenceUpdate(BaseModel):
    """Update user's default agent preference."""

    # 新的默认智能体 ID（必填）
    default_agent_id: str = Field(..., description="Default agent ID")


# 查询/更新用户默认智能体偏好操作的响应体
class UserAgentPreferenceResponse(BaseModel):
    """Response for user agent preference operations."""

    # 用户当前的默认智能体 ID
    default_agent_id: Optional[str] = Field(None, description="Default agent ID for the user")

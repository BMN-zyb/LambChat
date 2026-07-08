"""
类型和协议定义

定义系统中的核心类型、协议和枚举。
"""

# 本文件是内核（kernel，"共享内核" Shared Kernel）层的类型定义文件
# 是全项目零依赖的类型基石：不依赖 src 下任何其它业务模块，可被任意层安全导入，避免循环依赖
# 其中 AgentProtocol / StorageProtocol / LLMClientProtocol / Permission
# 会被 src/kernel/__init__.py 选择性重新导出；
# 而 MessageType 和 ToolProtocol 未被 __init__.py 重新导出，
# 需通过 `from src.kernel.types import ToolProtocol` 等方式直接从本模块导入使用
from enum import Enum
from typing import (
    Any,
    AsyncGenerator,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)


# RBAC 权限枚举：每个成员的值都是形如 "资源:动作" 的字符串（例如 "chat:read"）
# 是全项目权限校验的基础，通常配合 src/infra/auth/rbac.py 中的角色-权限映射表使用
# （该映射表以 Permission.XXX.value 的形式引用这些枚举值）
# 各业务 API 路由通过权限依赖注入，判断当前角色/用户是否有权限调用某个接口
class Permission(str, Enum):
    """权限枚举"""

    # 聊天消息的读取/发送权限
    # Chat
    CHAT_READ = "chat:read"
    CHAT_WRITE = "chat:write"

    # 会话（Session）的读取/写入/删除/管理/分享权限
    # Session
    SESSION_READ = "session:read"
    SESSION_WRITE = "session:write"
    SESSION_DELETE = "session:delete"
    SESSION_ADMIN = "session:admin"
    SESSION_SHARE = "session:share"

    # 技能（Skill）的读取/写入/删除/管理权限
    # Skill
    SKILL_READ = "skill:read"
    SKILL_WRITE = "skill:write"
    SKILL_DELETE = "skill:delete"
    SKILL_ADMIN = "skill:admin"

    # 用户账号管理权限（管理员功能）
    # User (Admin)
    USER_READ = "user:read"
    USER_WRITE = "user:write"
    USER_DELETE = "user:delete"

    # 角色管理权限（管理员功能）
    # Role (Admin)
    ROLE_MANAGE = "role:manage"

    # 系统设置管理权限（管理员功能）
    # Settings (Admin)
    SETTINGS_MANAGE = "settings:manage"

    # MCP 服务器/工具连接相关权限；写权限按 MCP 服务器的传输/部署方式细分为
    # SSE、HTTP、沙箱（Sandbox）三类，分别对应三种不同的接入方式，并非重复项，
    # 用于对不同接入方式的写操作做更精细的权限控制
    # MCP
    MCP_READ = "mcp:read"
    MCP_WRITE_SSE = "mcp:write_sse"
    MCP_WRITE_HTTP = "mcp:write_http"
    MCP_WRITE_SANDBOX = "mcp:write_sandbox"
    MCP_DELETE = "mcp:delete"
    MCP_ADMIN = "mcp:admin"

    # 文件上传权限，按文件类型（图片/视频/音频/文档）细分
    # File
    FILE_UPLOAD = "file:upload"
    FILE_UPLOAD_IMAGE = "file:upload:image"
    FILE_UPLOAD_VIDEO = "file:upload:video"
    FILE_UPLOAD_AUDIO = "file:upload:audio"
    FILE_UPLOAD_DOCUMENT = "file:upload:document"

    # 用户反馈的写入/读取/管理权限
    # Feedback
    FEEDBACK_WRITE = "feedback:write"
    FEEDBACK_READ = "feedback:read"
    FEEDBACK_ADMIN = "feedback:admin"

    # 头像上传权限
    # Avatar
    AVATAR_UPLOAD = "avatar:upload"

    # 统一渠道 API 使用的通用权限：Channel 在本项目中指对外接入的消息渠道
    # （例如企业 IM、机器人等外部集成），这里不区分具体渠道类型，
    # 是与渠道类型无关的通用读/写/删权限
    # Channel - Generic (for unified channel API)
    CHANNEL_READ = "channel:read"
    CHANNEL_WRITE = "channel:write"
    CHANNEL_DELETE = "channel:delete"

    # Agent 相关权限
    # Agent
    AGENT_READ = "agent:read"
    AGENT_ADMIN = "agent:admin"

    # 团队（Team）相关权限
    # Team
    TEAM_READ = "team:read"
    TEAM_WRITE = "team:write"
    TEAM_DELETE = "team:delete"

    # 模型管理权限
    # Model
    MODEL_ADMIN = "model:admin"

    # 技能/插件市场相关权限：浏览、发布、管理
    # Marketplace
    MARKETPLACE_READ = "marketplace:read"
    MARKETPLACE_PUBLISH = "marketplace:publish"
    MARKETPLACE_ADMIN = "marketplace:admin"

    # 人格/角色预设模板相关权限
    # Persona Preset
    PERSONA_PRESET_READ = "persona_preset:read"
    PERSONA_PRESET_WRITE = "persona_preset:write"
    PERSONA_PRESET_ADMIN = "persona_preset:admin"

    # 定时任务相关权限：控制"谁能操作定时任务"（读/写/删）
    # 注意与 src/kernel/config/_definitions_tools.py 中 ENABLE_SCHEDULED_TASK 配置项的区别：
    # 那是"整个定时任务功能是否启用"的全局开关，这里是启用之后的细粒度操作权限校验，二者是不同层面的控制
    # Scheduled Task
    SCHEDULED_TASK_READ = "scheduled_task:read"
    SCHEDULED_TASK_WRITE = "scheduled_task:write"
    SCHEDULED_TASK_DELETE = "scheduled_task:delete"

    # 用户可配置环境变量条目的读/写/删权限
    # Environment Variables
    ENVVAR_READ = "envvar:read"
    ENVVAR_WRITE = "envvar:write"
    ENVVAR_DELETE = "envvar:delete"

    # 通知相关管理权限
    # Notification
    NOTIFICATION_MANAGE = "notification:manage"

    # 用量/额度统计相关权限
    # Usage
    USAGE_READ = "usage:read"
    USAGE_ADMIN = "usage:admin"


# 消息角色类型（HUMAN/AI/SYSTEM/TOOL），类似 LangChain 消息的 role 概念
# 实际使用位置见 src/kernel/schemas/message.py 中 Message.type 字段
class MessageType(str, Enum):
    """消息类型"""

    HUMAN = "human"
    AI = "ai"
    SYSTEM = "system"
    TOOL = "tool"


# 下面这四个协议类均使用 Protocol 定义（PEP 544 结构化子类型/鸭子类型）：
# 不需要显式继承该协议，只要一个类拥有协议中声明的同名属性/方法，就被视为实现了该协议
# @runtime_checkable 使协议可以配合 isinstance()/issubclass() 做运行时检查，
# 但这种运行时检查只验证方法/属性名是否存在，并不校验参数类型和方法签名是否匹配，
# 这是 Python typing 模块的已知局限，实现类需自行保证签名与协议声明一致
@runtime_checkable
class AgentProtocol(Protocol):
    """Agent 协议接口"""
    # 说明：截至目前代码库中未发现显式实现或依赖本协议的具体类，
    # 本协议更像是面向未来扩展预留的抽象接口契约，而非已有实现类正在遵循的协议
    # 定义了一个 Agent 应具备的最小接口：三个只读属性 + 初始化方法 + 非流式/流式两种对话方式

    @property
    def agent_id(self) -> str:
        """Agent ID"""
        ...

    @property
    def name(self) -> str:
        """Agent 名称"""
        ...

    @property
    def description(self) -> str:
        """Agent 描述"""
        ...

    async def initialize(self) -> None:
        """初始化 Agent"""
        ...

    # 非流式对话：message 为用户输入的消息文本，session_id 用于标识/隔离多轮对话的会话上下文；
    # 等待模型生成完整回复后，一次性返回完整的回复字符串
    async def chat(self, message: str, session_id: str = "default") -> str:
        """非流式聊天"""
        ...

    # 流式对话：参数含义与 chat 相同，区别在于不等待完整回复生成完毕，
    # 而是返回一个异步生成器，随生成过程逐块（chunk）产出 dict 类型的回复片段
    async def stream_chat(
        self, message: str, session_id: str = "default"
    ) -> AsyncGenerator[dict, None]:
        """流式聊天"""
        ...


@runtime_checkable
class StorageProtocol(Protocol):
    """存储协议接口"""
    # 说明：截至目前代码库中未发现显式实现或依赖本协议的具体类，是预留的抽象接口契约
    # 定义了一个通用异步键值存储应具备的接口：读取 / 写入（可选 ttl 过期时间）/ 删除 / 判断是否存在

    async def get(self, key: str) -> Optional[Any]:
        """获取数据"""
        ...

    # 写入数据：key 为键，value 为待存储的值（任意类型），
    # ttl 为可选的过期时间（单位：秒），不传则表示永久保存、不过期
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """设置数据"""
        ...

    async def delete(self, key: str) -> bool:
        """删除数据"""
        ...

    async def exists(self, key: str) -> bool:
        """检查是否存在"""
        ...


@runtime_checkable
class LLMClientProtocol(Protocol):
    """LLM 客户端协议接口"""
    # 说明：截至目前代码库中未发现显式实现或依赖本协议的具体类，是预留的抽象接口契约
    # 定义了一个通用 LLM 客户端应具备的接口：非流式补全 / 流式补全，
    # 二者均接受统一的 messages 消息列表和采样参数

    # 非流式补全：messages 为对话消息列表（每条消息形如 {"role": ..., "content": ...} 的字典），
    # temperature 为采样温度（值越高输出越随机多样，值越低越趋于确定/保守），
    # max_tokens 为本次生成允许产出的最大 token 数；等待模型生成完毕后返回完整的回复字符串
    async def complete(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        """非流式完成"""
        ...

    # 流式补全：参数含义与 complete 相同，区别在于返回一个异步生成器，
    # 随生成过程逐块产出字符串片段，而不是等待完整结果后一次性返回
    async def stream_complete(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[str, None]:
        """流式完成"""
        ...


@runtime_checkable
class ToolProtocol(Protocol):
    """工具协议接口"""
    # 说明：截至目前代码库中未发现显式实现或依赖本协议的具体类，是预留的抽象接口契约
    # 定义了一个"工具"应具备的接口：name/description 只读属性 + execute 执行方法
    # 注意：本协议未被 src/kernel/__init__.py 重新导出，需直接从 src.kernel.types 模块导入使用

    @property
    def name(self) -> str:
        """工具名称"""
        ...

    @property
    def description(self) -> str:
        """工具描述"""
        ...

    # 执行工具：接受任意关键字参数（**kwargs，具体参数名和含义由每个工具自行定义），
    # 返回值类型不固定（Any），具体返回内容由工具的实际实现决定
    async def execute(self, **kwargs: Any) -> Any:
        """执行工具"""
        ...

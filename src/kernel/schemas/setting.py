"""
Setting schemas for API request/response
"""

# 本模块定义系统设置（Setting）相关的数据模型。
# 系统设置项本身在 src/kernel/config/_definitions_*.py 中以字典形式静态声明
# （包含默认值、类型、分类、依赖关系等），运行时的读取、覆盖与持久化由
# src/infra/settings/service.py、src/infra/settings/storage.py 负责；
# src/api/routes/settings.py 通过本模块的模型对外暴露设置的查询与更新接口。
from enum import Enum
from typing import Any, Optional, Union

from pydantic import BaseModel, Field


# 表示某个设置项的可见性/生效性依赖于另一个设置项的取值，
# 用于前端设置页面的联动显示（例如仅当某开关打开时才显示其子设置）。
class SettingDependsOn(BaseModel):
    """Setting dependency condition"""

    # 被依赖的父级设置项的 key
    key: str  # Parent setting key
    # 父级设置项需要等于该值时，当前设置项才可见/生效
    value: Any  # Expected value for visibility


class SettingType(str, Enum):
    """Setting value type"""

    # 普通单行字符串输入
    STRING = "string"
    TEXT = "text"  # Long text (renders as textarea)
    # 数字输入
    NUMBER = "number"
    # 布尔开关（true/false）
    BOOLEAN = "boolean"
    # 任意 JSON 结构（数组或对象），具体结构由 json_schema 字段描述
    JSON = "json"
    SELECT = "select"  # Dropdown select (uses options field)


class SettingCategory(str, Enum):
    """Setting category for grouping"""

    # 前端展示与交互相关设置
    FRONTEND = "frontend"
    # Agent 运行时行为相关设置
    AGENT = "agent"
    # 大语言模型调用相关设置（如默认模型、超时时间等）
    LLM = "llm"
    # 会话（Session）相关设置
    SESSION = "session"
    # MongoDB 数据库连接相关设置
    MONGODB = "mongodb"
    # Redis 连接相关设置
    REDIS = "redis"
    # LangGraph 检查点（对话状态持久化）后端相关设置
    CHECKPOINT = "checkpoint"
    # 长期存储（PostgreSQL）连接相关设置
    LONG_TERM_STORAGE = "long_term_storage"
    # 安全相关设置（如密钥、Token 有效期等）
    SECURITY = "security"
    # 邮件发送（注册验证、找回密码等）相关设置
    EMAIL = "email"
    # 验证码相关设置
    CAPTCHA = "captcha"
    # S3 兼容对象存储相关设置
    S3 = "s3"
    # 文件上传相关设置（大小限制、数量限制等）
    FILE_UPLOAD = "file_upload"
    # 代码执行沙箱相关设置
    SANDBOX = "sandbox"
    # Agent 技能（skills）相关设置
    SKILLS = "skills"
    # 工具调用相关设置
    TOOLS = "tools"
    # 链路追踪（如 LangSmith）相关设置
    TRACING = "tracing"
    # 用户体系相关设置
    USER = "user"
    # 第三方 OAuth 登录相关设置
    OAUTH = "oauth"
    # 长期记忆功能总开关等相关设置
    MEMORY = "memory"
    # 记忆功能使用的 Embedding 模型相关设置
    MEMORY_EMBEDDING = "memory_embedding"
    # 记忆检索与索引相关设置
    MEMORY_SEARCH = "memory_search"
    # 记忆存储相关设置
    MEMORY_STORAGE = "memory_storage"
    # 语音转写相关设置
    AUDIO_TRANSCRIPTION = "audio_transcription"
    # 定时任务相关设置
    SCHEDULED_TASK = "scheduled_task"


class JsonSchemaField(BaseModel):
    """Field definition within a JSON schema"""

    # 字段名（对应 JSON 值中的 key）
    name: str
    type: str = "text"  # text, password, number, toggle, select
    label: str  # i18n key
    # 输入框占位提示文本
    placeholder: Optional[str] = None
    # 该字段是否必填
    required: bool = False
    options: Optional[list[str]] = None  # for select type
    layout_width: Optional[str] = None  # compact or full


class JsonSchema(BaseModel):
    """Schema describing the structure of a JSON-type setting"""

    # 顶层结构类型："array"（列表）或 "object"（键值对）
    type: str  # "array" or "object"
    item_label: Optional[str] = None  # i18n key for array items
    key_label: Optional[str] = None  # i18n key for object keys (object type)
    value_type: Optional[str] = None  # "array" for object values that are arrays
    key_options: Optional[list[str]] = None  # allowed keys for object type
    # 当 type 为 "object" 时用于渲染值部分表单的字段定义；
    # 当 type 为 "array" 时用于渲染每个数组元素的字段定义
    fields: list[JsonSchemaField] = []


class SettingItem(BaseModel):
    """Single setting item"""

    # 设置项唯一标识（如 "ENABLE_MEMORY"、"POSTGRES_HOST" 等，通常是大写环境变量风格的名字）
    key: str
    # 当前生效的值
    value: Any
    # 值的类型，决定前端渲染成何种输入控件
    type: SettingType
    # 所属分类，用于设置页面分组展示
    category: SettingCategory
    # 分类下的二级分组（自由字符串，如 "connection"、"pool" 等），默认不分组
    subcategory: str = ""
    # 设置项说明（通常是 i18n key，由前端翻译展示）
    description: str = ""
    # 默认值，用于"恢复默认值"等场景的对比展示
    default_value: Any = None
    # 修改该设置后是否需要重启服务才能生效
    requires_restart: bool = False
    # 是否为敏感信息（如密码、密钥），敏感设置返回给前端时会被脱敏处理
    is_sensitive: bool = False
    # 该设置是否允许在前端设置页面中展示（部分内部设置不对前端暴露）
    frontend_visible: bool = False
    # 可见性依赖条件：字符串表示依赖的父设置 key（按父设置为真值判断），
    # SettingDependsOn 则显式指定父设置 key 与期望值，None 表示不依赖任何设置
    depends_on: Optional[Union[str, SettingDependsOn]] = (
        None  # Key or condition for visibility control
    )
    options: Optional[list[str]] = None  # Available options for SELECT type
    json_schema: Optional[JsonSchema] = None  # Schema for JSON-type settings
    # 最近一次更新时间（ISO 字符串）
    updated_at: Optional[str] = None
    # 最近一次更新该设置的用户标识
    updated_by: Optional[str] = None


class SettingUpdate(BaseModel):
    """Setting update request"""

    # 待写入的新值，具体校验规则由后端根据该设置项的 SettingType 决定
    value: Any


class SettingsResponse(BaseModel):
    """Settings grouped by category"""

    # 按分类名（SettingCategory 的取值）分组的设置项列表，
    # 便于前端按分类渲染设置页面的各个分区
    settings: dict[str, list[SettingItem]] = Field(default_factory=dict)


class SettingUpdateResponse(BaseModel):
    """Response after updating a setting"""

    # 更新后的完整设置项（包含新值）
    setting: SettingItem
    # 给前端展示的提示信息
    message: str
    # 是否需要重启服务才能使本次更新生效
    requires_restart: bool


class SettingResetResponse(BaseModel):
    """Reset response"""

    # 给前端展示的提示信息
    message: str
    # 本次被重置为默认值的设置项数量
    reset_count: int

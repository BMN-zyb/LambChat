"""Settings class definition."""
# 本模块是配置系统的核心：定义 Settings 类（继承 pydantic-settings 的 BaseSettings），
# 声明了全部配置项的静态字段、类型与默认值，并在 __init__ 中处理若干"启动期一次性"
# 的副作用逻辑（缺失密钥的自动生成/扩展、VAPID 密钥对生成、写入 git 版本信息、
# 同步 LangSmith 相关环境变量等）。文件末尾提供 get_settings()/settings，
# 是整个项目其它模块获取配置的唯一入口。
# 注意：这里每个字段的默认值只是给 pydantic 的静态类型声明；大部分配置项在
# definitions.py 的 SETTING_DEFINITIONS 里还有一份"元数据"（含同名的 "default"），
# 二者理应保持一致——修改某个配置的默认值时通常需要同时改这两处。

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Optional

# Field：自定义字段的默认值工厂/元数据；PrivateAttr：声明不参与 pydantic
# 校验/序列化的私有运行时状态；field_validator：自定义字段校验/归一化逻辑
from pydantic import Field, PrivateAttr, field_validator
# BaseSettings 在实例化时会自动从环境变量、.env 文件（见 model_config）中
# 按字段名（大小写不敏感）读取并覆盖对应字段的值
from pydantic_settings import BaseSettings

from src.infra.logging import get_logger

# 复用 constants.py 中的安全长度阈值，以及 utils.py 中的密钥扩展函数、
# 版本号读取函数、项目根路径、启动时读取到的 git tag/commit
from .constants import JWT_SECRET_KEY_MIN_LENGTH, MCP_ENCRYPTION_SALT_MIN_LENGTH
from .utils import (
    COMMIT_HASH,
    GIT_TAG,
    PROJECT_ROOT,
    expand_encryption_salt,
    expand_jwt_secret_key,
    get_app_version,
)

# S3Config 只用于 get_s3_config() 方法的返回类型标注；真正使用时该方法内部会
# 重新 import，这里放在 TYPE_CHECKING 下是为了避免 kernel 层在模块加载阶段
# 就直接依赖 infra.storage.s3
if TYPE_CHECKING:
    from src.infra.storage.s3 import S3Config

logger = get_logger(__name__)


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    Default values are defined in SETTING_DEFINITIONS (single source of truth).
    This class uses pydantic-settings to load from .env and environment variables.
    Runtime values can be updated from database via initialize_settings().
    """
    # 类字段大致分两类：
    # 1) 下面这一批标注了"(not in SETTING_DEFINITIONS - ...)"的，是纯内部/开发者用途，
    #    不会出现在管理后台设置页面，也不支持通过数据库热更新；
    # 2) "All settings below get defaults from SETTING_DEFINITIONS"分隔线之后的字段，
    #    在 definitions.py 里都有对应的元数据（分类、描述、是否敏感等），
    #    支持管理后台修改并通过 service.py 热更新到这里。

    # Application (not in SETTING_DEFINITIONS - internal use only)
    APP_NAME: str = "LambChat"
    # 用 default_factory 而不是直接调用 get_app_version()：把"读取 pyproject.toml"
    # 这个文件 IO 延迟到 Settings 实例真正被创建时才执行，而不是在类定义（模块加载）时执行
    APP_VERSION: str = Field(default_factory=get_app_version)

    # Version Info (populated at startup)
    # 这三个字段默认是 None，实际值在 __init__ 里被填充：
    # GIT_TAG/COMMIT_HASH 来自 utils.py 模块加载时执行的 git 命令结果，
    # BUILD_TIME 来自环境变量（通常由 CI/构建脚本注入）；三者也都可以被环境变量显式覆盖
    GIT_TAG: Optional[str] = None
    COMMIT_HASH: Optional[str] = None
    BUILD_TIME: Optional[str] = None
    GITHUB_URL: str = "https://github.com/Yanyutin753/LambChat"

    # Debug (not in SETTING_DEFINITIONS - developer toggle)
    DEBUG_STREAM_EVENTS: bool = False

    # Logging Configuration (not in SETTING_DEFINITIONS - internal use only)
    LOG_LEVELS: str = ""
    # %(trace_context)s 是自定义日志字段，用于在日志里带上请求/trace 的追踪上下文，
    # 方便按 trace 把分散在各处的日志串起来排查问题
    LOG_FORMAT: str = (
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(trace_context)s%(name)s - %(message)s"
    )
    LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"

    # Session Configuration (not in SETTING_DEFINITIONS)
    SESSION_MAX_MESSAGES: int = 20
    SESSION_MAX_EVENTS_PER_TRACE: int = 50000  # 单个 trace 最多保留的事件数，防止内存爆炸
    SESSION_EVENT_READ_DEFAULT_LIMIT: int = 1000
    SESSION_EVENT_MONGO_BUFFER_MAX: int = 10000
    SESSION_EVENT_TTL_CACHE_MAX: int = 5000
    SESSION_EVENT_REDIS_REPLAY_BATCH_SIZE: int = 500
    # 开启后，新产生的 trace 事件不再无限追加进单个大文档的 events 数组，而是拆分保存到
    # 独立的 chunk 文档中（大小见下面 SESSION_EVENT_CHUNK_SIZE），用于规避 MongoDB
    # 单文档 16MB 的大小限制
    SESSION_EVENT_CHUNK_STORAGE_ENABLED: bool = False
    # 启用分片存储后，是否同时按旧方式把事件也写入旧的 events 数组（双写）；
    # 用于迁移/兼容过渡期间不丢数据，确认新方案稳定后可关闭以节省存储和写入开销
    SESSION_EVENT_CHUNK_DUAL_WRITE_LEGACY: bool = False
    SESSION_EVENT_CHUNK_SIZE: int = 5000
    # 飞书文件上传接口自身的大小限制（20MB），与下面通用的 FILE_UPLOAD_MAX_SIZE_* 是
    # 两套独立的限制，互不影响
    FEISHU_UPLOAD_BYTES_MAX_SIZE: int = 20 * 1024 * 1024

    # ============================================
    # All settings below get defaults from SETTING_DEFINITIONS
    # ============================================
    # 下面这些字段在 definitions.py 的 SETTING_DEFINITIONS 里都有一份对应的元数据
    # （包含分类、i18n 描述、是否敏感、是否需要重启等），且默认值理应与那边的
    # "default" 保持一致：这里的默认值供 pydantic 静态类型/IDE 补全使用，
    # definitions.py 里的默认值则是管理后台展示、数据库首次初始化时真正采用的来源。

    # Application Settings
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    APP_BASE_URL: str = ""  # e.g. https://lambchat.example.com — 用于生成文件 URL 的固定前缀
    LOG_LEVEL: str = "INFO"

    # LLM Settings
    LLM_MAX_RETRIES: int = 3
    LLM_RETRY_DELAY: float = 1.0
    LLM_MODEL_CACHE_SIZE: int = 50  # 模型实例缓存大小，防止内存泄漏
    # 以下两项对应 Anthropic 风格的 prompt caching：一次请求最多打几个 system 段的
    # cache 断点、最多给几个 tool 定义打 cache 断点，超出部分不再享受缓存加速
    PROMPT_CACHE_MAX_SYSTEM_BLOCKS: int = 4
    PROMPT_CACHE_MAX_TOOLS: int = 1

    # MCP Settings
    ENABLE_MCP: bool = True
    # 当可用工具数超过 DEFERRED_TOOL_THRESHOLD 时，不再把所有工具的完整 schema
    # 一次性塞进 LLM 的上下文，而是先给模型一份"可搜索的工具列表"，
    # 模型按需调用 search_tools 检索、再展开具体工具，避免上下文被工具定义占满
    ENABLE_DEFERRED_TOOL_LOADING: bool = True
    DEFERRED_TOOL_THRESHOLD: int = 20
    # 延迟加载模式下，一次 search_tools 最多返回多少个匹配结果
    DEFERRED_TOOL_SEARCH_LIMIT: int = 25
    DEFERRED_TOOL_PROMPT_LIMIT: int = 25
    # 以下 MCP_GLOBAL_* 是进程级全局单例 MCP 管理器（跨用户共享）的缓存/预热参数
    MCP_GLOBAL_CACHE_TTL_SECONDS: int = 900
    MCP_GLOBAL_MAX_ENTRIES: int = 100
    MCP_GLOBAL_INIT_WAIT_SECONDS: int = 5
    MCP_GLOBAL_WARMUP_CONCURRENCY: int = 5
    MCP_GLOBAL_WARMUP_MAX_USERS: int = 100
    # 以下 MCP_USER_* 是用户级 MCP 缓存（区别于上面的进程级全局缓存）的参数
    MCP_USER_CACHE_TTL_SECONDS: int = 900
    MCP_USER_CACHE_MAX_ENTRIES: int = 100
    # MCP 连接池：控制与各 MCP server 之间实际网络连接的存活时间和最大并发连接数
    MCP_POOL_TTL_SECONDS: int = 900
    MCP_POOL_MAX_CONNECTIONS: int = 100
    MCP_SERVER_LOAD_CONCURRENCY: int = 4
    # 单个"生效配置"里允许的 server/tool 数量上限，超出则拒绝或截断，防止配置过大拖慢系统
    MCP_EFFECTIVE_CONFIG_MAX_SERVERS: int = 100
    MCP_EFFECTIVE_CONFIG_MAX_TOOLS: int = 200
    MCP_ENCRYPTION_SALT: Optional[str] = None  # 默认随机生成，确保加密一致性
    # deepagents 框架单次调用允许的输入 token 上限，防止上下文超出模型限制
    DEEPAGENT_DEFAULT_MAX_INPUT_TOKENS: int = 64000

    # Session Settings
    SESSION_MAX_RUNS_PER_SESSION: int = 100
    ENABLE_MESSAGE_HISTORY: bool = True
    # SSE 事件缓存的存活时间（秒），用于客户端断线重连后补发错过的事件
    SSE_CACHE_TTL: int = 86400
    SESSION_SEARCH_BACKFILL_STARTUP_DELAY_SECONDS: float = 30.0
    # 会话标题自动生成可以使用与主对话完全不同的模型/端点/密钥（通常配置一个更便宜、
    # 更快的小模型）；下面的 SESSION_TITLE_PROMPT 是生成标题用的提示词模板，
    # 其中 {lang}/{message} 是运行时填充的占位符
    SESSION_TITLE_MODEL: str = ""
    SESSION_TITLE_API_BASE: str = ""
    SESSION_TITLE_API_KEY: str = ""
    SESSION_TITLE_PROMPT: str = "请您用简短的3-5个字的标题加上一个表情符号作为用户对话的提示标题。请您选取适合用于总结的表情符号来增强理解，但请避免使用符号或特殊格式。请您根据提示回复一个提示标题文本。\n\n回复示例：\n\n📉 股市趋势\n\n🍪 完美巧克力曲奇食谱\n\n🎮 视频游戏开发洞察\n\n# 重要\n\n1. 请务必用{lang}回复我\n2. 回复字数控制在3-5个字\n\nPrompt: {message}"
    # 是否在每次回复后，让模型顺带生成几个"猜你想问"的追问建议
    ENABLE_RECOMMEND_QUESTIONS: bool = True
    # 生成追问建议是异步后台任务，这里限制同时进行的此类后台任务数量上限
    RECOMMEND_QUESTIONS_MAX_BACKGROUND_TASKS: int = 8

    # Redis Settings
    # Redis 在项目中承担缓存、发布/订阅（如 MCP 全局缓存失效通知）、
    # 分布式锁等多种角色，是运行在多进程/多副本模式下常见的共享基础设施
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_PASSWORD: Optional[str] = None

    # Task execution settings
    # TASK_BACKEND 决定后台/异步任务如何执行："local" 表示直接在当前进程内跑，
    # 适合单机开发；"arq" 表示交给基于 Redis 的 arq 任务队列，支持多进程/多副本消费
    TASK_BACKEND: str = "arq"  # local | arq
    # 是否在当前进程内嵌入启动一个 arq worker，而不是要求单独部署一个独立的 worker 进程；
    # 小规模部署图省事可以嵌入，大规模/生产部署通常建议关闭并单独扩缩容 worker
    ARQ_EMBEDDED_WORKER: bool = True
    ARQ_QUEUE_NAME: str = "lambchat:arq"
    ARQ_WORKER_MAX_JOBS: int = 128
    ARQ_JOB_TIMEOUT_SECONDS: int = 86400
    # 进程启动阶段清理历史遗留/僵死任务时的并发度上限
    TASK_STARTUP_CLEANUP_CONCURRENCY: int = 16

    # MongoDB Settings
    # MongoDB 是本项目的主数据存储：会话、trace 事件、用量日志等均落在这里
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "agent_state"
    MONGODB_USERNAME: str = ""
    MONGODB_PASSWORD: str = ""
    MONGODB_AUTH_SOURCE: str = "admin"
    MONGODB_SESSIONS_COLLECTION: str = "sessions"
    MONGODB_TRACES_COLLECTION: str = "traces"
    # 仅在 SESSION_EVENT_CHUNK_STORAGE_ENABLED 开启分片存储后才会实际写入，
    # 用于存放从 traces 文档中拆分出来的事件分片，规避单文档 16MB 限制
    MONGODB_TRACE_EVENT_CHUNKS_COLLECTION: str = "trace_event_chunks"
    MONGODB_USAGE_LOGS_COLLECTION: str = "usage_logs"
    MONGODB_STORE_BATCH_CONCURRENCY: int = 16

    # Event Merger Settings
    # 智能体执行过程中产生的 trace 事件不是每一条都立即单独写库，而是先在内存里
    # 缓冲，再按下面的间隔/批量大小定期合并写入 MongoDB，减少高频小写入带来的开销
    ENABLE_EVENT_MERGER: bool = True  # 是否启用事件合并
    EVENT_MERGE_INTERVAL: float = 300.0  # 合并间隔（秒，默认 1 分钟）
    # 单次合并写入的事件批量大小，以及允许多少个合并任务并发执行
    EVENT_MERGE_BATCH_SIZE: int = 100
    EVENT_MERGE_CONCURRENCY: int = 3
    EVENT_MERGE_TIMEOUT_SECONDS: float = 120.0
    # 与顶部 SESSION_MAX_EVENTS_PER_TRACE 呼应，在合并阶段也做一次同样的上限保护
    EVENT_MERGE_MAX_EVENTS_PER_TRACE: int = 50000
    # 防抖：事件密集连续到达时不必每条都触发一次合并，等待这段静默期后才真正执行，
    # 避免短时间内重复触发大量合并操作
    EVENT_MERGE_IMMEDIATE_DEBOUNCE_SECONDS: float = 2.0

    # Memory Monitoring Settings
    # 注意：这里的"Memory"指进程占用的物理内存（RAM），是运维/稳定性范畴的
    # 内存泄漏监控功能；不要与后文的 "Memory Settings (Master Switch)"/
    # "Native Memory Settings"（AI 智能体的长期记忆功能）混淆，两者只是碰巧
    # 都用了 Memory 这个词，功能上完全无关
    MEMORY_MONITOR_ENABLED: bool = True
    # 采样间隔（秒）：多久检查一次当前进程的内存占用
    MEMORY_MONITOR_INTERVAL_SECONDS: float = 60.0
    # 最多保留多少个历史采样点用于趋势分析，超出后旧数据被淘汰
    MEMORY_MONITOR_HISTORY_LIMIT: int = 60
    # 内存增长超过该阈值（MB）时判定为疑似泄漏并触发告警
    MEMORY_MONITOR_LEAK_THRESHOLD_MB: int = 128
    # 进行趋势判断前至少需要积累的样本数，避免进程刚启动、样本太少时误报
    MEMORY_MONITOR_MIN_SAMPLES: int = 5
    # 告警冷却时间（秒）：同一问题在冷却期内不会重复告警，避免刷屏
    MEMORY_MONITOR_ALERT_COOLDOWN_SECONDS: float = 600.0
    # 触发诊断时，记录调用栈的最大深度，以及展示内存占用最高的前 N 项分配点，
    # 用于定位是哪里的代码在持续吃内存
    MEMORY_MONITOR_TRACEBACK_LIMIT: int = 8
    MEMORY_MONITOR_TOP_STATS_LIMIT: int = 8
    # 诊断时最多列出多少种 gc 追踪的对象类型及其数量
    MEMORY_MONITOR_GC_OBJECT_LIMIT: int = 10
    # 是否开启更详细但更昂贵的诊断手段（如 tracemalloc 全量快照）；
    # 默认关闭是因为这类诊断本身也会消耗额外的 CPU/内存
    MEMORY_MONITOR_HEAVY_DIAGNOSTICS: bool = False

    # Long-term Storage Settings
    # 是否启用 Postgres 作为补充的长期存储（默认主存储是 MongoDB，Postgres 目前
    # 主要用于 LangGraph checkpoint，见下面 CHECKPOINT_BACKEND）
    ENABLE_POSTGRES_STORAGE: bool = False
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "langgraph"
    POSTGRES_POOL_MIN_SIZE: int = 2
    POSTGRES_POOL_MAX_SIZE: int = 10

    # Checkpoint Backend Settings
    # LangGraph 用于持久化对话执行状态（checkpoint）的后端，可选 "mongodb" 或
    # postgres；这组配置同时出现在 constants.py 的 RESTART_REQUIRED_SETTINGS
    # （修改后建议重启）和 service.py 的 _CHECKPOINT_AFFECTED_SETTINGS
    # （修改后会自动尝试重置连接池，尽力做到不重启也能生效）
    CHECKPOINT_BACKEND: str = "mongodb"
    CHECKPOINT_PG_HOST: str = ""  # empty = fallback to POSTGRES_*
    # 下面几个 CHECKPOINT_PG_* 字段彼此独立：为空时各自单独回退到对应的
    # POSTGRES_* 字段（见下方 checkpoint_postgres_url 属性的实现），
    # 因此可以只覆盖其中一部分（例如只换 host，其余复用共享的 Postgres 账号）
    CHECKPOINT_PG_PORT: int = 5432
    CHECKPOINT_PG_USER: str = ""
    CHECKPOINT_PG_PASSWORD: str = ""
    CHECKPOINT_PG_DB: str = ""
    CHECKPOINT_PG_POOL_MIN_SIZE: int = 2
    CHECKPOINT_PG_POOL_MAX_SIZE: int = 10

    # Sandbox Settings
    # ENABLE_SANDBOX 是沙箱功能总开关；SANDBOX_PLATFORM 选择具体使用哪家沙箱后端
    # （daytona/e2b/cube），下面三组 Daytona/E2B/CubeSandbox 各自维护一套独立的
    # 连接凭据与生命周期参数，但运行时只有 SANDBOX_PLATFORM 指定的那一组会被使用
    ENABLE_SANDBOX: bool = True
    SANDBOX_PLATFORM: str = "daytona"
    DAYTONA_API_KEY: str = ""
    DAYTONA_SERVER_URL: str = ""
    DAYTONA_TIMEOUT: int = 180
    DAYTONA_IMAGE: str = ""
    # grep 类搜索操作在沙箱内执行的超时时间，防止大目录搜索长时间卡住
    SANDBOX_GREP_TIMEOUT: int = 30
    SANDBOX_MCP_REBUILD_CONCURRENCY: int = 4
    # 空闲沙箱的生命周期梯度控制，用于降低长期占用云端沙箱资源的成本：
    # 先自动停止（间隔最短），停止后达到归档间隔则自动归档，
    # 最后达到删除间隔（1440，明显比前两者大一个量级）后彻底删除释放资源；
    # 具体计时单位以 Daytona 自身的定义为准，这里仅保证三者的相对大小关系正确
    DAYTONA_AUTO_STOP_INTERVAL: int = 5
    DAYTONA_AUTO_ARCHIVE_INTERVAL: int = 5
    DAYTONA_AUTO_DELETE_INTERVAL: int = 1440

    # E2B Settings
    E2B_API_KEY: str = ""
    E2B_TEMPLATE: str = "base"
    E2B_TIMEOUT: int = 3600
    # 空闲自动暂停以节省费用，下一次使用时自动恢复，效果上类似 Daytona 的自动停止/
    # 归档，只是 E2B SDK 把这两个动作合并成一对开关
    E2B_AUTO_PAUSE: bool = True
    E2B_AUTO_RESUME: bool = True

    # CubeSandbox Settings
    # CubeSandbox 是可自托管的沙箱后端；CUBE_API_URL 指向其控制面 API 地址
    CUBE_API_URL: str = "http://127.0.0.1:3000"
    CUBE_TEMPLATE: str = ""
    # 沙箱内部暴露的 HTTP 服务需要通过一个反向代理节点才能从外部访问，
    # 以下三项描述该代理节点的地址、端口与域名后缀
    CUBE_PROXY_NODE_IP: str = ""
    CUBE_PROXY_PORT_HTTP: int = 80
    CUBE_SANDBOX_DOMAIN: str = "cube.app"
    # CUBE_TIMEOUT 是沙箱会话整体存活时长，CUBE_REQUEST_TIMEOUT 是单次 HTTP
    # 请求的超时时间，二者含义不同不可混用
    CUBE_TIMEOUT: int = 3600
    CUBE_REQUEST_TIMEOUT: float = 120.0
    CUBE_AUTO_PAUSE: bool = True
    CUBE_AUTO_RESUME: bool = True

    # Skills Settings
    ENABLE_SKILLS: bool = True

    # Code Interpreter Settings
    # 是否启用"代码解释器"这一具体工具能力；与上面的 ENABLE_SANDBOX/SANDBOX_PLATFORM
    # 是两个独立维度的开关，分别控制"要不要提供沙箱执行环境"和"要不要暴露代码解释器
    # 这个工具"
    ENABLE_CODE_INTERPRETER: bool = False

    # LangSmith Tracing Settings
    # 这些字段在下面 __init__ 里会被同步写入 os.environ，因为 LangSmith 官方 SDK
    # 是直接读环境变量来决定是否开启追踪、连接哪个项目，而不支持在代码里显式传参
    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: Optional[str] = None
    LANGSMITH_PROJECT: str = "lamb-agent"
    LANGSMITH_API_URL: str = "https://api.smith.langchain.com"
    # 采样率：1.0 表示所有请求都上报追踪数据，生产环境流量较大时可调低以节省开销
    LANGSMITH_SAMPLE_RATE: float = 1.0

    # JWT Authentication Settings
    # 这里的 default_factory 只是 pydantic 字段层面"完全没有任何配置来源时"的兜底值；
    # 真正的安全校验（识别占位符、长度不足时确定性扩展）在下面 __init__ 方法中完成，
    # 详见 __init__ 内对应位置的注释
    JWT_SECRET_KEY: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    JWT_ALGORITHM: str = "HS256"
    # 访问令牌有效期较短、刷新令牌有效期较长，是常见的"短期访问+长期刷新"双令牌模式
    ACCESS_TOKEN_EXPIRE_HOURS: int = 24
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # S3 Storage Settings
    # S3_ENABLED 为 False 时，文件上传会退回本地文件系统（见下方
    # ENABLE_LOCAL_FILESYSTEM_FALLBACK/LOCAL_STORAGE_PATH）
    S3_ENABLED: bool = False
    # 对应 get_s3_config() 中 provider_map 的取值范围（aws/aliyun/tencent/minio/
    # custom/local），决定用哪种 S3 兼容协议客户端及默认参数
    S3_PROVIDER: str = "aws"
    # 仅非 AWS 官方服务（如私有部署的 MinIO、自定义兼容服务）通常需要显式指定
    S3_ENDPOINT_URL: Optional[str] = None
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = ""
    S3_CUSTOM_DOMAIN: Optional[str] = None
    # 是否使用"路径风格"寻址（http://host/bucket/key）而非"虚拟主机风格"
    # （http://bucket.host/key）；部分自建/兼容 S3 的服务只支持路径风格
    S3_PATH_STYLE: bool = False
    # 面向用户的普通上传大小上限，与下面内部/系统内部使用的上传上限分开控制，
    # 避免普通用户上传口子被用来上传超大文件
    S3_MAX_FILE_SIZE: int = 10 * 1024 * 1024
    S3_INTERNAL_UPLOAD_MAX_SIZE: int = 50 * 1024 * 1024
    # 桶是否本身就是公开可读的；若是公开桶，通常不需要再生成下面的带签名 URL
    S3_PUBLIC_BUCKET: bool = False
    # 生成的预签名 URL 的有效期（秒）
    S3_PRESIGNED_URL_EXPIRES: int = 7 * 24 * 3600

    # File Upload Settings
    # S3 未启用或不可用时，回退到本地文件系统存储，路径由 LOCAL_STORAGE_PATH 指定
    LOCAL_STORAGE_PATH: str = "./uploads"
    ENABLE_LOCAL_FILESYSTEM_FALLBACK: bool = True
    # 按文件类别分别设置上传大小上限（单位与前端约定一致，通常为 MB）
    FILE_UPLOAD_MAX_SIZE_IMAGE: int = 10
    FILE_UPLOAD_MAX_SIZE_VIDEO: int = 100
    FILE_UPLOAD_MAX_SIZE_AUDIO: int = 50
    FILE_UPLOAD_MAX_SIZE_DOCUMENT: int = 50
    # 单次上传/单条消息最多可附带的文件数量
    FILE_UPLOAD_MAX_FILES: int = 10

    # Frontend Settings
    # 仅本地开发时使用，用于前后端分离调试场景下的跨域/回跳地址
    FRONTEND_DEV_URL: str = ""
    DEFAULT_AGENT: str = "fast"
    DEFAULT_MODEL_ID: str = ""
    # 用 default_factory=lambda 返回一个新列表，而不是直接写 `= [...]`：
    # pydantic 字段默认值如果是可变对象（list/dict），直接赋值会被所有实例共享，
    # 一个实例修改了这个列表会影响到其它实例；用 factory 保证每次实例化都拿到全新列表
    WELCOME_SUGGESTIONS: list = Field(
        default_factory=lambda: [
            {"icon": "🐍", "text": "Create a Python hello world script"},
            {"icon": "📁", "text": "List files in the workspace directory"},
            {"icon": "📄", "text": "Read the README.md file"},
            {"icon": "🔧", "text": "Help me write a shell script"},
        ]
    )
    DEFAULT_USER_ROLE: str = "user"
    # 是否允许用户自助注册；关闭后通常只能由管理员创建账号
    ENABLE_REGISTRATION: bool = True
    # 展示给用户的管理员联系方式（如反馈问题、申请开通等场景），纯展示用途
    ADMIN_CONTACT_EMAIL: str = ""
    ADMIN_CONTACT_URL: str = ""

    # OAuth Settings
    # 三个第三方登录供应商各自独立开关+凭据，互不影响，可任意组合启用
    OAUTH_GOOGLE_ENABLED: bool = False
    OAUTH_GOOGLE_CLIENT_ID: str = ""
    OAUTH_GOOGLE_CLIENT_SECRET: str = ""
    OAUTH_GITHUB_ENABLED: bool = False
    OAUTH_GITHUB_CLIENT_ID: str = ""
    OAUTH_GITHUB_CLIENT_SECRET: str = ""
    OAUTH_APPLE_ENABLED: bool = False
    OAUTH_APPLE_CLIENT_ID: str = ""
    OAUTH_APPLE_CLIENT_SECRET: str = ""
    # Sign in with Apple 的客户端认证方式与 Google/GitHub 不同：它不是一个静态的
    # client secret，而是需要用 TEAM_ID + KEY_ID 对应的私钥动态签发一个短期有效的
    # JWT 作为 client secret，因此比另外两家多出这两个字段
    OAUTH_APPLE_TEAM_ID: str = ""
    OAUTH_APPLE_KEY_ID: str = ""

    # Cloudflare Turnstile Settings
    # Turnstile 是 Cloudflare 提供的验证码/人机校验服务，用于防刷
    TURNSTILE_ENABLED: bool = False
    # SITE_KEY 会暴露给前端页面使用，SECRET_KEY 只在后端用于向 Cloudflare
    # 校验用户提交的凭证，两者敏感级别不同，不能混用
    TURNSTILE_SITE_KEY: str = ""
    TURNSTILE_SECRET_KEY: str = ""
    # 可以按场景分别决定是否强制要求人机校验：登录、注册、修改密码三处各自独立开关
    TURNSTILE_REQUIRE_ON_LOGIN: bool = False
    TURNSTILE_REQUIRE_ON_REGISTER: bool = True
    TURNSTILE_REQUIRE_ON_PASSWORD_CHANGE: bool = True

    # Email Settings (Resend)
    # 基于 Resend 服务发送邮件（找回密码、邮箱验证等场景）
    EMAIL_ENABLED: bool = False
    # 允许配置多个 Resend 发信账号（列表形式），用 default_factory=list 避免
    # 可变默认值在多个实例间共享；具体的账号选择/轮换策略由使用处决定
    RESEND_ACCOUNTS: Any = Field(default_factory=list)
    PASSWORD_RESET_EXPIRE_HOURS: int = 24
    # 是否强制要求邮箱验证通过后才能正常登录/使用账号
    REQUIRE_EMAIL_VERIFICATION: bool = False

    # Memory Settings (Master Switch)
    # 这里的"Memory"指 AI 智能体的长期记忆功能总开关，与前面"Memory Monitoring
    # Settings"（进程内存/RAM 监控）是完全不同的两个概念，仅因命名相似容易混淆；
    # 具体的记忆实现细节由后面的 "Native Memory Settings" 一组字段控制
    ENABLE_MEMORY: bool = False

    # Scheduled Task Settings
    # 是否启用"定时任务"能力，允许智能体创建按计划自动触发执行的任务
    ENABLE_SCHEDULED_TASK: bool = False

    # Web Push (VAPID) Settings
    # 公钥/私钥若都留空，下面 __init__ 会自动生成一对 ECDSA P-256 密钥，
    # 并在 service.py 的 initialize_settings() 中持久化到数据库，避免重启后
    # 生成不同的密钥对导致已订阅推送的浏览器全部失效
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    # Web Push 协议要求提供一个联系方式，推送服务商在异常时可据此联系发送方
    VAPID_SUBJECT: str = "mailto:admin@example.com"

    # Native Memory Settings (MongoDB-backed, zero external deps)
    # 基于 MongoDB 实现的 AI 长期记忆能力（"zero external deps"指不需要
    # 额外引入专门的向量数据库，复用已有的 MongoDB 存储和检索），受上面的
    # ENABLE_MEMORY 总开关控制。以下按用途分组：embedding/检索模型配置、
    # 陈旧数据清理策略、各场景字符数上限、并发控制、自动压缩与自动捕获策略。
    # Embedding 服务用于把记忆文本转换为向量以支持语义检索，可独立于主对话模型单独配置
    NATIVE_MEMORY_EMBEDDING_API_BASE: str = ""
    NATIVE_MEMORY_EMBEDDING_API_KEY: str = ""
    NATIVE_MEMORY_EMBEDDING_MODEL: str = "text-embedding-3-small"
    # 记忆判定为"陈旧"的天数阈值，以及触发清理动作的阈值参数
    NATIVE_MEMORY_STALENESS_DAYS: int = 30
    NATIVE_MEMORY_PRUNE_THRESHOLD: int = 90
    # 是否为记忆启用检索索引，以及索引结果缓存的存活时间（秒）
    NATIVE_MEMORY_INDEX_ENABLED: bool = True
    NATIVE_MEMORY_INDEX_CACHE_TTL: int = 300
    # 用于记忆相关 LLM 处理（如整理、压缩）的模型与连接配置，可独立于主对话模型，
    # 通常配置更轻量/更省成本的模型
    NATIVE_MEMORY_MODEL: str = ""
    NATIVE_MEMORY_COMPACTION_MODEL_ID: str = ""
    NATIVE_MEMORY_API_BASE: str = ""
    NATIVE_MEMORY_API_KEY: str = ""
    # 检索到候选记忆后用于重排序（rerank）以提升相关性排序的模型配置，同样可独立指定
    NATIVE_MEMORY_RERANK_MODEL: str = ""
    NATIVE_MEMORY_RERANK_API_BASE: str = ""
    NATIVE_MEMORY_RERANK_API_KEY: str = ""
    # 记忆内容注入对话上下文时的 token 上限
    NATIVE_MEMORY_MAX_TOKENS: int = 2000
    # 分别是"内联展示"、"批量导入总量"、"压缩处理单次输入"、"合并整理单次输入"
    # 四个不同场景各自独立的字符数上限，避免单次操作内容过大拖慢处理或超出模型上下文
    NATIVE_MEMORY_INLINE_CONTENT_MAX_CHARS: int = 1200
    NATIVE_MEMORY_IMPORT_TOTAL_CONTENT_MAX_CHARS: int = 2_000_000
    NATIVE_MEMORY_COMPACTION_CONTENT_MAX_CHARS: int = 4000
    NATIVE_MEMORY_CONSOLIDATION_INPUT_MAX_CHARS: int = 4000
    # 记忆在底层 store 中使用的命名空间前缀，用于和其它数据分区隔离
    NATIVE_MEMORY_STORE_NAMESPACE: str = "memories"
    # 单条记忆最多追加保留的细节条目数
    NATIVE_MEMORY_APPEND_MAX_DETAILS: int = 8
    # 语义检索召回记忆时的最低相关性分数阈值，低于该分数的候选会被过滤掉
    NATIVE_MEMORY_RECALL_MIN_SCORE: float = 0.3
    # 三个批量操作（补全详情/合并富化/删除内容）各自独立的并发度上限
    NATIVE_MEMORY_HYDRATE_CONCURRENCY: int = 4
    NATIVE_MEMORY_CONSOLIDATION_ENRICH_CONCURRENCY: int = 4
    NATIVE_MEMORY_CONTENT_DELETE_CONCURRENCY: int = 4
    # 自动压缩：定期把零散记忆整理合并，减少冗余；开关、累积多少条后触发的阈值、
    # 检查间隔与最小间隔（避免过于频繁触发）
    NATIVE_MEMORY_AUTO_COMPACT_ENABLED: bool = True
    NATIVE_MEMORY_AUTO_COMPACT_THRESHOLD: int = 40
    NATIVE_MEMORY_AUTO_COMPACT_INTERVAL_SECONDS: int = 43200
    NATIVE_MEMORY_AUTO_COMPACT_MIN_INTERVAL_SECONDS: int = 900
    # 自动捕获：从对话中自动提取值得记住的信息；单次输入字符上限与最大后台任务数
    NATIVE_MEMORY_AUTO_CAPTURE_INPUT_MAX_CHARS: int = 8000
    NATIVE_MEMORY_AUTO_CAPTURE_MAX_TASKS: int = 8

    # Audio transcription tool settings
    ENABLE_AUDIO_TRANSCRIPTION: bool = False
    AUDIO_TRANSCRIPTION_API_KEY: str = ""
    # 兼容 OpenAI 接口协议的转写服务地址，可指向官方或第三方兼容服务
    AUDIO_TRANSCRIPTION_BASE_URL: str = ""
    AUDIO_TRANSCRIPTION_MODEL: str = "gpt-4o-mini-transcribe"
    # 下载远程音频文件用于转写时的大小上限，避免被诱导下载超大文件占满资源
    AUDIO_TRANSCRIPTION_MAX_DOWNLOAD_BYTES: int = 50 * 1024 * 1024

    # Image analysis tool settings
    ENABLE_IMAGE_ANALYSIS: bool = False
    # 用于图像理解/分析的视觉模型 ID
    IMAGE_ANALYSIS_MODEL_ID: str = ""
    # 调用失败时的最大重试次数与重试前的等待时间（秒）
    IMAGE_ANALYSIS_MAX_ATTEMPTS: int = 3
    IMAGE_ANALYSIS_RETRY_DELAY: float = 1.0

    # Image generation tool settings
    ENABLE_IMAGE_GENERATION: bool = False
    IMAGE_GENERATION_API_KEY: str = ""
    # 同样是兼容 OpenAI 接口协议的图像生成服务
    IMAGE_GENERATION_BASE_URL: str = "https://api.openai.com/v1"
    IMAGE_GENERATION_MODEL: str = "gpt-image-2"
    # 图像生成耗时通常明显长于文本生成，需要更宽松的超时时间
    IMAGE_GENERATION_TIMEOUT: int = 120

    # 三个 PrivateAttr 不是 pydantic 字段：不会被环境变量/数据库覆盖，也不会出现在
    # 序列化结果（如 model_dump()）里，纯粹是运行时内部状态标记。它们在下面的
    # __init__ 中，当对应的密钥因缺失/是占位符而被"自动生成"时置为 True；
    # service.py 的 _mark_runtime_secret_as_explicit() 在该值后续被数据库中的
    # 显式配置覆盖后会将其重置为 False；src/infra/distributed_validation.py
    # 在多副本部署下检查这些标志——若为 True，说明该密钥是本进程随机生成的，
    # 其它副本进程生成的值必然不同，会导致跨副本 JWT 校验/解密失败，因此会在
    # 启动时报错拦截，要求管理员显式配置一份所有副本共享的值
    _jwt_secret_key_generated: bool = PrivateAttr(False)
    _mcp_encryption_salt_generated: bool = PrivateAttr(False)
    _vapid_keys_generated: bool = PrivateAttr(False)

    # env_file 用前面导入的 PROJECT_ROOT 拼出项目根目录下 .env 的绝对路径，
    # 不依赖进程的当前工作目录，保证无论从哪里启动都能找到同一份 .env；
    # extra="ignore" 表示 .env/环境变量中出现的、Settings 类没有声明对应字段的
    # 多余配置项会被静默忽略而不是报错（兼容历史遗留或第三方注入的环境变量）
    model_config = {
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    def __init__(self, **kwargs: Any) -> None:
        """在 pydantic 完成常规的字段解析/校验（含从 env/.env 读取覆盖值）之后，
        额外执行一系列"启动期一次性"的收尾处理：为缺失或不安全的
        JWT_SECRET_KEY/MCP_ENCRYPTION_SALT 生成或确定性扩展安全值、
        缺失时自动生成 VAPID Web Push 密钥对、回填 git 版本信息、
        把 LangSmith 相关配置同步进 os.environ。
        配合文件末尾的 lru_cache 单例模式，这些副作用在进程生命周期内只会执行一次。
        """
        # 先让 pydantic 按正常流程完成字段解析、类型转换与校验，
        # 下面的自定义逻辑都建立在"字段已经有初步取值"的基础上做进一步修正
        super().__init__(**kwargs)

        # Generate random JWT_SECRET_KEY if not set or using placeholder
        # 未配置或仍是仓库示例中的占位字符串，两种情况都视为"不安全"，
        # 用密码学安全的随机数重新生成一份，并标记 _jwt_secret_key_generated=True
        # （用于后续分布式部署下的安全检查，见上方 PrivateAttr 字段的说明）
        if not self.JWT_SECRET_KEY or self.JWT_SECRET_KEY == "your-secret-key-change-in-production":
            self.JWT_SECRET_KEY = secrets.token_urlsafe(32)
            self._jwt_secret_key_generated = True
            logger.warning(
                "JWT_SECRET_KEY not set or using placeholder value. "
                f"Generated random secret key: {self.JWT_SECRET_KEY[:8]}..."
            )
        # Expand short JWT_SECRET_KEY to meet minimum length requirement
        # 长度不达标但并非缺失时，不是简单丢弃重新生成，而是用确定性扩展
        # （expand_jwt_secret_key，内部对同一个短 key 反复哈希）——同一个短 key
        # 每次启动都会扩展出同一个长 key，不会导致已签发的 token 在重启后失效
        elif len(self.JWT_SECRET_KEY) < JWT_SECRET_KEY_MIN_LENGTH:
            original_key = self.JWT_SECRET_KEY
            self.JWT_SECRET_KEY = expand_jwt_secret_key(self.JWT_SECRET_KEY)
            logger.warning(
                f"JWT_SECRET_KEY too short ({len(original_key)} bytes). "
                f"Expanded to meet minimum {JWT_SECRET_KEY_MIN_LENGTH} bytes requirement. "
                f"Expanded key prefix: {self.JWT_SECRET_KEY[:8]}..."
            )

        # Generate random MCP_ENCRYPTION_SALT if not set
        # 逻辑与上面的 JWT_SECRET_KEY 一致，这个盐值用于加密存储在数据库里的
        # MCP server 敏感配置（如第三方服务的 API Key）；缺失时随机生成，
        # 并标记 _mcp_encryption_salt_generated=True
        if not self.MCP_ENCRYPTION_SALT:
            self.MCP_ENCRYPTION_SALT = secrets.token_urlsafe(16)
            self._mcp_encryption_salt_generated = True
            logger.info("MCP_ENCRYPTION_SALT not set, generated random salt")
        # Expand short MCP_ENCRYPTION_SALT to meet minimum length requirement
        # 同样用确定性扩展而不是重新生成：同一个短 salt 每次启动扩展结果一致，
        # 否则重启后旧数据库里用旧 salt 加密的内容将无法解密
        elif len(self.MCP_ENCRYPTION_SALT) < MCP_ENCRYPTION_SALT_MIN_LENGTH:
            original_salt = self.MCP_ENCRYPTION_SALT
            self.MCP_ENCRYPTION_SALT = expand_encryption_salt(self.MCP_ENCRYPTION_SALT)
            logger.warning(
                f"MCP_ENCRYPTION_SALT too short ({len(original_salt)} bytes). "
                f"Expanded to meet minimum {MCP_ENCRYPTION_SALT_MIN_LENGTH} bytes requirement. "
                f"Expanded salt prefix: {self.MCP_ENCRYPTION_SALT[:8]}..."
            )

        # Auto-generate VAPID keys for Web Push if not configured
        # 只有公钥和私钥都为空时才自动生成；只要其中一个非空就认为已经显式配置，
        # 由使用方自行保证成对有效，这里不做"只补齐缺的一半"的处理
        if not self.VAPID_PUBLIC_KEY and not self.VAPID_PRIVATE_KEY:
            try:
                # 使用 ECDSA P-256（SECP256R1）曲线生成密钥对，这是 Web Push
                # VAPID 协议要求使用的曲线
                from cryptography.hazmat.primitives.asymmetric import ec
                from cryptography.hazmat.primitives.serialization import (
                    Encoding,
                    NoEncryption,
                    PrivateFormat,
                    PublicFormat,
                )

                private_key = ec.generate_private_key(ec.SECP256R1())
                # pywebpush expects base64url-encoded DER of PKCS8 private key
                priv_der = private_key.private_bytes(
                    Encoding.DER, PrivateFormat.PKCS8, NoEncryption()
                )
                # Browsers expect the VAPID public key as an uncompressed P-256
                # point (0x04 + X + Y), base64url encoded.
                pub_raw = private_key.public_key().public_bytes(
                    Encoding.X962, PublicFormat.UncompressedPoint
                )
                import base64

                self.VAPID_PRIVATE_KEY = base64.urlsafe_b64encode(priv_der).decode()
                self.VAPID_PUBLIC_KEY = base64.urlsafe_b64encode(pub_raw).decode()
                # 标记为"本次启动自动生成"；service.py 的 initialize_settings()
                # 会在拿到数据库连接后把这对新密钥持久化并将标志重置为 False，
                # 否则下次重启会生成不同的密钥对，导致所有已订阅推送的浏览器全部失效
                self._vapid_keys_generated = True
                logger.info(
                    "VAPID keys not configured, auto-generated ECDSA P-256 key pair. "
                    "Keys will be persisted to database on startup."
                )
            except Exception as e:
                # 生成失败（如 cryptography 库异常）不应阻止整个应用启动，
                # 因此只记录警告；后果是 Web Push 功能在没有有效密钥期间不可用，
                # 但不影响其它功能正常运行
                logger.warning("Failed to auto-generate VAPID keys: %s", e)

        # Set version info from git (if not already set via env)
        # 只在字段仍为 None（即没有被环境变量显式设置）时才回填：utils.py 模块
        # 加载时已经通过 git 命令取到 GIT_TAG/COMMIT_HASH（见 utils.py 中
        # `GIT_TAG, COMMIT_HASH = get_git_info()`），环境变量的显式配置始终优先
        if self.GIT_TAG is None:
            self.GIT_TAG = GIT_TAG
        if self.COMMIT_HASH is None:
            self.COMMIT_HASH = COMMIT_HASH
        if self.BUILD_TIME is None:
            self.BUILD_TIME = os.environ.get("BUILD_TIME")

        # Sync LangSmith settings to os.environ (required by langsmith SDK)
        # LangSmith 官方 SDK 是直接读取 os.environ 判断是否开启追踪、使用哪个
        # API Key/项目，并不接受在代码里显式传参配置，因此必须把 pydantic 字段的值
        # 反向写回环境变量才能让 SDK 真正生效；只有真值/非空的字段才写入，
        # 避免用空值覆盖掉进程启动前可能已经存在的同名环境变量
        if self.LANGSMITH_TRACING:
            os.environ["LANGSMITH_TRACING"] = "true"
        if self.LANGSMITH_API_KEY:
            os.environ["LANGSMITH_API_KEY"] = self.LANGSMITH_API_KEY
        if self.LANGSMITH_PROJECT:
            os.environ["LANGSMITH_PROJECT"] = self.LANGSMITH_PROJECT
        if self.LANGSMITH_API_URL:
            os.environ["LANGSMITH_API_URL"] = self.LANGSMITH_API_URL
        if self.LANGSMITH_SAMPLE_RATE:
            os.environ["LANGSMITH_SAMPLE_RATE"] = str(self.LANGSMITH_SAMPLE_RATE)

    # mode="before"：在 pydantic 把值转换成 bool 类型之前先拦截原始输入。
    # 如果不用 mode="before"（即默认的 mode="after"），pydantic 会先尝试自己把
    # 字符串转换成 bool，像 "release"/"prod" 这种既不是 "true"/"false"
    # 也不是 "1"/"0" 的自定义取值会直接校验失败，根本走不到这个函数里
    @field_validator("DEBUG", mode="before")
    @classmethod
    def _normalize_debug_mode(cls, value: Any) -> Any:
        """允许 DEBUG 环境变量除了标准布尔字符串外，还可以直接写运行环境名称
        （如 release/prod/production 表示关闭调试，debug/dev/development 表示开启），
        方便部署时复用同一个环境变量表达"当前是什么环境"的语义。
        """
        # 非字符串输入（比如已经是 bool，或 pydantic-settings 从其它来源解析出的值）
        # 原样返回，交给 pydantic 后续默认的 bool 转换逻辑处理
        if not isinstance(value, str):
            return value

        normalized = value.strip().lower()
        if normalized in {"release", "prod", "production"}:
            return False
        if normalized in {"debug", "dev", "development"}:
            return True
        # 不匹配任何已知环境名称时，原样返回给 pydantic 按标准布尔字符串规则处理
        # （如 "true"/"false"/"1"/"0" 等），无法识别的取值会在那一步报错
        return value

    def get_s3_config(self) -> "S3Config":
        """Get S3 storage configuration."""
        # 延迟导入：S3Config/S3Provider 属于 infra 存储层，这里不放在模块顶层 import，
        # 避免 kernel.config 在被最早加载时就必须依赖 infra.storage.s3
        # （同时也呼应文件顶部 TYPE_CHECKING 下只导入类型注解用的 S3Config 的做法）
        from src.infra.storage.s3 import S3Config, S3Provider

        # S3_PROVIDER 配置项存的是自由格式的字符串（见 definitions.py），
        # 这里统一转小写后映射到强类型的 S3Provider 枚举；匹配不到任何已知厂商时
        # 默认按 AWS 的接口协议处理（大多数兼容 S3 协议的厂商与 AWS 语义一致）
        provider_map = {
            "aws": S3Provider.AWS,
            "aliyun": S3Provider.ALIYUN,
            "tencent": S3Provider.TENCENT,
            "minio": S3Provider.MINIO,
            "custom": S3Provider.CUSTOM,
            "local": S3Provider.LOCAL,
        }
        provider = provider_map.get(self.S3_PROVIDER.lower(), S3Provider.AWS)

        # 把本类里分散的 S3_* 字段重新组装成 infra 层 S3Config 所需的强类型对象，
        # 使 kernel（配置）层与 infra（存储实现）层的字段命名可以独立演进，
        # 不要求两边字段名一一对应
        return S3Config(
            provider=provider,
            endpoint_url=self.S3_ENDPOINT_URL,
            access_key=self.S3_ACCESS_KEY,
            secret_key=self.S3_SECRET_KEY,
            region=self.S3_REGION,
            bucket_name=self.S3_BUCKET_NAME,
            custom_domain=self.S3_CUSTOM_DOMAIN,
            path_style=self.S3_PATH_STYLE,
            max_file_size=self.S3_MAX_FILE_SIZE,
            internal_max_upload_size=self.S3_INTERNAL_UPLOAD_MAX_SIZE,
            presigned_url_expires=self.S3_PRESIGNED_URL_EXPIRES,
            storage_path=self.LOCAL_STORAGE_PATH,
        )

    @property
    def postgres_url(self) -> str:
        """Construct PostgreSQL connection URL from components."""
        # 主业务数据库（用户、会话等长期存储）的连接串，各组成部分直接来自对应的
        # POSTGRES_* 字段，不做任何回退处理
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def checkpoint_postgres_url(self) -> str:
        """Construct checkpoint PostgreSQL connection URL. Falls back to shared POSTGRES_* when CHECKPOINT_PG_HOST is empty."""
        # 允许 checkpoint（LangGraph 对话状态持久化）单独配置一套独立的 Postgres
        # 实例；host/user/password/db 四项若各自留空，都会独立回退到共享的
        # POSTGRES_* 配置，四者的回退与否互不影响（比如可以只单独指定
        # CHECKPOINT_PG_USER，其它三项继续复用共享数据库的配置）
        host = self.CHECKPOINT_PG_HOST or self.POSTGRES_HOST
        # 注意：port 是个例外，并不会回退到 POSTGRES_PORT，而是直接使用
        # CHECKPOINT_PG_PORT 自身的默认值——这是与上面四个字段不一致的地方，
        # 如果共享 Postgres 使用了非默认端口、又没有显式设置 CHECKPOINT_PG_PORT，
        # 拼出的连接串端口可能与 host 实际监听的端口不一致，需要注意
        port = self.CHECKPOINT_PG_PORT
        user = self.CHECKPOINT_PG_USER or self.POSTGRES_USER
        password = self.CHECKPOINT_PG_PASSWORD or self.POSTGRES_PASSWORD
        db = self.CHECKPOINT_PG_DB or self.POSTGRES_DB
        return f"postgresql://{user}:{password}@{host}:{port}/{db}"


# lru_cache 装饰一个无参函数，等效实现单例模式：第一次调用真正执行 Settings()
# 完成 env/.env 解析与上面 __init__ 里的收尾处理，后续所有调用直接返回同一个缓存实例，
# 不会重复读取环境变量或重复生成随机密钥
@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Global settings instance
# 模块加载时立即求值一次，得到的是全局唯一、可在整个进程内共享的 Settings 实例；
# 其它模块普遍通过 `from src.kernel.config import settings` 拿到这个同一个对象，
# service.py 等模块通过 setattr(settings, ...) 原地修改它的属性即可让所有引用方
# 立刻感知到配置变化，无需重新导入或重启进程
settings = get_settings()

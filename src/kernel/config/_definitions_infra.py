"""Infrastructure setting definitions: MongoDB, Redis, Task Backend, LangSmith Tracing."""

# 启用延迟注解求值（PEP 563），类型注解以字符串形式保存，不在模块加载时立即求值
from __future__ import annotations

# 导入配置分类枚举 SettingCategory 与配置取值类型枚举 SettingType，供下方各配置项的 category/type 字段使用
from src.kernel.schemas.setting import SettingCategory, SettingType

# 基础设施类配置项字典：MongoDB 存储、Redis 缓存与基于 Redis 的 ARQ 分布式任务队列、
# 以及 LangSmith 链路追踪这三大块配置的元数据都集中在这里声明。
# 本字典最终会在 src/kernel/config/definitions.py 中通过 **INFRA_SETTING_DEFINITIONS
# 展开合并进全局的 SETTING_DEFINITIONS 字典（配置元数据的唯一权威来源），每一项会被
# 转换成一个 SettingItem（定义见 src/kernel/schemas/setting.py），用于驱动后台管理
# 设置页面的展示。
#
# 每个配置项 value 字典里常见字段说明（下面各配置项不再重复解释字段本身，只说明该项
# 具体用途）：
#   - type：取值类型，对应 SettingType 枚举（STRING/NUMBER/BOOLEAN/SELECT），决定
#     前端渲染成什么控件、值如何做类型转换；本文件中 TASK_BACKEND 用的是 SELECT
#     类型，需配合 options 字段给出可选项列表。
#   - category / subcategory：所属分类/子分类，用于设置页面分组展示。
#   - description：是一个 i18n 文案 key（形如 settingDesc.XXX），并非直接展示的
#     文本，前端会拿这个 key 去查多语言翻译表，不要误解成实际文案内容。
#   - default：默认值，数据库/环境变量都没有覆盖时的兜底值；真实运行时的类型化
#     默认值同时写在 src/kernel/config/base.py 的 Pydantic Settings 类里，两处
#     应保持一致。
#   - is_sensitive：标记为敏感信息（连接串/密码/API Key 等），API 返回和日志中
#     会被打码/隐藏。
#   - depends_on：控制该配置项在设置界面上的“条件显示”。字符串值表示“只有当这个
#     字符串对应的父配置项（通常是布尔开关）为真时才显示”；{"key": ..., "value": ...}
#     字典形式表示“只有当父配置项的值等于指定值时才显示”。本文件里所有 ARQ 相关
#     配置都依赖 TASK_BACKEND 这个 SELECT 配置的取值是否为 "arq"，用的正是字典形式。
#   - frontend_visible：是否在前端设置页面上直接可见，不写默认为 False（隐藏/仅
#     内部使用）；本文件中 MONGODB_STORE_BATCH_CONCURRENCY 与
#     TASK_STARTUP_CLEANUP_CONCURRENCY 显式设为 False，表示它们是纯内部调优参数，
#     不打算暴露给管理员在界面上修改。
INFRA_SETTING_DEFINITIONS: dict[str, dict] = {
    # ============================================
    # MongoDB Settings
    # ============================================
    # 本节：MongoDB 连接与鉴权参数，以及使用 MongoDB 作为 Store 时批量操作的并发度调优参数。
    # MONGODB_URL：MongoDB 连接串（不含账号密码），默认指向本机默认端口；若下方
    # MONGODB_USERNAME/MONGODB_PASSWORD 均非空，运行时会自动将其拼接进连接串并
    # 附加 authSource（见 src/infra/storage/mongodb.py），因此这里标记为敏感信息。
    "MONGODB_URL": {
        "type": SettingType.STRING,
        "category": SettingCategory.MONGODB,
        "subcategory": "connection",
        "description": "settingDesc.MONGODB_URL",
        "default": "mongodb://localhost:27017",
        "is_sensitive": True,
    },
    # MONGODB_DB：实际使用的数据库名，存放会话、Trace、长期记忆等 Agent 运行状态数据。
    "MONGODB_DB": {
        "type": SettingType.STRING,
        "category": SettingCategory.MONGODB,
        "subcategory": "connection",
        "description": "settingDesc.MONGODB_DB",
        "default": "agent_state",
    },
    # MONGODB_USERNAME：MongoDB 认证用户名，默认为空表示不启用用户名密码认证。
    "MONGODB_USERNAME": {
        "type": SettingType.STRING,
        "category": SettingCategory.MONGODB,
        "subcategory": "connection",
        "description": "settingDesc.MONGODB_USERNAME",
        "default": "",
    },
    # MONGODB_PASSWORD：MongoDB 认证密码，默认为空；is_sensitive=True，管理后台
    # 展示与日志中会被脱敏。
    "MONGODB_PASSWORD": {
        "type": SettingType.STRING,
        "category": SettingCategory.MONGODB,
        "subcategory": "connection",
        "description": "settingDesc.MONGODB_PASSWORD",
        "default": "",
        "is_sensitive": True,
    },
    # MONGODB_AUTH_SOURCE：进行账号密码认证时使用的认证数据库（对应连接串中的
    # authSource 参数），默认 admin（MongoDB 默认的管理/认证数据库）。
    "MONGODB_AUTH_SOURCE": {
        "type": SettingType.STRING,
        "category": SettingCategory.MONGODB,
        "subcategory": "connection",
        "description": "settingDesc.MONGODB_AUTH_SOURCE",
        "default": "admin",
    },
    # MONGODB_STORE_BATCH_CONCURRENCY：LangGraph Store 异步批量接口 abatch() 在
    # 一批 Get/Put/Search/ListNamespaces 混合操作中并发访问 MongoDB 的协程数上限
    # （实际并发取该值与本批操作条数的较小者），同步 batch() 逐条执行不受此项影响；
    # 调大可提升批量吞吐，但会加重 MongoDB 连接池/负载压力，默认 16；
    # frontend_visible=False 表示这是纯内部调优参数，不在前端设置页面展示。
    "MONGODB_STORE_BATCH_CONCURRENCY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MONGODB,
        "subcategory": "performance",
        "description": "settingDesc.MONGODB_STORE_BATCH_CONCURRENCY",
        "default": 16,
        "frontend_visible": False,
    },
    # ============================================
    # Redis Settings
    # ============================================
    # 本节：Redis 连接参数，以及任务执行后端（本地直接执行 / 基于 Redis 的 ARQ 分布式
    # 队列）选型开关及 ARQ worker 相关参数。
    # REDIS_URL：Redis 连接串，默认指向本机默认端口的 0 号库；连接串中可以内嵌密码，
    # 但下方 REDIS_PASSWORD 一旦显式配置会优先于其生效（参见 arq_settings.py、
    # redis.py），因此这里标记为敏感信息。
    "REDIS_URL": {
        "type": SettingType.STRING,
        "category": SettingCategory.REDIS,
        "subcategory": "connection",
        "description": "settingDesc.REDIS_URL",
        "default": "redis://localhost:6379/0",
        "is_sensitive": True,
    },
    # REDIS_PASSWORD：Redis 认证密码，默认为空；显式配置时优先于 REDIS_URL 中
    # 内嵌的密码生效。
    "REDIS_PASSWORD": {
        "type": SettingType.STRING,
        "category": SettingCategory.REDIS,
        "subcategory": "connection",
        "description": "settingDesc.REDIS_PASSWORD",
        "default": "",
        "is_sensitive": True,
    },
    # TASK_BACKEND：后台任务（新对话运行、定时任务、崩溃恢复重放等）的执行方式，
    # SELECT 类型配合 options 给出两个可选值：local 表示在处理请求的当前进程内
    # 直接以 asyncio task 执行，无需额外部署 worker；arq 表示将任务序列化后推入
    # Redis 队列，由 ARQ worker（可内嵌或独立进程部署）异步取出执行，支持多进程/
    # 多机水平扩展；默认 arq。下方所有 ARQ_* 配置均通过 depends_on 依赖本项取值
    # 为 "arq" 时才在设置页面显示。
    "TASK_BACKEND": {
        "type": SettingType.SELECT,
        "category": SettingCategory.REDIS,
        "subcategory": "task",
        "description": "settingDesc.TASK_BACKEND",
        "default": "arq",
        "options": ["local", "arq"],
    },
    # ARQ_EMBEDDED_WORKER：TASK_BACKEND 为 arq 时，是否在 API 进程启动阶段额外
    # 内嵌启动一个 ARQ worker 协程消费队列（见 src/infra/task/arq_runtime.py）；
    # True（默认）表示无需单独部署 worker 进程即可工作，适合单机部署；多机部署且
    # 希望任务执行与 API 服务分开水平扩展时，可关闭本项，改由独立部署的 worker
    # 进程消费队列。
    "ARQ_EMBEDDED_WORKER": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.REDIS,
        "subcategory": "task",
        "description": "settingDesc.ARQ_EMBEDDED_WORKER",
        "default": True,
        "depends_on": {"key": "TASK_BACKEND", "value": "arq"},
    },
    # ARQ_QUEUE_NAME：ARQ 在 Redis 中使用的队列名（命名空间），提交任务的连接池
    # 与消费任务的 worker 必须使用相同的队列名才能对上；默认 "lambchat:arq"。
    "ARQ_QUEUE_NAME": {
        "type": SettingType.STRING,
        "category": SettingCategory.REDIS,
        "subcategory": "task",
        "description": "settingDesc.ARQ_QUEUE_NAME",
        "default": "lambchat:arq",
        "depends_on": {"key": "TASK_BACKEND", "value": "arq"},
    },
    # ARQ_WORKER_MAX_JOBS：单个 ARQ worker 进程内可同时并发执行的任务数上限
    # （对应 arq 的 max_jobs 参数，内部通过信号量控制，并非队列容量或任务总数
    # 上限）；调大可提高单 worker 吞吐，但会增加内存/连接等资源占用，默认 128。
    "ARQ_WORKER_MAX_JOBS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.REDIS,
        "subcategory": "task",
        "description": "settingDesc.ARQ_WORKER_MAX_JOBS",
        "default": 128,
        "depends_on": {"key": "TASK_BACKEND", "value": "arq"},
    },
    # ARQ_JOB_TIMEOUT_SECONDS：单个任务允许运行的最长时间（秒），超时后 ARQ 会
    # 取消该任务对应的协程并将其标记为失败，只影响这一个任务，不会连带终止整个
    # worker 进程；默认 86400 秒（24 小时），用于适配长时间运行的 Agent 任务。
    "ARQ_JOB_TIMEOUT_SECONDS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.REDIS,
        "subcategory": "task",
        "description": "settingDesc.ARQ_JOB_TIMEOUT_SECONDS",
        "default": 86400,
        "depends_on": {"key": "TASK_BACKEND", "value": "arq"},
    },
    # TASK_STARTUP_CLEANUP_CONCURRENCY：应用启动阶段扫描并恢复遗留/僵死任务
    # （运行中/待处理/可恢复失败的会话，以及重放排队中任务，见
    # src/infra/task/startup_cleanup.py）时的并发扇出上限，实际并发取该值与
    # 待处理条目数的较小者；调大可加快启动恢复速度，但会增加启动瞬间对
    # MongoDB/Redis 的压力，默认 16；frontend_visible=False 表示这是纯内部
    # 调优参数，不在前端设置页面展示。
    "TASK_STARTUP_CLEANUP_CONCURRENCY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.REDIS,
        "subcategory": "task",
        "description": "settingDesc.TASK_STARTUP_CLEANUP_CONCURRENCY",
        "default": 16,
        "depends_on": {"key": "TASK_BACKEND", "value": "arq"},
        "frontend_visible": False,
    },
    # ============================================
    # LangSmith Tracing Settings
    # ============================================
    # 本节：LangSmith（LangChain/LangGraph 官方链路追踪平台）相关配置，用于观测
    # Agent 运行时内部的调用链、Token 消耗等；LANGSMITH_TRACING 是总开关，其余
    # 四项均通过 depends_on 依赖它，仅当追踪开启时才在设置页面显示。
    # LANGSMITH_TRACING：是否启用 LangSmith 链路追踪，默认关闭；启用后由 base.py
    # 在启动时写入同名环境变量，供 LangChain/LangGraph SDK 读取。
    "LANGSMITH_TRACING": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.TRACING,
        "subcategory": "langsmith",
        "description": "settingDesc.LANGSMITH_TRACING",
        "default": False,
    },
    # LANGSMITH_API_KEY：LangSmith 平台的 API Key，默认为空；is_sensitive=True，
    # 管理后台展示与日志中会被脱敏。
    "LANGSMITH_API_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.TRACING,
        "subcategory": "langsmith",
        "description": "settingDesc.LANGSMITH_API_KEY",
        "default": "",
        "depends_on": "LANGSMITH_TRACING",
        "is_sensitive": True,
    },
    # LANGSMITH_PROJECT：追踪数据在 LangSmith 平台上归属的项目名，默认 "lamb-agent"。
    "LANGSMITH_PROJECT": {
        "type": SettingType.STRING,
        "category": SettingCategory.TRACING,
        "subcategory": "langsmith",
        "description": "settingDesc.LANGSMITH_PROJECT",
        "default": "lamb-agent",
        "depends_on": "LANGSMITH_TRACING",
    },
    # LANGSMITH_API_URL：LangSmith 服务的 API 地址，默认使用官方云服务地址；
    # 如自建/私有化部署了 LangSmith 后端，可改为对应地址。
    "LANGSMITH_API_URL": {
        "type": SettingType.STRING,
        "category": SettingCategory.TRACING,
        "subcategory": "langsmith",
        "description": "settingDesc.LANGSMITH_API_URL",
        "default": "https://api.smith.langchain.com",
        "depends_on": "LANGSMITH_TRACING",
    },
    # LANGSMITH_SAMPLE_RATE：追踪采样率，取值范围 0.0~1.0，表示有多大比例的调用
    # 会被上报到 LangSmith；默认 1.0 即全部上报，调用量较大时可适当调低以降低
    # 上报开销及 LangSmith 平台的存储/配额消耗。
    "LANGSMITH_SAMPLE_RATE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TRACING,
        "subcategory": "langsmith",
        "description": "settingDesc.LANGSMITH_SAMPLE_RATE",
        "default": 1.0,
        "depends_on": "LANGSMITH_TRACING",
    },
}

"""Core setting definitions: Frontend, Application, LLM, Session, Event Merger."""
# 本文件是配置系统“设置项元数据”的一个领域分片（core 分片）。
# 全局唯一的 SETTING_DEFINITIONS 由 definitions.py 汇总多个分片模块得到
# （core / sandbox / tools / infra / extra），本文件负责其中：
# 前端展示、应用基础、LLM 调用、会话（Session）与事件合并（Event Merger）相关的设置项。
# 注意：这里的字典描述的是“设置项的元数据”（类型、分类、默认值、是否敏感等），
# 而不是设置项当前的运行值——运行值保存在 MongoDB 的 system_settings 集合中，
# 数据库里没有对应记录时才会回退使用这里声明的 "default"
# （参见 src/infra/settings/storage.py 的 get_all 实现）。

# 启用 PEP 563 延迟注解求值，使下方 `dict[str, dict]` 等类型标注按字符串处理，
# 兼容尚未原生支持内置泛型下标语法的 Python 版本
from __future__ import annotations

# 从 setting.py 引入渲染/校验管理后台设置页面所需的枚举与结构定义
from src.kernel.schemas.setting import (
    # JsonSchema：type 为 JSON 的设置项用它描述内部结构，
    # 使前端可以渲染结构化表单而非裸 JSON 文本框
    JsonSchema,
    # JsonSchemaField：JsonSchema 内单个字段（如输入框/密码框/下拉框）的描述
    JsonSchemaField,
    # SettingCategory：设置项所属大类，决定其在管理后台的分组/导航位置
    SettingCategory,
    # SettingType：设置项取值类型（字符串/长文本/数字/布尔/JSON/下拉选择），
    # 决定前端渲染成什么表单控件
    SettingType,
)

# CORE_SETTING_DEFINITIONS：本分片包含的全部设置项定义。
# key 为设置项名称（同时也是数据库文档的 _id，多数情况下也与
# .env / Settings 中的字段名同名），value 为该设置项的元数据字典，常见字段含义：
#   - type：取值类型（SettingType），决定前端表单控件
#   - category / subcategory：分类/子分类，用于管理后台分组展示
#   - description：i18n key（形如 "settingDesc.XXX"），前端据此查多语言文案，
#     这里存的是 key 本身而不是文案原文
#   - default：数据库中没有管理员覆盖值时使用的回退默认值
#   - frontend_visible：是否允许非管理员（普通用户前端）读取该项，
#     未设置时默认为 False，即默认只有管理员可见（见 storage.py 中 admin_mode 过滤逻辑）
#   - is_sensitive：是否敏感信息；为 True 时接口返回会被脱敏为 "********"，
#     并会被收进 SENSITIVE_SETTINGS 供日志脱敏使用
#   - depends_on：管理后台中的可见性依赖条件；值为字符串时表示
#     “父设置项为真值时才显示”，值为 {"key":..,"value":..} 字典时表示
#     “父设置项等于该特定值时才显示”
#   - options：type 为 SELECT 时的下拉可选项列表
#   - json_schema：type 为 JSON 时，描述其结构以便前端渲染结构化编辑器
CORE_SETTING_DEFINITIONS: dict[str, dict] = {
    # ============================================
    # Frontend Settings
    # ============================================
    # 控制聊天前端默认行为与展示内容的设置：默认 Agent、默认模型、欢迎页建议问题等
    # DEFAULT_AGENT：新建会话时默认使用的 Agent 类型标识（如 "fast"）；
    # 用户未显式切换 Agent 时即采用该值，frontend_visible=True 表示普通用户前端也能读取
    "DEFAULT_AGENT": {
        "type": SettingType.STRING,
        "category": SettingCategory.FRONTEND,
        "subcategory": "display",
        "description": "settingDesc.DEFAULT_AGENT",
        "default": "fast",
        "frontend_visible": True,
    },
    # DEFAULT_MODEL_ID：新会话及后台任务（生成标题、推荐问题等）默认使用的模型配置 ID；
    # 留空（""）表示回退到系统中第一个已启用的模型
    "DEFAULT_MODEL_ID": {
        "type": SettingType.STRING,
        "category": SettingCategory.LLM,
        "subcategory": "model",
        "description": "settingDesc.DEFAULT_MODEL_ID",
        "default": "",
        "frontend_visible": True,
    },
    # WELCOME_SUGGESTIONS：欢迎页/空会话页展示给用户的“建议问题”卡片列表，
    # 按语言代码分组的多语言 JSON 对象，每种语言下是一组 {icon, text} 建议项；
    # type=JSON 配合下面的 json_schema 使前端能渲染结构化编辑器而不是裸文本框
    "WELCOME_SUGGESTIONS": {
        "type": SettingType.JSON,
        "category": SettingCategory.FRONTEND,
        "subcategory": "display",
        "description": "settingDesc.WELCOME_SUGGESTIONS",
        # default：管理员未自定义时使用的内置样例，覆盖 en/zh/ja/ko/ru 五种语言，
        # 每种语言 4 条建议，内容为通用的“你好世界”类操作演示
        "default": {
            "en": [
                {"icon": "🐍", "text": "Create a Python hello world script"},
                {"icon": "📁", "text": "List files in the workspace directory"},
                {"icon": "📄", "text": "Read the README.md file"},
                {"icon": "🔧", "text": "Help me write a shell script"},
            ],
            "zh": [
                {"icon": "🐍", "text": "创建一个 Python Hello World 脚本"},
                {"icon": "📁", "text": "列出工作区目录中的文件"},
                {"icon": "📄", "text": "读取 README.md 文件"},
                {"icon": "🔧", "text": "帮我写一个 Shell 脚本"},
            ],
            "ja": [
                {"icon": "🐍", "text": "PythonのHello Worldスクリプトを作成"},
                {
                    "icon": "📁",
                    "text": "ワークスペースディレクトリのファイルを一覧表示",
                },
                {"icon": "📄", "text": "README.mdファイルを読む"},
                {"icon": "🔧", "text": "シェルスクリプトを書くのを手伝って"},
            ],
            "ko": [
                {"icon": "🐍", "text": "Python Hello World 스크립트 만들기"},
                {"icon": "📁", "text": "작업 공간 디렉토리의 파일 목록 보기"},
                {"icon": "📄", "text": "README.md 파일 읽기"},
                {"icon": "🔧", "text": "쉘 스크립트 작성 도와줘"},
            ],
            "ru": [
                {"icon": "🐍", "text": "Создайте скрипт Python Hello World"},
                {"icon": "📁", "text": "Покажите файлы в рабочей директории"},
                {"icon": "📄", "text": "Прочитайте файл README.md"},
                {"icon": "🔧", "text": "Помогите написать скрипт оболочки"},
            ],
        },
        "frontend_visible": True,
        "json_schema": JsonSchema(
            type="object",
            key_label="settingDesc.WELCOME_SUGGESTION_LANG",
            value_type="array",
            item_label="settingDesc.WELCOME_SUGGESTION_ITEM",
            key_options=["en", "zh", "ja", "ko", "ru"],
            fields=[
                JsonSchemaField(
                    name="icon",
                    type="text",
                    label="settingDesc.WELCOME_SUGGESTION_ICON",
                    placeholder="🐍",
                    required=True,
                    layout_width="compact",
                ),
                JsonSchemaField(
                    name="text",
                    type="text",
                    label="settingDesc.WELCOME_SUGGESTION_TEXT",
                    placeholder="...",
                    required=True,
                    layout_width="full",
                ),
            ],
        ),
    },
    # ============================================
    # Email Service Settings (Resend)
    # ============================================
    # 基于 Resend（https://resend.com）的邮件发送账户配置，以及管理员联系方式展示
    # RESEND_ACCOUNTS：Resend 发信账户列表（支持配置多个账户，例如按用量/额度轮询或
    # 分渠道发送），type=JSON 数组；depends_on=EMAIL_ENABLED 表示仅当邮件服务总开关
    # 打开时才在管理后台显示；default 为空列表，表示未配置时不发送邮件
    "RESEND_ACCOUNTS": {
        "type": SettingType.JSON,
        "category": SettingCategory.EMAIL,
        "subcategory": "service",
        "description": "settingDesc.RESEND_ACCOUNTS",
        "default": [],
        "depends_on": "EMAIL_ENABLED",
        "frontend_visible": True,
        # json_schema：数组类型，每一项（一个 Resend 账户）包含以下三个字段
        "json_schema": JsonSchema(
            type="array",
            item_label="settingDesc.RESEND_ACCOUNT_ITEM",
            fields=[
                # api_key：Resend 的 API Key（re_ 开头），密码框展示，必填
                JsonSchemaField(
                    name="api_key",
                    type="password",
                    label="settingDesc.RESEND_ACCOUNT_API_KEY",
                    placeholder="re_xxxxxxxx",
                    required=True,
                ),
                # email_from：发件人邮箱地址，必填
                JsonSchemaField(
                    name="email_from",
                    type="text",
                    label="settingDesc.RESEND_ACCOUNT_EMAIL_FROM",
                    placeholder="noreply@example.com",
                    required=True,
                ),
                # email_from_name：发件人显示名称，选填
                JsonSchemaField(
                    name="email_from_name",
                    type="text",
                    label="settingDesc.RESEND_ACCOUNT_EMAIL_FROM_NAME",
                    placeholder="LambChat",
                ),
            ],
        ),
    },
    # ADMIN_CONTACT_EMAIL：展示在前端“关于”对话框中的管理员联系邮箱，便于用户反馈问题
    "ADMIN_CONTACT_EMAIL": {
        "type": SettingType.STRING,
        "category": SettingCategory.FRONTEND,
        "subcategory": "contact",
        "description": "settingDesc.ADMIN_CONTACT_EMAIL",
        "default": "",
        "frontend_visible": True,
    },
    # ADMIN_CONTACT_URL：展示在前端“关于”对话框中的管理员联系/支持链接（如工单系统、反馈表单）
    "ADMIN_CONTACT_URL": {
        "type": SettingType.STRING,
        "category": SettingCategory.FRONTEND,
        "subcategory": "contact",
        "description": "settingDesc.ADMIN_CONTACT_URL",
        "default": "",
        "frontend_visible": True,
    },
    # ============================================
    # Application Settings
    # ============================================
    # 应用级通用运行参数：外部可访问的基础 URL、调试模式、日志级别
    # APP_BASE_URL：文件上传/下载等场景生成外链时使用的公共基础 URL
    # （如 https://lambchat.example.com）；当服务部署在反向代理之后，
    # 导致框架从请求推断出的 base_url 不准确时，需要在此显式配置；留空则按请求推断
    "APP_BASE_URL": {
        "type": SettingType.STRING,
        "category": SettingCategory.AGENT,
        "subcategory": "general",
        "description": "settingDesc.APP_BASE_URL",
        "default": "",
        "frontend_visible": True,
    },
    # DEBUG：调试模式开关；开启后可能输出更详细的日志/异常堆栈，生产环境建议保持关闭
    "DEBUG": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.AGENT,
        "subcategory": "general",
        "description": "settingDesc.DEBUG",
        "default": False,
    },
    # LOG_LEVEL：应用日志级别（DEBUG/INFO/WARNING/ERROR），级别越低输出越详细
    "LOG_LEVEL": {
        "type": SettingType.STRING,
        "category": SettingCategory.AGENT,
        "subcategory": "general",
        "description": "settingDesc.LOG_LEVEL",
        "default": "INFO",
    },
    # ============================================
    # LLM Settings
    # ============================================
    # 调用大模型 API 时的重试策略与两类缓存（模型实例缓存、Prompt Caching 缓存）参数
    # LLM_MAX_RETRIES：调用 LLM API 失败（如遇到 429 限流、网络抖动）时的最大重试次数
    "LLM_MAX_RETRIES": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.LLM,
        "subcategory": "retry",
        "description": "settingDesc.LLM_MAX_RETRIES",
        "default": 3,
    },
    # LLM_RETRY_DELAY：重试的基础等待时间（秒），通常与指数退避（exponential backoff）
    # 策略配合，作为第一次重试前的等待起始值
    "LLM_RETRY_DELAY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.LLM,
        "subcategory": "retry",
        "description": "settingDesc.LLM_RETRY_DELAY",
        "default": 1.0,
    },
    # LLM_MODEL_CACHE_SIZE：LLM 模型客户端实例的缓存上限；每个实例约占 10-30MB 内存，
    # 用于避免多用户/多参数组合下重复创建实例的开销；设置过小会频繁创建/销毁实例，
    # 设置过大则占用更多内存
    "LLM_MODEL_CACHE_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.LLM,
        "subcategory": "cache",
        "description": "settingDesc.LLM_MODEL_CACHE_SIZE",
        "default": 50,
    },
    # PROMPT_CACHE_MAX_SYSTEM_BLOCKS：Prompt Caching（模型侧提示词缓存）机制中，
    # 系统提示词（system prompt）最多允许缓存的块数
    "PROMPT_CACHE_MAX_SYSTEM_BLOCKS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.LLM,
        "subcategory": "cache",
        "description": "settingDesc.PROMPT_CACHE_MAX_SYSTEM_BLOCKS",
        "default": 4,
    },
    # PROMPT_CACHE_MAX_TOOLS：Prompt Caching 机制中，工具（tool）定义最多允许缓存的数量
    "PROMPT_CACHE_MAX_TOOLS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.LLM,
        "subcategory": "cache",
        "description": "settingDesc.PROMPT_CACHE_MAX_TOOLS",
        "default": 1,
    },
    # ============================================
    # Session Settings
    # ============================================
    # 会话（Session/对话）运行时行为、会话事件（Event）存储与回放相关的设置。
    # 其中 SESSION_EVENT_* 系列管理会话事件在 MongoDB / Redis 中的缓冲、分片与回放策略
    # SESSION_MAX_RUNS_PER_SESSION：单个会话允许执行的最大“运行”（一次 Agent 完整执行）
    # 次数，超过后通常需要新建会话，避免单个会话无限增长
    "SESSION_MAX_RUNS_PER_SESSION": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "general",
        "description": "settingDesc.SESSION_MAX_RUNS_PER_SESSION",
        "default": 1000,
    },
    # ENABLE_MESSAGE_HISTORY：是否持久化消息历史记录
    "ENABLE_MESSAGE_HISTORY": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SESSION,
        "subcategory": "general",
        "description": "settingDesc.ENABLE_MESSAGE_HISTORY",
        "default": True,
    },
    # SSE_CACHE_TTL：SSE（Server-Sent Events，流式推送）事件在 Redis 中缓存的过期时间
    # （秒），默认 86400 秒（1 天），用于客户端断线重连后回放期间未接收到的事件
    "SSE_CACHE_TTL": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "general",
        "description": "settingDesc.SSE_CACHE_TTL",
        "default": 86400,
    },
    # SESSION_EVENT_MONGO_BUFFER_MAX：会话事件写入 MongoDB 前，内存缓冲区可积压的
    # 最大事件条数，超过后需要落库或触发限流
    "SESSION_EVENT_MONGO_BUFFER_MAX": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.SESSION_EVENT_MONGO_BUFFER_MAX",
        "default": 10000,
    },
    # SESSION_EVENT_READ_DEFAULT_LIMIT：查询会话事件接口未显式指定 limit 参数时，
    # 默认返回的事件条数
    "SESSION_EVENT_READ_DEFAULT_LIMIT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.SESSION_EVENT_READ_DEFAULT_LIMIT",
        "default": 1000,
    },
    # SESSION_EVENT_TTL_CACHE_MAX：会话事件相关 TTL 缓存的最大容量（条目数上限）
    "SESSION_EVENT_TTL_CACHE_MAX": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.SESSION_EVENT_TTL_CACHE_MAX",
        "default": 5000,
    },
    # SESSION_EVENT_REDIS_REPLAY_BATCH_SIZE：从 Redis 回放历史会话事件时，
    # 单批次读取处理的事件数量
    "SESSION_EVENT_REDIS_REPLAY_BATCH_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.SESSION_EVENT_REDIS_REPLAY_BATCH_SIZE",
        "default": 500,
    },
    # SESSION_EVENT_CHUNK_STORAGE_ENABLED：是否启用“分片存储”——开启后，新产生的
    # trace 事件不再无限追加进单个大文档的 events 数组，而是拆分保存到独立的
    # chunk 文档中（每个 chunk 大小见 SESSION_EVENT_CHUNK_SIZE），用于规避
    # MongoDB 单文档 16MB 大小限制
    "SESSION_EVENT_CHUNK_STORAGE_ENABLED": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.SESSION_EVENT_CHUNK_STORAGE_ENABLED",
        "default": False,
    },
    # SESSION_EVENT_CHUNK_DUAL_WRITE_LEGACY：启用分片存储后，是否同时按旧方式把事件
    # 也写入旧的 events 数组（双写），用于迁移/兼容过渡期间不丢数据；
    # 迁移完成、确认新方案稳定后可关闭以节省存储空间和写入开销
    "SESSION_EVENT_CHUNK_DUAL_WRITE_LEGACY": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.SESSION_EVENT_CHUNK_DUAL_WRITE_LEGACY",
        "default": False,
    },
    # SESSION_EVENT_CHUNK_SIZE：启用分片存储时，单个 chunk 文档最多保存的事件数，
    # 超过后新开一个 chunk 文档继续写入
    "SESSION_EVENT_CHUNK_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.SESSION_EVENT_CHUNK_SIZE",
        "default": 5000,
    },
    # FEISHU_UPLOAD_BYTES_MAX_SIZE：飞书（Feishu/Lark）文件上传接口允许的最大字节数，
    # 默认 20971520 字节（20MB）；frontend_visible=False 表示仅管理员可见/可改
    "FEISHU_UPLOAD_BYTES_MAX_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.FILE_UPLOAD,
        "subcategory": "feishu",
        "description": "settingDesc.FEISHU_UPLOAD_BYTES_MAX_SIZE",
        "default": 20971520,
        "frontend_visible": False,
    },
    # SESSION_SEARCH_BACKFILL_STARTUP_DELAY_SECONDS：服务启动完成后，延迟多少秒再
    # 执行“会话搜索索引回填”任务，用于避开启动阶段与其它初始化任务抢占资源
    "SESSION_SEARCH_BACKFILL_STARTUP_DELAY_SECONDS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "general",
        "description": "settingDesc.SESSION_SEARCH_BACKFILL_STARTUP_DELAY_SECONDS",
        "default": 30.0,
    },
    # 会话标题自动生成 与 回答后推荐追问 相关的模型配置与提示词模板
    # SESSION_TITLE_MODEL：生成会话标题所使用的模型配置 ID；留空则回退到
    # DEFAULT_MODEL_ID 指向的全局默认模型
    "SESSION_TITLE_MODEL": {
        "type": SettingType.STRING,
        "category": SettingCategory.SESSION,
        "subcategory": "title",
        "description": "settingDesc.SESSION_TITLE_MODEL",
        "default": "",
    },
    # SESSION_TITLE_API_BASE：历史遗留字段，仅为兼容旧配置保留——
    # 标题生成实际使用的 provider/API 地址现在统一从 SESSION_TITLE_MODEL
    # 指向的模型配置中读取，此字段不再生效
    "SESSION_TITLE_API_BASE": {
        "type": SettingType.STRING,
        "category": SettingCategory.SESSION,
        "subcategory": "title",
        "description": "settingDesc.SESSION_TITLE_API_BASE",
        "default": "",
    },
    # SESSION_TITLE_API_KEY：历史遗留字段，仅为兼容旧配置保留，语义同上；
    # is_sensitive=True，接口返回时会被脱敏
    "SESSION_TITLE_API_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.SESSION,
        "subcategory": "title",
        "description": "settingDesc.SESSION_TITLE_API_KEY",
        "default": "",
        "is_sensitive": True,
    },
    # SESSION_TITLE_PROMPT：生成会话标题所用的 Prompt 模板，type=TEXT 前端渲染为
    # 多行文本框；模板中 {lang} 会被替换为目标语言、{message} 会被替换为本次对话内容，
    # default 内置了一份中文提示词，要求模型输出“表情符号 + 3-5字标题”
    "SESSION_TITLE_PROMPT": {
        "type": SettingType.TEXT,
        "category": SettingCategory.SESSION,
        "subcategory": "title",
        "description": "settingDesc.SESSION_TITLE_PROMPT",
        "default": "请您用简短的3-5个字的标题加上一个表情符号作为用户对话的提示标题。请您选取适合用于总结的表情符号来增强理解，但请避免使用符号或特殊格式。请您根据提示回复一个提示标题文本。\n\n回复示例：\n\n📉 股市趋势\n\n🍪 完美巧克力曲奇食谱\n\n🎮 视频游戏开发洞察\n\n# 重要\n\n1. 请务必用{lang}回复我\n2. 回复字数控制在3-5个字\n\nPrompt: {message}",
    },
    # ENABLE_RECOMMEND_QUESTIONS：是否启用“回答完成后生成推荐追问”功能的全局开关；
    # 这是管理员级别的总控制，普通用户无法单独关闭
    "ENABLE_RECOMMEND_QUESTIONS": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SESSION,
        "subcategory": "recommendations",
        "description": "settingDesc.ENABLE_RECOMMEND_QUESTIONS",
        "default": True,
    },
    # RECOMMEND_QUESTIONS_MAX_BACKGROUND_TASKS：生成推荐追问时允许同时运行的后台
    # 任务并发数上限，避免短时间内大量回答同时触发推荐追问生成导致资源过载
    "RECOMMEND_QUESTIONS_MAX_BACKGROUND_TASKS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "recommendations",
        "description": "settingDesc.RECOMMEND_QUESTIONS_MAX_BACKGROUND_TASKS",
        "default": 8,
    },
    # ============================================
    # Event Merger Settings
    # ============================================
    # 事件合并（Event Merger）：Agent 单次运行中会产生大量细粒度事件（如逐 token 的
    # 流式片段、工具调用的中间状态等），若原样全部持久化会给存储和前端渲染带来很大压力；
    # 该功能按 trace 周期性地把同一批事件合并为更紧凑的表示。以下设置均依赖
    # ENABLE_EVENT_MERGER 这一总开关
    # ENABLE_EVENT_MERGER：是否启用事件合并功能
    "ENABLE_EVENT_MERGER": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.ENABLE_EVENT_MERGER",
        "default": True,
        "frontend_visible": True,
    },
    # EVENT_MERGE_INTERVAL：后台合并任务的执行/扫描间隔（秒），默认 300 秒（5 分钟）
    "EVENT_MERGE_INTERVAL": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.EVENT_MERGE_INTERVAL",
        "default": 300.0,
        "depends_on": "ENABLE_EVENT_MERGER",
    },
    # EVENT_MERGE_BATCH_SIZE：单次合并处理的事件批量大小
    "EVENT_MERGE_BATCH_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.EVENT_MERGE_BATCH_SIZE",
        "default": 100,
        "depends_on": "ENABLE_EVENT_MERGER",
    },
    # EVENT_MERGE_CONCURRENCY：事件合并任务的并发处理数
    "EVENT_MERGE_CONCURRENCY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.EVENT_MERGE_CONCURRENCY",
        "default": 3,
        "depends_on": "ENABLE_EVENT_MERGER",
    },
    # EVENT_MERGE_TIMEOUT_SECONDS：单次合并操作的超时时间（秒），超时后中止该次合并，
    # 避免因单个异常 trace 拖慢整体后台任务
    "EVENT_MERGE_TIMEOUT_SECONDS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.EVENT_MERGE_TIMEOUT_SECONDS",
        "default": 120.0,
        "depends_on": "ENABLE_EVENT_MERGER",
    },
    # EVENT_MERGE_MAX_EVENTS_PER_TRACE：单个 trace 参与合并的事件数量上限，
    # 超出此规模的超大 trace 可能被跳过合并或截断处理，避免拖垮合并任务
    "EVENT_MERGE_MAX_EVENTS_PER_TRACE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.EVENT_MERGE_MAX_EVENTS_PER_TRACE",
        "default": 50000,
        "depends_on": "ENABLE_EVENT_MERGER",
    },
    # EVENT_MERGE_IMMEDIATE_DEBOUNCE_SECONDS：“立即合并”触发路径的防抖时间（秒），
    # 短时间内的多次触发只会真正执行一次合并，避免过于频繁地触发
    "EVENT_MERGE_IMMEDIATE_DEBOUNCE_SECONDS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SESSION,
        "subcategory": "events",
        "description": "settingDesc.EVENT_MERGE_IMMEDIATE_DEBOUNCE_SECONDS",
        "default": 2.0,
        "depends_on": "ENABLE_EVENT_MERGER",
    },
}

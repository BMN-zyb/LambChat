"""Tool setting definitions: MCP, Audio, Image Analysis, Image Generation, Scheduled Task."""

# 启用延迟注解求值（PEP 563），类型注解以字符串形式保存，不在模块加载时立即求值
from __future__ import annotations

# 导入配置分类枚举 SettingCategory 与配置取值类型枚举 SettingType，供下方各配置项的 category/type 字段使用
from src.kernel.schemas.setting import SettingCategory, SettingType

# 工具类配置项字典：MCP、语音转写、图片理解、图片生成、定时任务这五大块配置的元数据都集中在这里声明。
# 本字典最终会在 src/kernel/config/definitions.py 中通过 **TOOLS_SETTING_DEFINITIONS 展开合并进
# 全局的 SETTING_DEFINITIONS 字典（配置元数据的唯一权威来源），每一项会被转换成一个 SettingItem
# （定义见 src/kernel/schemas/setting.py），用于驱动后台管理设置页面的展示。
#
# 每个配置项 value 字典里常见字段说明（下面各配置项不再重复解释字段本身，只说明该项具体用途）：
#   - type：取值类型，对应 SettingType 枚举（STRING/TEXT/NUMBER/BOOLEAN/JSON/SELECT），
#     决定前端渲染成什么控件、值如何做类型转换。
#   - category / subcategory：所属分类/子分类，用于设置页面分组展示。
#   - description：是一个 i18n 文案 key（形如 settingDesc.XXX），并非直接展示的文本，
#     前端会拿这个 key 去查多语言翻译表，不要误解成实际文案内容。
#   - default：默认值，数据库/环境变量都没有覆盖时的兜底值；真实运行时的类型化默认值
#     同时写在 src/kernel/config/base.py 的 Pydantic Settings 类里，两处应保持一致。
#   - is_sensitive：标记为敏感信息（API Key/密码等），API 返回和日志中会被打码/隐藏。
#   - depends_on：控制该配置项在设置界面上的“条件显示”。字符串值表示“只有当这个字符串
#     对应的父配置项（通常是布尔开关）为真时才显示”；{"key": ..., "value": ...} 字典形式表示
#     “只有当父配置项的值等于指定值时才显示”。本文件里所有 depends_on 都是字符串形式。
#   - frontend_visible：是否在前端设置页面上直接可见，不写默认为 False（隐藏/仅内部使用）。
TOOLS_SETTING_DEFINITIONS: dict[str, dict] = {
    # ============================================
    # Mcp Settings
    # ============================================
    # 本节：MCP（Model Context Protocol）工具集成相关配置，包括全局/用户级缓存、连接池、
    # 并发加载与“延迟工具加载”机制的开关和参数。
    # 总开关：是否启用 MCP 工具集成功能。
    "ENABLE_MCP": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.ENABLE_MCP",
        "default": True,
        "frontend_visible": True,
    },
    # 以下 MCP_GLOBAL_* 系列参数用于配置进程级全局单例 MCP 管理器的行为，具体实现见
    # src/infra/tool/mcp_global.py（全局单例 + Redis 分布式锁 + Redis Pub/Sub 做跨实例缓存失效通知）。
    # 全局缓存过期时间（TTL，单位秒），默认 900 秒（15 分钟），超过该时长的缓存数据视为过期并重新加载。
    "MCP_GLOBAL_CACHE_TTL_SECONDS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_GLOBAL_CACHE_TTL_SECONDS",
        "default": 900,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": True,
    },
    # 全局缓存条目数上限，超出后触发淘汰，防止长时间运行导致内存无限增长。
    "MCP_GLOBAL_MAX_ENTRIES": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_GLOBAL_MAX_ENTRIES",
        "default": 100,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": True,
    },
    # 当另一个实例正在初始化同一份数据时，当前等待者最多等待多少秒后放弃等待（单位秒）。
    "MCP_GLOBAL_INIT_WAIT_SECONDS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_GLOBAL_INIT_WAIT_SECONDS",
        "default": 5,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": False,
    },
    # 服务启动时预热 MCP 连接的并发度，调大可加快预热速度，但会增加启动阶段的瞬时资源占用。
    "MCP_GLOBAL_WARMUP_CONCURRENCY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_GLOBAL_WARMUP_CONCURRENCY",
        "default": 5,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": False,
    },
    # 服务启动时最多预热多少个用户的 MCP 连接，超出的用户不预热，改为首次使用时按需加载。
    "MCP_GLOBAL_WARMUP_MAX_USERS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_GLOBAL_WARMUP_MAX_USERS",
        "default": 100,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": False,
    },
    # 以下 MCP_USER_* 系列参数用于配置用户级（区别于上面进程级全局）MCP 缓存的行为。
    # 用户级缓存过期时间（TTL，单位秒），默认 900 秒。
    "MCP_USER_CACHE_TTL_SECONDS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_USER_CACHE_TTL_SECONDS",
        "default": 900,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": True,
    },
    # 用户级缓存条目数上限，防止用户数过多时缓存无限增长。
    "MCP_USER_CACHE_MAX_ENTRIES": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_USER_CACHE_MAX_ENTRIES",
        "default": 100,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": True,
    },
    # MCP 连接池中连接的存活时间（单位秒），超时后连接会被回收，避免占用失效或僵死的连接。
    "MCP_POOL_TTL_SECONDS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_POOL_TTL_SECONDS",
        "default": 900,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": True,
    },
    # MCP 连接池允许维持的最大连接数，防止连接数过多耗尽资源。
    "MCP_POOL_MAX_CONNECTIONS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_POOL_MAX_CONNECTIONS",
        "default": 100,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": True,
    },
    # 并发加载多个 MCP server 时的并发度上限（见 src/infra/tool/mcp_client.py），
    # 调大可加快多 server 场景下的加载速度，但会增加瞬时并发请求量。
    "MCP_SERVER_LOAD_CONCURRENCY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_SERVER_LOAD_CONCURRENCY",
        "default": 4,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": False,
    },
    # 单个“有效 MCP 配置”中最多允许包含多少个 server，超出则拒绝/截断，防止配置过大拖慢系统。
    "MCP_EFFECTIVE_CONFIG_MAX_SERVERS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_EFFECTIVE_CONFIG_MAX_SERVERS",
        "default": 100,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": False,
    },
    # 单个“有效 MCP 配置”中最多允许包含多少个 tool，超出则拒绝/截断，防止配置过大拖慢系统。
    "MCP_EFFECTIVE_CONFIG_MAX_TOOLS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.MCP_EFFECTIVE_CONFIG_MAX_TOOLS",
        "default": 200,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": False,
    },
    # “延迟工具加载”机制总开关：开启后，当可用工具数超过阈值时不会把所有工具一次性塞进
    # LLM 的工具列表/上下文，而是改为让模型按需通过搜索来发现和调用工具（参见
    # src/agents/fast_agent/context.py、search_agent/nodes.py、team_agent/nodes.py 等）。
    "ENABLE_DEFERRED_TOOL_LOADING": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.TOOLS,
        "subcategory": "mcp",
        "description": "settingDesc.ENABLE_DEFERRED_TOOL_LOADING",
        "default": True,
        "depends_on": "ENABLE_MCP",
        "frontend_visible": True,
    },
    # 触发延迟工具加载的工具数量阈值：可用工具数超过该值才会切换到按需搜索模式。
    "DEFERRED_TOOL_THRESHOLD": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "deferred",
        "description": "settingDesc.DEFERRED_TOOL_THRESHOLD",
        "default": 20,
        "depends_on": "ENABLE_DEFERRED_TOOL_LOADING",
    },
    # 延迟加载模式下，一次工具搜索最多返回多少个匹配的工具结果。
    "DEFERRED_TOOL_SEARCH_LIMIT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "deferred",
        "description": "settingDesc.DEFERRED_TOOL_SEARCH_LIMIT",
        "default": 25,
        "depends_on": "ENABLE_DEFERRED_TOOL_LOADING",
    },
    # ============================================
    # Audio Transcription Settings
    # ============================================
    # 本节：语音转文字（Audio Transcription）功能配置，用于将音频文件转写为文本。
    # 总开关：是否启用语音转写功能。
    "ENABLE_AUDIO_TRANSCRIPTION": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.AUDIO_TRANSCRIPTION,
        "description": "settingDesc.ENABLE_AUDIO_TRANSCRIPTION",
        "default": False,
    },
    # 语音转写服务（兼容 OpenAI 接口协议）的 API Key，属于敏感信息。
    "AUDIO_TRANSCRIPTION_API_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.AUDIO_TRANSCRIPTION,
        "description": "settingDesc.AUDIO_TRANSCRIPTION_API_KEY",
        "default": "",
        "is_sensitive": True,
        "depends_on": "ENABLE_AUDIO_TRANSCRIPTION",
    },
    # 语音转写服务的接口地址（Base URL），需兼容 OpenAI 转写接口协议。
    "AUDIO_TRANSCRIPTION_BASE_URL": {
        "type": SettingType.STRING,
        "category": SettingCategory.AUDIO_TRANSCRIPTION,
        "description": "settingDesc.AUDIO_TRANSCRIPTION_BASE_URL",
        "default": "",
        "depends_on": "ENABLE_AUDIO_TRANSCRIPTION",
    },
    # 语音转写使用的模型名称，默认使用 gpt-4o-mini-transcribe。
    "AUDIO_TRANSCRIPTION_MODEL": {
        "type": SettingType.STRING,
        "category": SettingCategory.AUDIO_TRANSCRIPTION,
        "description": "settingDesc.AUDIO_TRANSCRIPTION_MODEL",
        "default": "gpt-4o-mini-transcribe",
        "depends_on": "ENABLE_AUDIO_TRANSCRIPTION",
    },
    # 下载待转写音频文件时允许的最大字节数，默认 52428800 字节（50MB），超出则拒绝下载/转写。
    "AUDIO_TRANSCRIPTION_MAX_DOWNLOAD_BYTES": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.AUDIO_TRANSCRIPTION,
        "description": "settingDesc.AUDIO_TRANSCRIPTION_MAX_DOWNLOAD_BYTES",
        "default": 52428800,
        "depends_on": "ENABLE_AUDIO_TRANSCRIPTION",
    },
    # ============================================
    # Image Analysis Settings
    # ============================================
    # 本节：图片理解/视觉分析（Image Analysis）功能配置，用于让模型理解图片内容。
    # 总开关：是否启用图片分析功能。
    "ENABLE_IMAGE_ANALYSIS": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.TOOLS,
        "subcategory": "image_analysis",
        "description": "settingDesc.ENABLE_IMAGE_ANALYSIS",
        "default": False,
        "frontend_visible": True,
    },
    # 用于图片分析的视觉模型 ID。
    "IMAGE_ANALYSIS_MODEL_ID": {
        "type": SettingType.STRING,
        "category": SettingCategory.TOOLS,
        "subcategory": "image_analysis",
        "description": "settingDesc.IMAGE_ANALYSIS_MODEL_ID",
        "default": "",
        "depends_on": "ENABLE_IMAGE_ANALYSIS",
        "frontend_visible": True,
    },
    # 图片分析失败时的最大重试次数（含首次调用），默认 3 次。
    "IMAGE_ANALYSIS_MAX_ATTEMPTS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "image_analysis",
        "description": "settingDesc.IMAGE_ANALYSIS_MAX_ATTEMPTS",
        "default": 3,
        "depends_on": "ENABLE_IMAGE_ANALYSIS",
    },
    # 图片分析失败后，下一次重试前的等待时间（单位秒），默认 1.0 秒。
    "IMAGE_ANALYSIS_RETRY_DELAY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "image_analysis",
        "description": "settingDesc.IMAGE_ANALYSIS_RETRY_DELAY",
        "default": 1.0,
        "depends_on": "ENABLE_IMAGE_ANALYSIS",
    },
    # ============================================
    # Image Generation Settings
    # ============================================
    # 本节：AI 生图（Image Generation）功能配置。
    # 总开关：是否启用图片生成功能。
    "ENABLE_IMAGE_GENERATION": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.TOOLS,
        "subcategory": "image_generation",
        "description": "settingDesc.ENABLE_IMAGE_GENERATION",
        "default": False,
        "frontend_visible": True,
    },
    # 生图服务的 API Key，属于敏感信息。
    "IMAGE_GENERATION_API_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.TOOLS,
        "subcategory": "image_generation",
        "description": "settingDesc.IMAGE_GENERATION_API_KEY",
        "default": "",
        "is_sensitive": True,
        "depends_on": "ENABLE_IMAGE_GENERATION",
    },
    # 生图服务的接口地址（Base URL），默认指向 OpenAI 官方接口地址。
    "IMAGE_GENERATION_BASE_URL": {
        "type": SettingType.STRING,
        "category": SettingCategory.TOOLS,
        "subcategory": "image_generation",
        "description": "settingDesc.IMAGE_GENERATION_BASE_URL",
        "default": "https://api.openai.com/v1",
        "depends_on": "ENABLE_IMAGE_GENERATION",
    },
    # 生图使用的模型名称，默认 gpt-image-2。
    "IMAGE_GENERATION_MODEL": {
        "type": SettingType.STRING,
        "category": SettingCategory.TOOLS,
        "subcategory": "image_generation",
        "description": "settingDesc.IMAGE_GENERATION_MODEL",
        "default": "gpt-image-2",
        "depends_on": "ENABLE_IMAGE_GENERATION",
    },
    # 调用生图服务的请求超时时间（单位秒），默认 120 秒；生图耗时通常较长，超时时间需设置得比一般接口更长。
    "IMAGE_GENERATION_TIMEOUT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.TOOLS,
        "subcategory": "image_generation",
        "description": "settingDesc.IMAGE_GENERATION_TIMEOUT",
        "default": 120,
        "depends_on": "ENABLE_IMAGE_GENERATION",
    },
    # ============================================
    # Scheduled Task Settings
    # ============================================
    # 本节：定时任务/计划任务（Scheduled Task）功能配置。
    # 总开关：是否启用定时任务功能。
    "ENABLE_SCHEDULED_TASK": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SCHEDULED_TASK,
        "subcategory": "general",
        "description": "settingDesc.ENABLE_SCHEDULED_TASK",
        "default": False,
        "frontend_visible": True,
    },
}

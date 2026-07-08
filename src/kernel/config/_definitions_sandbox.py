"""Sandbox setting definitions: Sandbox platform, Skills, Code Interpreter."""

from __future__ import annotations

from src.kernel.schemas.setting import SettingCategory, SettingType

# Sandbox 配置项字典：Sandbox（代码执行沙箱）、Skills（技能包）、Code Interpreter（代码解释器）
# 三大块配置的元数据都集中在这里声明。本字典最终会在 src/kernel/config/definitions.py 中通过
# **SANDBOX_SETTING_DEFINITIONS 展开合并进全局的 SETTING_DEFINITIONS 字典（配置元数据的唯一权威
# 来源），每一项会被转换成一个 SettingItem（定义见 src/kernel/schemas/setting.py），用于驱动后台
# 管理设置页面的展示。
#
# 每个配置项 value 字典里常见字段说明（下面各配置项不再重复解释字段本身，只说明该项具体用途）：
#   - type：取值类型，对应 SettingType 枚举（STRING/NUMBER/BOOLEAN/SELECT 等），决定前端渲染成
#     什么控件、值如何做类型转换；本文件中 SANDBOX_PLATFORM 用的是 SELECT 类型，需配合 options
#     字段给出可选项列表。
#   - category / subcategory：所属分类/子分类，用于设置页面分组展示。
#   - description：是一个 i18n 文案 key（形如 settingDesc.XXX），并非直接展示的文本，前端会拿这个
#     key 去查多语言翻译表，不要误解成实际文案内容。
#   - default：默认值，数据库/环境变量都没有覆盖时的兜底值；真实运行时的类型化默认值同时写在
#     src/kernel/config/base.py 的 Pydantic Settings 类里，两处应保持一致。
#   - is_sensitive：标记为敏感信息（API Key 等），API 返回和日志中会被打码/隐藏。
#   - depends_on：控制该配置项在设置界面上的“条件显示”。字符串值表示“只有当这个字符串对应的
#     父配置项（通常是布尔开关）为真时才显示”；{"key": ..., "value": ...} 字典形式（对应
#     SettingDependsOn schema）表示“只有当父配置项的取值等于指定 value 时才显示”。本文件里大量
#     使用字典形式：所有 Daytona/E2B/CubeSandbox 专属配置都依赖 SANDBOX_PLATFORM 这个 SELECT
#     配置的具体取值，详见下方 SANDBOX_PLATFORM 处的说明。
#   - frontend_visible：是否在前端设置页面上直接可见，不写默认为 False（隐藏/仅内部使用）。
#   - options：SELECT 类型的可选值列表。
SANDBOX_SETTING_DEFINITIONS: dict[str, dict] = {
    # ============================================
    # Sandbox Settings
    # ============================================
    # 本节：Sandbox（代码执行沙箱）平台配置，包括总开关、具体使用哪个沙箱后端，以及
    # Daytona/E2B/CubeSandbox 三种沙箱平台各自的连接参数与生命周期管理参数。
    # 总开关：是否启用代码执行沙箱子系统，关闭后 Sandbox 相关功能全部不可用。
    "ENABLE_SANDBOX": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SANDBOX,
        "subcategory": "general",
        "description": "settingDesc.ENABLE_SANDBOX",
        "default": False,
        "frontend_visible": True,
    },
    # 选择具体使用哪个沙箱后端实现，可选 daytona/e2b/cubesandbox，默认 daytona；本项通过
    # depends_on="ENABLE_SANDBOX" 依赖上面的总开关（字符串形式：父配置为真才显示）。同时，
    # 下方所有 DAYTONA_*/E2B_*/CUBE_* 专属配置都以 depends_on={"key": "SANDBOX_PLATFORM",
    # "value": "<平台名>"} 的字典形式依赖本配置项的取值——只有当 SANDBOX_PLATFORM 选中对应
    # 平台时，该平台的专属配置才会在设置页面上出现，这是“一个父配置的取值决定一组子配置
    # 是否显示”的联动写法。
    "SANDBOX_PLATFORM": {
        "type": SettingType.SELECT,
        "category": SettingCategory.SANDBOX,
        "subcategory": "general",
        "description": "settingDesc.SANDBOX_PLATFORM",
        "default": "daytona",
        "depends_on": "ENABLE_SANDBOX",
        "options": ["daytona", "e2b", "cubesandbox"],
    },
    # Daytona 平台的 API 认证 Key，属于敏感信息（is_sensitive），仅当 SANDBOX_PLATFORM
    # 选中 daytona 时在设置页面显示。
    "DAYTONA_API_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.SANDBOX,
        "subcategory": "daytona",
        "description": "settingDesc.DAYTONA_API_KEY",
        "default": "",
        "is_sensitive": True,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "daytona"},
    },
    # Daytona 平台的服务端地址（Server URL）。
    "DAYTONA_SERVER_URL": {
        "type": SettingType.STRING,
        "category": SettingCategory.SANDBOX,
        "subcategory": "daytona",
        "description": "settingDesc.DAYTONA_SERVER_URL",
        "default": "",
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "daytona"},
    },
    # Daytona 沙箱操作的超时时间（单位秒），默认 180 秒。
    "DAYTONA_TIMEOUT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SANDBOX,
        "subcategory": "daytona",
        "description": "settingDesc.DAYTONA_TIMEOUT",
        "default": 180,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "daytona"},
    },
    # 在沙箱内执行 grep 搜索操作的超时时间（单位秒），默认 30 秒，实际用于
    # src/infra/sandbox_grep.py；依赖 ENABLE_SANDBOX 总开关，不区分具体沙箱平台。
    "SANDBOX_GREP_TIMEOUT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SANDBOX,
        "subcategory": "general",
        "description": "settingDesc.SANDBOX_GREP_TIMEOUT",
        "default": 30,
        "depends_on": "ENABLE_SANDBOX",
    },
    # 沙箱内重建 MCP（重新安装/连接 MCP server）任务的并发度，默认 4，实际用于
    # src/infra/tool/sandbox_mcp_rebuild.py；调大可加快批量重建速度，但会增加瞬时资源占用。
    "SANDBOX_MCP_REBUILD_CONCURRENCY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SANDBOX,
        "subcategory": "general",
        "description": "settingDesc.SANDBOX_MCP_REBUILD_CONCURRENCY",
        "default": 4,
        "depends_on": "ENABLE_SANDBOX",
    },
    # Daytona 沙箱创建时使用的基础镜像。
    "DAYTONA_IMAGE": {
        "type": SettingType.STRING,
        "category": SettingCategory.SANDBOX,
        "subcategory": "daytona",
        "description": "settingDesc.DAYTONA_IMAGE",
        "default": "",
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "daytona"},
    },
    # Daytona 沙箱闲置多久（单位分钟）后自动停止，默认 5 分钟，用于节省沙箱资源开销。
    "DAYTONA_AUTO_STOP_INTERVAL": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SANDBOX,
        "subcategory": "daytona",
        "description": "settingDesc.DAYTONA_AUTO_STOP_INTERVAL",
        "default": 5,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "daytona"},
    },
    # Daytona 沙箱闲置多久（单位分钟）后自动归档，默认 5 分钟。
    "DAYTONA_AUTO_ARCHIVE_INTERVAL": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SANDBOX,
        "subcategory": "daytona",
        "description": "settingDesc.DAYTONA_AUTO_ARCHIVE_INTERVAL",
        "default": 5,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "daytona"},
    },
    # Daytona 沙箱闲置多久（单位分钟）后自动删除，默认 1440 分钟（即 24 小时）。
    "DAYTONA_AUTO_DELETE_INTERVAL": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SANDBOX,
        "subcategory": "daytona",
        "description": "settingDesc.DAYTONA_AUTO_DELETE_INTERVAL",
        "default": 1440,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "daytona"},
    },
    # E2B 平台的 API 认证 Key，属于敏感信息。
    "E2B_API_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.SANDBOX,
        "subcategory": "e2b",
        "description": "settingDesc.E2B_API_KEY",
        "default": "",
        "is_sensitive": True,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "e2b"},
    },
    # E2B 沙箱使用的模板名称，默认 base。
    "E2B_TEMPLATE": {
        "type": SettingType.STRING,
        "category": SettingCategory.SANDBOX,
        "subcategory": "e2b",
        "description": "settingDesc.E2B_TEMPLATE",
        "default": "base",
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "e2b"},
    },
    # E2B 沙箱的超时时间（单位秒），默认 3600 秒（即 1 小时）。
    "E2B_TIMEOUT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SANDBOX,
        "subcategory": "e2b",
        "description": "settingDesc.E2B_TIMEOUT",
        "default": 3600,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "e2b"},
    },
    # E2B 沙箱是否在闲置时自动暂停，默认开启。
    "E2B_AUTO_PAUSE": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SANDBOX,
        "subcategory": "e2b",
        "description": "settingDesc.E2B_AUTO_PAUSE",
        "default": True,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "e2b"},
    },
    # E2B 沙箱是否在再次使用时自动恢复，默认开启。
    "E2B_AUTO_RESUME": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SANDBOX,
        "subcategory": "e2b",
        "description": "settingDesc.E2B_AUTO_RESUME",
        "default": True,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "e2b"},
    },
    # CubeSandbox 平台的 API 地址，默认指向本机 http://127.0.0.1:3000。
    "CUBE_API_URL": {
        "type": SettingType.STRING,
        "category": SettingCategory.SANDBOX,
        "subcategory": "cubesandbox",
        "description": "settingDesc.CUBE_API_URL",
        "default": "http://127.0.0.1:3000",
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "cubesandbox"},
    },
    # CubeSandbox 使用的模板名称。
    "CUBE_TEMPLATE": {
        "type": SettingType.STRING,
        "category": SettingCategory.SANDBOX,
        "subcategory": "cubesandbox",
        "description": "settingDesc.CUBE_TEMPLATE",
        "default": "",
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "cubesandbox"},
    },
    # CubeSandbox 沙箱的超时时间（单位秒），默认 3600 秒。
    "CUBE_TIMEOUT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SANDBOX,
        "subcategory": "cubesandbox",
        "description": "settingDesc.CUBE_TIMEOUT",
        "default": 3600,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "cubesandbox"},
    },
    # CubeSandbox 代理节点的 IP 地址。
    "CUBE_PROXY_NODE_IP": {
        "type": SettingType.STRING,
        "category": SettingCategory.SANDBOX,
        "subcategory": "cubesandbox",
        "description": "settingDesc.CUBE_PROXY_NODE_IP",
        "default": "",
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "cubesandbox"},
    },
    # CubeSandbox 代理的 HTTP 端口，默认 80。
    "CUBE_PROXY_PORT_HTTP": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SANDBOX,
        "subcategory": "cubesandbox",
        "description": "settingDesc.CUBE_PROXY_PORT_HTTP",
        "default": 80,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "cubesandbox"},
    },
    # CubeSandbox 沙箱对外访问使用的域名，默认 cube.app。
    "CUBE_SANDBOX_DOMAIN": {
        "type": SettingType.STRING,
        "category": SettingCategory.SANDBOX,
        "subcategory": "cubesandbox",
        "description": "settingDesc.CUBE_SANDBOX_DOMAIN",
        "default": "cube.app",
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "cubesandbox"},
    },
    # CubeSandbox 单次请求的超时时间（单位秒），默认 120 秒。
    "CUBE_REQUEST_TIMEOUT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SANDBOX,
        "subcategory": "cubesandbox",
        "description": "settingDesc.CUBE_REQUEST_TIMEOUT",
        "default": 120,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "cubesandbox"},
    },
    # CubeSandbox 沙箱是否在闲置时自动暂停，默认开启。
    "CUBE_AUTO_PAUSE": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SANDBOX,
        "subcategory": "cubesandbox",
        "description": "settingDesc.CUBE_AUTO_PAUSE",
        "default": True,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "cubesandbox"},
    },
    # CubeSandbox 沙箱是否在再次使用时自动恢复，默认开启。
    "CUBE_AUTO_RESUME": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SANDBOX,
        "subcategory": "cubesandbox",
        "description": "settingDesc.CUBE_AUTO_RESUME",
        "default": True,
        "depends_on": {"key": "SANDBOX_PLATFORM", "value": "cubesandbox"},
    },
    # ============================================
    # Skills Settings
    # ============================================
    # 本节：Skills（可复用的 Agent 技能包）功能配置。
    # 总开关：是否启用 Skills 功能，默认开启。
    "ENABLE_SKILLS": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.SKILLS,
        "subcategory": "general",
        "description": "settingDesc.ENABLE_SKILLS",
        "default": True,
        "frontend_visible": True,
    },
    # ============================================
    # Code Interpreter Settings
    # ============================================
    # 本节：Code Interpreter（代码解释器）工具配置，用于让 Agent 在沙箱中执行代码。
    # 总开关：是否启用代码解释器工具。
    "ENABLE_CODE_INTERPRETER": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.TOOLS,
        "subcategory": "code",
        "description": "settingDesc.ENABLE_CODE_INTERPRETER",
        "default": False,
        "frontend_visible": True,
    },
}

"""Configuration constants."""
# 本模块存放配置系统中相对"静态"的常量与派生规则，是 base.py / service.py 等模块的基础依赖：
# 1) 安全相关的最小长度限制（JWT 密钥、MCP 加密 salt），供 base.py 在启动时做校验/扩展；
# 2) RESTART_REQUIRED_SETTINGS：修改后必须重启进程才能生效的配置项集合（因为对应资源
#    通常在启动时一次性建立，如连接池、监听端口），供管理后台提示用户"需要重启"；
# 3) SENSITIVE_SETTINGS：敏感配置项集合，从 SETTING_DEFINITIONS 派生而来，
#    用于在接口返回/日志打印时对这些字段做掩码处理，避免泄露密钥等敏感信息。

# HS256 算法建议密钥长度不少于 32 字节，否则签名容易被暴力破解；
# base.py 中 Settings.__init__ 会校验 JWT_SECRET_KEY 长度，不足时通过哈希扩展到该长度
# Minimum JWT secret key length (32 bytes for HS256)
JWT_SECRET_KEY_MIN_LENGTH = 32

# MCP_ENCRYPTION_SALT 用于对用户配置的 MCP 服务器密钥等敏感信息做加密派生，
# salt 至少 16 字节才能保证 KDF（密钥派生函数）的安全性；不足时同样会被扩展
# Minimum MCP encryption salt length (16 bytes for KDF security)
MCP_ENCRYPTION_SALT_MIN_LENGTH = 16

# 该集合会被 service.py 的 SettingsService.requires_restart() 引用，
# 管理后台在保存设置后据此提示用户"此项修改需要重启服务才能生效"
# ============================================
# Settings that require server restart to take effect
# ============================================
RESTART_REQUIRED_SETTINGS = {
    # 监听地址/端口：uvicorn 进程启动时绑定一次，运行期修改不会重新绑定
    "HOST",
    "PORT",
    # LangGraph checkpoint 后端类型及 PostgreSQL 连接参数：连接池在启动时创建，
    # 修改后需重启才能用新参数重新建立连接
    "CHECKPOINT_BACKEND",
    "CHECKPOINT_PG_HOST",
    "CHECKPOINT_PG_PORT",
    "CHECKPOINT_PG_USER",
    "CHECKPOINT_PG_PASSWORD",
    "CHECKPOINT_PG_DB",
    "CHECKPOINT_PG_POOL_MIN_SIZE",
    "CHECKPOINT_PG_POOL_MAX_SIZE",
    # MongoDB 连接：client 在启动时创建，运行期修改 URL/DB 不会自动重连
    "MONGODB_URL",
    "MONGODB_DB",
    # Redis 连接：同上，连接池在启动时建立
    "REDIS_URL",
    "REDIS_PASSWORD",
    # JWT 签名密钥：运行期切换会导致此前已签发的 token 全部校验失败，
    # 为避免"改配置却让所有用户瞬间掉线"的意外行为，要求重启后统一生效
    "JWT_SECRET_KEY",
}


def _build_sensitive_settings() -> set[str]:
    """Build SENSITIVE_SETTINGS from definitions where is_sensitive=True."""
    # 延迟到函数体内部才 import definitions 模块，而不是放在文件顶部：
    # constants.py 会在包加载的很早期被 base.py import
    # （config/__init__.py -> base.py -> constants.py），此时把 definitions.py 的
    # import 放到函数内部，可以避免让 constants.py 在模块顶层就直接依赖
    # definitions.py 及其间接依赖，降低未来出现循环导入的风险，写法更稳健
    from src.kernel.config.definitions import SETTING_DEFINITIONS

    # 遍历所有配置项定义，筛选出标记了 is_sensitive=True 的 key
    # （如各种 API_KEY、PASSWORD、SECRET 等），供接口/日志脱敏使用
    return {k for k, v in SETTING_DEFINITIONS.items() if v.get("is_sensitive", False)}


# 模块加载时立即计算一次并缓存为模块级常量；SETTING_DEFINITIONS 在运行期是静态字典，
# 不会动态增删 key，因此这里不需要每次访问都重新计算
SENSITIVE_SETTINGS = _build_sensitive_settings()

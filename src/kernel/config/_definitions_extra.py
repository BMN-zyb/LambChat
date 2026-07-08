"""Extra setting definitions — security, storage, user, and memory settings."""
# 本文件是配置系统“设置项元数据”的另一个领域分片（extra 分片），
# 由 definitions.py 与 core / sandbox / tools / infra 分片一起汇总为全局
# SETTING_DEFINITIONS。本文件覆盖：JWT 鉴权、Web Push、邮件服务、
# Cloudflare Turnstile 人机验证、S3/本地文件存储、PostgreSQL 长期存储、
# LangGraph Checkpoint 后端、用户注册、OAuth 第三方登录，以及跨会话记忆
# （Memory）子系统的全部设置项。
# 与 _definitions_core.py 中的说明一致：这里存的是“设置项元数据”
# （类型/分类/默认值/是否敏感/可见性依赖等），设置项的实际运行值保存在
# MongoDB 的 system_settings 集合中，数据库无覆盖记录时才回退到此处的 "default"
# （参见 src/infra/settings/storage.py）。各字段含义可参考 _definitions_core.py
# 顶部的详细说明，此处不再重复。

from __future__ import annotations

# 仅用到两个枚举：SettingType 决定前端渲染的表单控件，
# SettingCategory 决定该设置项在管理后台的分组
from src.kernel.schemas.setting import SettingCategory, SettingType

EXTRA_SETTING_DEFINITIONS: dict[str, dict] = {
    # ============================================
    # JWT Authentication Settings
    # ============================================
    # 登录态使用的 JWT（JSON Web Token）签发相关参数：签名算法与访问/刷新令牌有效期
    # JWT_ALGORITHM：签发/校验 JWT 使用的签名算法，默认 HS256（对称密钥签名）；
    # 对应的密钥 JWT_SECRET_KEY 要求长度不少于 32 字节（见 constants.py）
    "JWT_ALGORITHM": {
        "type": SettingType.STRING,
        "category": SettingCategory.SECURITY,
        "subcategory": "jwt",
        "description": "settingDesc.JWT_ALGORITHM",
        "default": "HS256",
    },
    # ACCESS_TOKEN_EXPIRE_HOURS：访问令牌（access token）的有效期（小时），
    # 过期后前端需使用刷新令牌换取新的访问令牌
    "ACCESS_TOKEN_EXPIRE_HOURS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SECURITY,
        "subcategory": "jwt",
        "description": "settingDesc.ACCESS_TOKEN_EXPIRE_HOURS",
        "default": 24,
    },
    # REFRESH_TOKEN_EXPIRE_DAYS：刷新令牌（refresh token）的有效期（天），
    # 超过此时长未使用则需要用户重新登录
    "REFRESH_TOKEN_EXPIRE_DAYS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.SECURITY,
        "subcategory": "jwt",
        "description": "settingDesc.REFRESH_TOKEN_EXPIRE_DAYS",
        "default": 7,
    },
    # ============================================
    # Web Push (VAPID) Settings
    # ============================================
    # Web Push（网页推送通知）使用的 VAPID（Voluntary Application Server Identification）
    # 密钥对与身份标识；三项均留空时，系统会在启动时自动生成一套密钥
    # （参见 service.py 中 _vapid_keys_generated 相关逻辑），管理员显式填写后则不再自动生成
    # VAPID_PUBLIC_KEY：VAPID 公钥，会下发给浏览器用于订阅推送服务
    "VAPID_PUBLIC_KEY": {
        "type": SettingType.TEXT,
        "category": SettingCategory.SECURITY,
        "subcategory": "web_push",
        "description": "settingDesc.VAPID_PUBLIC_KEY",
        "default": "",
    },
    # VAPID_PRIVATE_KEY：VAPID 私钥，服务端用它对推送消息签名；is_sensitive=True，
    # 管理后台展示时会被脱敏为 "********"
    "VAPID_PRIVATE_KEY": {
        "type": SettingType.TEXT,
        "category": SettingCategory.SECURITY,
        "subcategory": "web_push",
        "description": "settingDesc.VAPID_PRIVATE_KEY",
        "default": "",
        "is_sensitive": True,
    },
    # VAPID_SUBJECT：VAPID 身份标识（mailto: 邮箱或 URL），
    # 供推送服务提供商在出问题时联系应用管理员
    "VAPID_SUBJECT": {
        "type": SettingType.STRING,
        "category": SettingCategory.SECURITY,
        "subcategory": "web_push",
        "description": "settingDesc.VAPID_SUBJECT",
        "default": "mailto:admin@example.com",
    },
    # ============================================
    # Email Settings (Resend)
    # ============================================
    # 邮件服务总开关及依赖它的两个子功能；具体的发信账户配置在 core 分片的
    # RESEND_ACCOUNTS 中（EMAIL_ENABLED 是它们共同的 depends_on 父开关）
    # EMAIL_ENABLED：是否启用邮件服务（密码重置邮件、邮箱验证邮件等）；
    # 关闭时即使配置了 Resend 账户也不会发信
    "EMAIL_ENABLED": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.EMAIL,
        "subcategory": "general",
        "description": "settingDesc.EMAIL_ENABLED",
        "default": False,
        "frontend_visible": True,
    },
    # REQUIRE_EMAIL_VERIFICATION：是否要求用户完成邮箱验证后才能登录/使用；
    # 依赖 EMAIL_ENABLED（未启用邮件服务时该项无意义，管理后台也不会展示）
    "REQUIRE_EMAIL_VERIFICATION": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.EMAIL,
        "subcategory": "general",
        "description": "settingDesc.REQUIRE_EMAIL_VERIFICATION",
        "default": False,
        "depends_on": "EMAIL_ENABLED",
        "frontend_visible": True,
    },
    # PASSWORD_RESET_EXPIRE_HOURS：密码重置邮件中链接/验证码的有效期（小时），
    # 超时后需要用户重新发起找回密码流程
    "PASSWORD_RESET_EXPIRE_HOURS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.EMAIL,
        "subcategory": "general",
        "description": "settingDesc.PASSWORD_RESET_EXPIRE_HOURS",
        "default": 24,
        "depends_on": "EMAIL_ENABLED",
    },
    # ============================================
    # Cloudflare Turnstile (CAPTCHA) Settings
    # ============================================
    # Cloudflare Turnstile 是一种人机验证（CAPTCHA）服务，用于登录/注册/改密等
    # 敏感操作前拦截机器人；以下三个 REQUIRE_ON_* 开关可分别控制在哪些场景强制校验，
    # 均依赖 TURNSTILE_ENABLED 这一总开关
    # TURNSTILE_ENABLED：是否启用 Turnstile 人机验证功能的总开关
    "TURNSTILE_ENABLED": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.CAPTCHA,
        "subcategory": "turnstile",
        "description": "settingDesc.TURNSTILE_ENABLED",
        "default": False,
        "frontend_visible": True,
    },
    # TURNSTILE_SITE_KEY：Turnstile 站点密钥（公开），前端渲染验证组件时使用
    "TURNSTILE_SITE_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.CAPTCHA,
        "subcategory": "turnstile",
        "description": "settingDesc.TURNSTILE_SITE_KEY",
        "default": "",
        "depends_on": "TURNSTILE_ENABLED",
        "frontend_visible": True,
    },
    # TURNSTILE_SECRET_KEY：Turnstile 密钥（保密），服务端调用 Cloudflare 验证接口时使用；
    # is_sensitive=True，管理后台展示时会被脱敏
    "TURNSTILE_SECRET_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.CAPTCHA,
        "subcategory": "turnstile",
        "description": "settingDesc.TURNSTILE_SECRET_KEY",
        "default": "",
        "depends_on": "TURNSTILE_ENABLED",
        "is_sensitive": True,
    },
    # TURNSTILE_REQUIRE_ON_LOGIN：登录时是否强制要求通过 Turnstile 验证
    "TURNSTILE_REQUIRE_ON_LOGIN": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.CAPTCHA,
        "subcategory": "turnstile",
        "description": "settingDesc.TURNSTILE_REQUIRE_ON_LOGIN",
        "default": False,
        "depends_on": "TURNSTILE_ENABLED",
        "frontend_visible": True,
    },
    # TURNSTILE_REQUIRE_ON_REGISTER：注册时是否强制要求通过 Turnstile 验证，
    # 默认开启（True），因为注册接口通常是恶意机器人/批量注册的主要攻击面
    "TURNSTILE_REQUIRE_ON_REGISTER": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.CAPTCHA,
        "subcategory": "turnstile",
        "description": "settingDesc.TURNSTILE_REQUIRE_ON_REGISTER",
        "default": True,
        "depends_on": "TURNSTILE_ENABLED",
        "frontend_visible": True,
    },
    # TURNSTILE_REQUIRE_ON_PASSWORD_CHANGE：修改密码时是否强制要求通过 Turnstile 验证
    "TURNSTILE_REQUIRE_ON_PASSWORD_CHANGE": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.CAPTCHA,
        "subcategory": "turnstile",
        "description": "settingDesc.TURNSTILE_REQUIRE_ON_PASSWORD_CHANGE",
        "default": True,
        "depends_on": "TURNSTILE_ENABLED",
        "frontend_visible": True,
    },
    # ============================================
    # S3 Storage Settings
    # ============================================
    # S3 兼容对象存储的连接配置，支持 AWS/阿里云/腾讯云/MinIO/自定义 兼容端点，
    # 用于替代本地文件系统存放用户上传的文件；S3_ENABLED 为总开关，其余均依赖它
    # S3_ENABLED：是否启用 S3 兼容存储；关闭时文件上传落地到 LOCAL_STORAGE_PATH 指向的
    # 本地目录
    "S3_ENABLED": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.S3,
        "subcategory": "connection",
        "description": "settingDesc.S3_ENABLED",
        "default": False,
        "frontend_visible": True,
    },
    # S3_PROVIDER：选择的 S3 提供商（决定 SDK 内部签名/端点等细节的预设方式）；
    # 下拉可选值见 options：aws / aliyun（阿里云 OSS）/ tencent（腾讯云 COS）/
    # minio（自建 MinIO）/ custom（自定义兼容端点）
    "S3_PROVIDER": {
        "type": SettingType.SELECT,
        "category": SettingCategory.S3,
        "subcategory": "connection",
        "description": "settingDesc.S3_PROVIDER",
        "default": "aws",
        "depends_on": "S3_ENABLED",
        "options": ["aws", "aliyun", "tencent", "minio", "custom"],
    },
    # S3_ENDPOINT_URL：S3 兼容服务的访问端点 URL；使用 MinIO 或 custom 提供商时必填，
    # 使用 AWS/阿里云/腾讯云等公有云默认端点时可留空
    "S3_ENDPOINT_URL": {
        "type": SettingType.STRING,
        "category": SettingCategory.S3,
        "subcategory": "connection",
        "description": "settingDesc.S3_ENDPOINT_URL",
        "default": "",
        "depends_on": "S3_ENABLED",
    },
    # S3_ACCESS_KEY：S3 访问密钥 ID；is_sensitive=True，接口返回时会被脱敏
    "S3_ACCESS_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.S3,
        "subcategory": "connection",
        "description": "settingDesc.S3_ACCESS_KEY",
        "default": "",
        "is_sensitive": True,
        "depends_on": "S3_ENABLED",
    },
    # S3_SECRET_KEY：S3 访问密钥 Secret；is_sensitive=True，接口返回时会被脱敏
    "S3_SECRET_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.S3,
        "subcategory": "connection",
        "description": "settingDesc.S3_SECRET_KEY",
        "default": "",
        "is_sensitive": True,
        "depends_on": "S3_ENABLED",
    },
    # S3_REGION：S3 存储桶所在区域，默认 us-east-1（AWS 默认区域）；
    # 国内云厂商通常需要改为对应区域代码
    "S3_REGION": {
        "type": SettingType.STRING,
        "category": SettingCategory.S3,
        "subcategory": "connection",
        "description": "settingDesc.S3_REGION",
        "default": "us-east-1",
        "depends_on": "S3_ENABLED",
    },
    # S3_BUCKET_NAME：存放文件的 S3 存储桶名称
    "S3_BUCKET_NAME": {
        "type": SettingType.STRING,
        "category": SettingCategory.S3,
        "subcategory": "bucket",
        "description": "settingDesc.S3_BUCKET_NAME",
        "default": "",
        "depends_on": "S3_ENABLED",
    },
    # S3_CUSTOM_DOMAIN：访问 S3 文件时使用的自定义 CDN 域名，配置后生成的文件 URL
    # 会使用该域名而不是默认的 S3/OSS/COS 域名，便于走 CDN 加速或自有域名
    "S3_CUSTOM_DOMAIN": {
        "type": SettingType.STRING,
        "category": SettingCategory.S3,
        "subcategory": "bucket",
        "description": "settingDesc.S3_CUSTOM_DOMAIN",
        "default": "",
        "depends_on": "S3_ENABLED",
    },
    # S3_PATH_STYLE：是否使用路径风格的访问 URL（http://host/bucket/key，
    # 而非虚拟主机风格 http://bucket.host/key）；自建 MinIO 通常需要开启
    "S3_PATH_STYLE": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.S3,
        "subcategory": "bucket",
        "description": "settingDesc.S3_PATH_STYLE",
        "default": False,
        "depends_on": "S3_ENABLED",
    },
    # S3_PUBLIC_BUCKET：存储桶是否配置为公开可读；为 True 时可直接拼接 URL 访问文件，
    # 为 False 时需要通过预签名 URL（见 S3_PRESIGNED_URL_EXPIRES）临时授权访问
    "S3_PUBLIC_BUCKET": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.S3,
        "subcategory": "bucket",
        "description": "settingDesc.S3_PUBLIC_BUCKET",
        "default": False,
        "depends_on": "S3_ENABLED",
    },
    # S3_MAX_FILE_SIZE：单个文件上传到 S3 允许的最大字节数，默认 10485760 字节（10MB）
    "S3_MAX_FILE_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.S3,
        "subcategory": "limits",
        "description": "settingDesc.S3_MAX_FILE_SIZE",
        "default": 10485760,
        "depends_on": "S3_ENABLED",
    },
    # S3_INTERNAL_UPLOAD_MAX_SIZE：系统内部（如后台任务生成的文件）上传到 S3 的
    # 最大字节数限制，默认 52428800 字节（50MB），通常比普通用户上传限制更宽松
    "S3_INTERNAL_UPLOAD_MAX_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.S3,
        "subcategory": "limits",
        "description": "settingDesc.S3_INTERNAL_UPLOAD_MAX_SIZE",
        "default": 52428800,
        "depends_on": "S3_ENABLED",
    },
    # S3_PRESIGNED_URL_EXPIRES：为非公开存储桶生成的预签名下载 URL 的有效期（秒），
    # 默认 604800 秒（7 天），超时后链接失效需要重新生成
    "S3_PRESIGNED_URL_EXPIRES": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.S3,
        "subcategory": "limits",
        "description": "settingDesc.S3_PRESIGNED_URL_EXPIRES",
        "default": 604800,
        "depends_on": "S3_ENABLED",
    },
    # ============================================
    # File Upload Limits
    # ============================================
    # 用户上传文件的存储位置（当 S3 未启用或回退时）与按文件类型的大小/数量限制。
    # 注意：这里的 FILE_UPLOAD_MAX_SIZE_* 单位是 MB，与前面 S3_MAX_FILE_SIZE
    # （单位字节）不是同一套限制——这组是按“图片/视频/音频/文档”分类型的用户侧限制，
    # S3_MAX_FILE_SIZE 是存储后端层面的整体上限
    # LOCAL_STORAGE_PATH：未启用 S3（S3_ENABLED=False）时，文件保存到的本地磁盘目录
    "LOCAL_STORAGE_PATH": {
        "type": SettingType.STRING,
        "category": SettingCategory.FILE_UPLOAD,
        "subcategory": "storage",
        "description": "settingDesc.LOCAL_STORAGE_PATH",
        "default": "./uploads",
    },
    # ENABLE_LOCAL_FILESYSTEM_FALLBACK：当从存储后端（如 S3）下载文件失败时，
    # 是否允许 reveal/download 流程回退到服务器本地文件系统上查找文件
    "ENABLE_LOCAL_FILESYSTEM_FALLBACK": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.FILE_UPLOAD,
        "subcategory": "storage",
        "description": "settingDesc.ENABLE_LOCAL_FILESYSTEM_FALLBACK",
        "default": True,
    },
    # FILE_UPLOAD_MAX_SIZE_IMAGE：单个图片文件上传大小上限（MB）
    "FILE_UPLOAD_MAX_SIZE_IMAGE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.FILE_UPLOAD,
        "subcategory": "limits",
        "description": "settingDesc.FILE_UPLOAD_MAX_SIZE_IMAGE",
        "default": 40,
    },
    # FILE_UPLOAD_MAX_SIZE_VIDEO：单个视频文件上传大小上限（MB）
    "FILE_UPLOAD_MAX_SIZE_VIDEO": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.FILE_UPLOAD,
        "subcategory": "limits",
        "description": "settingDesc.FILE_UPLOAD_MAX_SIZE_VIDEO",
        "default": 100,
    },
    # FILE_UPLOAD_MAX_SIZE_AUDIO：单个音频文件上传大小上限（MB）
    "FILE_UPLOAD_MAX_SIZE_AUDIO": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.FILE_UPLOAD,
        "subcategory": "limits",
        "description": "settingDesc.FILE_UPLOAD_MAX_SIZE_AUDIO",
        "default": 50,
    },
    # FILE_UPLOAD_MAX_SIZE_DOCUMENT：单个文档文件上传大小上限（MB）
    "FILE_UPLOAD_MAX_SIZE_DOCUMENT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.FILE_UPLOAD,
        "subcategory": "limits",
        "description": "settingDesc.FILE_UPLOAD_MAX_SIZE_DOCUMENT",
        "default": 50,
    },
    # FILE_UPLOAD_MAX_FILES：单次上传操作允许携带的最大文件数量
    "FILE_UPLOAD_MAX_FILES": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.FILE_UPLOAD,
        "subcategory": "limits",
        "description": "settingDesc.FILE_UPLOAD_MAX_FILES",
        "default": 10,
    },
    # ============================================
    # Long-term Storage Settings (PostgreSQL)
    # ============================================
    # LangGraph 长期存储（Store，用于跨会话记忆等场景）使用的 PostgreSQL 连接配置；
    # 默认数据库名为 "langgraph"，与下面 Checkpoint 分组中的 PostgreSQL 配置是
    # 两套独立的连接（可以指向同一个实例，也可以分开部署）
    # ENABLE_POSTGRES_STORAGE：是否启用基于 PostgreSQL 的长期存储
    "ENABLE_POSTGRES_STORAGE": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.LONG_TERM_STORAGE,
        "subcategory": "connection",
        "description": "settingDesc.ENABLE_POSTGRES_STORAGE",
        "default": False,
        "frontend_visible": True,
    },
    # POSTGRES_HOST：长期存储 PostgreSQL 的主机地址
    "POSTGRES_HOST": {
        "type": SettingType.STRING,
        "category": SettingCategory.LONG_TERM_STORAGE,
        "subcategory": "connection",
        "description": "settingDesc.POSTGRES_HOST",
        "default": "localhost",
        "depends_on": "ENABLE_POSTGRES_STORAGE",
    },
    # POSTGRES_PORT：长期存储 PostgreSQL 的端口，默认 5432
    "POSTGRES_PORT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.LONG_TERM_STORAGE,
        "subcategory": "connection",
        "description": "settingDesc.POSTGRES_PORT",
        "default": 5432,
        "depends_on": "ENABLE_POSTGRES_STORAGE",
    },
    # POSTGRES_USER：长期存储 PostgreSQL 的登录用户名
    "POSTGRES_USER": {
        "type": SettingType.STRING,
        "category": SettingCategory.LONG_TERM_STORAGE,
        "subcategory": "connection",
        "description": "settingDesc.POSTGRES_USER",
        "default": "postgres",
        "depends_on": "ENABLE_POSTGRES_STORAGE",
    },
    # POSTGRES_PASSWORD：长期存储 PostgreSQL 的登录密码；is_sensitive=True，
    # 接口返回时会被脱敏
    "POSTGRES_PASSWORD": {
        "type": SettingType.STRING,
        "category": SettingCategory.LONG_TERM_STORAGE,
        "subcategory": "connection",
        "description": "settingDesc.POSTGRES_PASSWORD",
        "default": "postgres",
        "is_sensitive": True,
        "depends_on": "ENABLE_POSTGRES_STORAGE",
    },
    # POSTGRES_DB：长期存储使用的数据库名，默认 "langgraph"
    "POSTGRES_DB": {
        "type": SettingType.STRING,
        "category": SettingCategory.LONG_TERM_STORAGE,
        "subcategory": "connection",
        "description": "settingDesc.POSTGRES_DB",
        "default": "langgraph",
        "depends_on": "ENABLE_POSTGRES_STORAGE",
    },
    # POSTGRES_POOL_MIN_SIZE：连接池保持的最小连接数
    "POSTGRES_POOL_MIN_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.LONG_TERM_STORAGE,
        "subcategory": "pool",
        "description": "settingDesc.POSTGRES_POOL_MIN_SIZE",
        "default": 2,
        "depends_on": "ENABLE_POSTGRES_STORAGE",
    },
    # POSTGRES_POOL_MAX_SIZE：连接池允许的最大连接数，高并发场景下可适当调大
    "POSTGRES_POOL_MAX_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.LONG_TERM_STORAGE,
        "subcategory": "pool",
        "description": "settingDesc.POSTGRES_POOL_MAX_SIZE",
        "default": 10,
        "depends_on": "ENABLE_POSTGRES_STORAGE",
    },
    # ============================================
    # Checkpoint Backend Settings
    # ============================================
    # Checkpoint 是 LangGraph 用于持久化对话运行状态（消息历史、中间步骤等）的机制；
    # 默认使用 MongoDB，但单个 MongoDB 文档有 16MB 大小限制，长对话/大状态场景下
    # 容易超限，因此提供 postgres 作为可选后端。下面这些 CHECKPOINT_PG_* 均使用
    # dict 形式的 depends_on（{"key": ..., "value": ...}），表示仅当
    # CHECKPOINT_BACKEND 恰好等于 "postgres" 时才在前端可见/生效，
    # 与前面"父设置只需为真值"的字符串形式 depends_on 不同。
    # 另外，HOST/USER/PASSWORD/DB 四项在留空时会由
    # Settings.checkpoint_postgres_url（src/kernel/config/base.py）通过
    # `CHECKPOINT_PG_X or POSTGRES_X` 的写法回退到长期存储分组的通用 POSTGRES_*
    # 配置，从而可以与长期存储共用同一个 PostgreSQL 实例；但 PORT、
    # POOL_MIN_SIZE、POOL_MAX_SIZE 不参与回退，始终直接使用自身取值
    # （它们默认值与 POSTGRES_* 相同只是数值上的巧合）。
    # CHECKPOINT_BACKEND：选择 checkpoint 持久化后端，mongodb 或 postgres；
    # 该项在 RESTART_REQUIRED_SETTINGS 中，因为连接池是进程启动时一次性建立的，
    # 运行期修改不会生效，需要重启服务
    "CHECKPOINT_BACKEND": {
        "type": SettingType.SELECT,
        "category": SettingCategory.CHECKPOINT,
        "subcategory": "general",
        "description": "settingDesc.CHECKPOINT_BACKEND",
        "default": "mongodb",
        "options": ["mongodb", "postgres"],
        "frontend_visible": True,
    },
    # CHECKPOINT_PG_HOST：Checkpoint 专用 PostgreSQL 主机地址；留空时回退到 POSTGRES_HOST
    "CHECKPOINT_PG_HOST": {
        "type": SettingType.STRING,
        "category": SettingCategory.CHECKPOINT,
        "subcategory": "postgres",
        "description": "settingDesc.CHECKPOINT_PG_HOST",
        "default": "",
        "depends_on": {"key": "CHECKPOINT_BACKEND", "value": "postgres"},
    },
    # CHECKPOINT_PG_PORT：Checkpoint 专用 PostgreSQL 端口；不参与回退，始终直接生效
    "CHECKPOINT_PG_PORT": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.CHECKPOINT,
        "subcategory": "postgres",
        "description": "settingDesc.CHECKPOINT_PG_PORT",
        "default": 5432,
        "depends_on": {"key": "CHECKPOINT_BACKEND", "value": "postgres"},
    },
    # CHECKPOINT_PG_USER：Checkpoint 专用 PostgreSQL 用户名；留空时回退到 POSTGRES_USER
    "CHECKPOINT_PG_USER": {
        "type": SettingType.STRING,
        "category": SettingCategory.CHECKPOINT,
        "subcategory": "postgres",
        "description": "settingDesc.CHECKPOINT_PG_USER",
        "default": "",
        "depends_on": {"key": "CHECKPOINT_BACKEND", "value": "postgres"},
    },
    # CHECKPOINT_PG_PASSWORD：Checkpoint 专用 PostgreSQL 密码；留空时回退到
    # POSTGRES_PASSWORD；is_sensitive=True，接口返回时会被脱敏
    "CHECKPOINT_PG_PASSWORD": {
        "type": SettingType.STRING,
        "category": SettingCategory.CHECKPOINT,
        "subcategory": "postgres",
        "description": "settingDesc.CHECKPOINT_PG_PASSWORD",
        "default": "",
        "is_sensitive": True,
        "depends_on": {"key": "CHECKPOINT_BACKEND", "value": "postgres"},
    },
    # CHECKPOINT_PG_DB：Checkpoint 专用 PostgreSQL 数据库名；留空时回退到 POSTGRES_DB
    "CHECKPOINT_PG_DB": {
        "type": SettingType.STRING,
        "category": SettingCategory.CHECKPOINT,
        "subcategory": "postgres",
        "description": "settingDesc.CHECKPOINT_PG_DB",
        "default": "",
        "depends_on": {"key": "CHECKPOINT_BACKEND", "value": "postgres"},
    },
    # CHECKPOINT_PG_POOL_MIN_SIZE：Checkpoint 连接池最小连接数；不参与回退，始终直接生效
    "CHECKPOINT_PG_POOL_MIN_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.CHECKPOINT,
        "subcategory": "postgres",
        "description": "settingDesc.CHECKPOINT_PG_POOL_MIN_SIZE",
        "default": 2,
        "depends_on": {"key": "CHECKPOINT_BACKEND", "value": "postgres"},
    },
    # CHECKPOINT_PG_POOL_MAX_SIZE：Checkpoint 连接池最大连接数；不参与回退，始终直接生效
    "CHECKPOINT_PG_POOL_MAX_SIZE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.CHECKPOINT,
        "subcategory": "postgres",
        "description": "settingDesc.CHECKPOINT_PG_POOL_MAX_SIZE",
        "default": 10,
        "depends_on": {"key": "CHECKPOINT_BACKEND", "value": "postgres"},
    },
    # ============================================
    # User Management Settings
    # ============================================
    # DEFAULT_USER_ROLE：新用户注册时自动分配的默认角色，决定其初始权限范围
    "DEFAULT_USER_ROLE": {
        "type": SettingType.STRING,
        "category": SettingCategory.USER,
        "subcategory": "registration",
        "description": "settingDesc.DEFAULT_USER_ROLE",
        "default": "user",
    },
    # ENABLE_REGISTRATION：是否允许用户自主注册；关闭后新账号只能由管理员创建
    "ENABLE_REGISTRATION": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.USER,
        "subcategory": "registration",
        "description": "settingDesc.ENABLE_REGISTRATION",
        "default": True,
        "frontend_visible": True,
    },
    # ============================================
    # OAuth Settings
    # ============================================
    # 第三方登录（OAuth2）设置，按 Google/GitHub/Apple 三个提供方分组。
    # Google、GitHub 结构一致：ENABLED 总开关 + CLIENT_ID + CLIENT_SECRET
    # （标准 OAuth2 静态密钥，is_sensitive=True）。Apple 的"Sign in with Apple"
    # 机制不同：它的 client secret 不是一个静态字符串，而是需要在运行时用
    # 私钥现签的 ES256 JWT（有效期 180 天），因此 Apple 分组额外多出
    # TEAM_ID 和 KEY_ID 两项；实现见 src/infra/auth/oauth.py 的
    # _build_apple_client_secret()
    # OAUTH_GOOGLE_ENABLED：是否启用 Google 登录
    "OAUTH_GOOGLE_ENABLED": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.OAUTH,
        "subcategory": "google",
        "description": "settingDesc.OAUTH_GOOGLE_ENABLED",
        "default": False,
        "frontend_visible": True,
    },
    # OAUTH_GOOGLE_CLIENT_ID：Google OAuth 应用的 Client ID
    "OAUTH_GOOGLE_CLIENT_ID": {
        "type": SettingType.STRING,
        "category": SettingCategory.OAUTH,
        "subcategory": "google",
        "description": "settingDesc.OAUTH_GOOGLE_CLIENT_ID",
        "default": "",
        "depends_on": "OAUTH_GOOGLE_ENABLED",
    },
    # OAUTH_GOOGLE_CLIENT_SECRET：Google OAuth 应用的 Client Secret；
    # is_sensitive=True，接口返回时会被脱敏
    "OAUTH_GOOGLE_CLIENT_SECRET": {
        "type": SettingType.STRING,
        "category": SettingCategory.OAUTH,
        "subcategory": "google",
        "description": "settingDesc.OAUTH_GOOGLE_CLIENT_SECRET",
        "default": "",
        "depends_on": "OAUTH_GOOGLE_ENABLED",
        "is_sensitive": True,
    },
    # OAUTH_GITHUB_ENABLED：是否启用 GitHub 登录
    "OAUTH_GITHUB_ENABLED": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.OAUTH,
        "subcategory": "github",
        "description": "settingDesc.OAUTH_GITHUB_ENABLED",
        "default": False,
        "frontend_visible": True,
    },
    # OAUTH_GITHUB_CLIENT_ID：GitHub OAuth App 的 Client ID
    "OAUTH_GITHUB_CLIENT_ID": {
        "type": SettingType.STRING,
        "category": SettingCategory.OAUTH,
        "subcategory": "github",
        "description": "settingDesc.OAUTH_GITHUB_CLIENT_ID",
        "default": "",
        "depends_on": "OAUTH_GITHUB_ENABLED",
    },
    # OAUTH_GITHUB_CLIENT_SECRET：GitHub OAuth App 的 Client Secret；
    # is_sensitive=True，接口返回时会被脱敏
    "OAUTH_GITHUB_CLIENT_SECRET": {
        "type": SettingType.STRING,
        "category": SettingCategory.OAUTH,
        "subcategory": "github",
        "description": "settingDesc.OAUTH_GITHUB_CLIENT_SECRET",
        "default": "",
        "depends_on": "OAUTH_GITHUB_ENABLED",
        "is_sensitive": True,
    },
    # OAUTH_APPLE_ENABLED：是否启用 Apple 登录
    "OAUTH_APPLE_ENABLED": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.OAUTH,
        "subcategory": "apple",
        "description": "settingDesc.OAUTH_APPLE_ENABLED",
        "default": False,
        "frontend_visible": True,
    },
    # OAUTH_APPLE_CLIENT_ID：Apple 的 Services ID（不是 App 的 Bundle ID），
    # 同时用作 OAuth client_id、签发 client secret 时的 sub 声明，
    # 以及校验 Apple 返回 id_token 时的 aud
    "OAUTH_APPLE_CLIENT_ID": {
        "type": SettingType.STRING,
        "category": SettingCategory.OAUTH,
        "subcategory": "apple",
        "description": "settingDesc.OAUTH_APPLE_CLIENT_ID",
        "default": "",
        "depends_on": "OAUTH_APPLE_ENABLED",
    },
    # OAUTH_APPLE_CLIENT_SECRET：注意这里存放的并非静态密钥字符串，而是
    # Apple 私钥文件（.p8）的 PEM 文本内容，运行时结合 TEAM_ID/KEY_ID
    # 现场签发 ES256 JWT 作为真正的 client secret；is_sensitive=True
    "OAUTH_APPLE_CLIENT_SECRET": {
        "type": SettingType.STRING,
        "category": SettingCategory.OAUTH,
        "subcategory": "apple",
        "description": "settingDesc.OAUTH_APPLE_CLIENT_SECRET",
        "default": "",
        "depends_on": "OAUTH_APPLE_ENABLED",
        "is_sensitive": True,
    },
    # OAUTH_APPLE_TEAM_ID：Apple 开发者账号的 Team ID，签发 client secret JWT
    # 时填入 iss 声明
    "OAUTH_APPLE_TEAM_ID": {
        "type": SettingType.STRING,
        "category": SettingCategory.OAUTH,
        "subcategory": "apple",
        "description": "settingDesc.OAUTH_APPLE_TEAM_ID",
        "default": "",
        "depends_on": "OAUTH_APPLE_ENABLED",
    },
    # OAUTH_APPLE_KEY_ID：上述私钥对应的 Key ID，签发 client secret JWT 时
    # 填入 JWT header 的 kid 字段
    "OAUTH_APPLE_KEY_ID": {
        "type": SettingType.STRING,
        "category": SettingCategory.OAUTH,
        "subcategory": "apple",
        "description": "settingDesc.OAUTH_APPLE_KEY_ID",
        "default": "",
        "depends_on": "OAUTH_APPLE_ENABLED",
    },
    # ============================================
    # Memory Settings (Master Switch)
    # ============================================
    # 原生记忆（Native Memory）：跨会话的长期记忆子系统，实现见
    # src/infra/memory/client/native/。其核心流程是从对话中提取/合并记忆条目、
    # 存入向量库，并在后续会话检索召回、注入到系统提示或上下文中，
    # 使 Agent 能"记住"用户的偏好、事实等信息。下面 Embedding、
    # Search & Index、Storage & Policy 三个分组的设置均依赖本开关
    # （ENABLE_MEMORY）才会生效。
    # ENABLE_MEMORY：是否启用原生记忆功能总开关
    "ENABLE_MEMORY": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.MEMORY,
        "subcategory": "general",
        "description": "settingDesc.ENABLE_MEMORY",
        "default": False,
        "frontend_visible": True,
    },
    # ============================================
    # Memory Embedding Settings
    # ============================================
    # 记忆检索依赖向量相似度搜索，这里配置生成记忆向量所用的 Embedding 模型/API。
    # 与下面 Storage & Policy 分组中已废弃的 NATIVE_MEMORY_API_BASE/API_KEY 不同，
    # 这两个字段目前仍被实际读取（见 _setup_embedding_fn），但两者缺一即视为未配置，
    # 此时不会报错，而是退化为不带向量检索的纯文本匹配模式
    # NATIVE_MEMORY_EMBEDDING_API_BASE：Embedding 服务的 API Base URL
    "NATIVE_MEMORY_EMBEDDING_API_BASE": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_EMBEDDING,
        "subcategory": "api",
        "description": "settingDesc.NATIVE_MEMORY_EMBEDDING_API_BASE",
        "default": "",
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_EMBEDDING_API_KEY：Embedding 服务的 API Key；
    # is_sensitive=True，接口返回时会被脱敏
    "NATIVE_MEMORY_EMBEDDING_API_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_EMBEDDING,
        "subcategory": "api",
        "description": "settingDesc.NATIVE_MEMORY_EMBEDDING_API_KEY",
        "default": "",
        "is_sensitive": True,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_EMBEDDING_MODEL：使用的 Embedding 模型名，
    # 默认 "text-embedding-3-small"
    "NATIVE_MEMORY_EMBEDDING_MODEL": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_EMBEDDING,
        "subcategory": "api",
        "description": "settingDesc.NATIVE_MEMORY_EMBEDDING_MODEL",
        "default": "text-embedding-3-small",
        "depends_on": "ENABLE_MEMORY",
    },
    # ============================================
    # Memory Search & Index Settings
    # ============================================
    # 记忆检索与索引相关设置：控制记忆索引是否注入系统提示、检索候选的重排序
    # （rerank）行为，以及各类文本长度上限（用于控制注入上下文的 token 消耗
    # 或单次处理的内容规模）。
    # NATIVE_MEMORY_INDEX_ENABLED：是否将记忆索引摘要注入系统提示，
    # 使 Agent 在对话开始时就能感知已有哪些记忆条目
    "NATIVE_MEMORY_INDEX_ENABLED": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "index",
        "description": "settingDesc.NATIVE_MEMORY_INDEX_ENABLED",
        "default": True,
        "depends_on": "ENABLE_MEMORY",
        "frontend_visible": True,
    },
    # NATIVE_MEMORY_INDEX_CACHE_TTL：记忆索引缓存的存活时间（秒），
    # 避免频繁请求时重复构建索引
    "NATIVE_MEMORY_INDEX_CACHE_TTL": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "index",
        "description": "settingDesc.NATIVE_MEMORY_INDEX_CACHE_TTL",
        "default": 300,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_RERANK_MODEL：用于对检索到的记忆候选做重排序的模型；
    # 留空时不会报错也不会跳过重排序，而是改用内置的本地启发式打分
    # （local_rerank，见 src/infra/memory/client/native/search.py）
    "NATIVE_MEMORY_RERANK_MODEL": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "rerank",
        "description": "settingDesc.NATIVE_MEMORY_RERANK_MODEL",
        "default": "",
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_RERANK_API_BASE：rerank 模型的 API Base URL
    "NATIVE_MEMORY_RERANK_API_BASE": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "rerank",
        "description": "settingDesc.NATIVE_MEMORY_RERANK_API_BASE",
        "default": "",
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_RERANK_API_KEY：rerank 模型的 API Key；
    # is_sensitive=True，接口返回时会被脱敏
    "NATIVE_MEMORY_RERANK_API_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "rerank",
        "description": "settingDesc.NATIVE_MEMORY_RERANK_API_KEY",
        "default": "",
        "is_sensitive": True,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_APPEND_MAX_DETAILS：单次追加记忆时携带的明细条数上限
    "NATIVE_MEMORY_APPEND_MAX_DETAILS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "limits",
        "description": "settingDesc.NATIVE_MEMORY_APPEND_MAX_DETAILS",
        "default": 8,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_MAX_TOKENS：记忆内容注入上下文时占用的 token 数上限，
    # 避免记忆本身挤占过多上下文窗口
    "NATIVE_MEMORY_MAX_TOKENS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "limits",
        "description": "settingDesc.NATIVE_MEMORY_MAX_TOKENS",
        "default": 2000,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_INLINE_CONTENT_MAX_CHARS：单条记忆内联展示（直接嵌入提示）
    # 内容的字符数上限
    "NATIVE_MEMORY_INLINE_CONTENT_MAX_CHARS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "limits",
        "description": "settingDesc.NATIVE_MEMORY_INLINE_CONTENT_MAX_CHARS",
        "default": 1200,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_IMPORT_TOTAL_CONTENT_MAX_CHARS：批量导入记忆时总内容的
    # 字符数上限；默认高达 200 万字符，属于防止异常输入拖垮系统的内部安全阈值，
    # 而非日常需要调整的参数，因此 frontend_visible=False，不在前端设置页面展示
    "NATIVE_MEMORY_IMPORT_TOTAL_CONTENT_MAX_CHARS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "limits",
        "description": "settingDesc.NATIVE_MEMORY_IMPORT_TOTAL_CONTENT_MAX_CHARS",
        "default": 2000000,
        "depends_on": "ENABLE_MEMORY",
        "frontend_visible": False,
    },
    # NATIVE_MEMORY_COMPACTION_CONTENT_MAX_CHARS：记忆压缩（compaction）
    # 任务单次处理内容的字符数上限
    "NATIVE_MEMORY_COMPACTION_CONTENT_MAX_CHARS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "limits",
        "description": "settingDesc.NATIVE_MEMORY_COMPACTION_CONTENT_MAX_CHARS",
        "default": 4000,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_CONSOLIDATION_INPUT_MAX_CHARS：记忆合并去重（consolidation）
    # 任务单次输入的字符数上限；同样属于内部调优参数，frontend_visible=False
    "NATIVE_MEMORY_CONSOLIDATION_INPUT_MAX_CHARS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_SEARCH,
        "subcategory": "limits",
        "description": "settingDesc.NATIVE_MEMORY_CONSOLIDATION_INPUT_MAX_CHARS",
        "default": 4000,
        "depends_on": "ENABLE_MEMORY",
        "frontend_visible": False,
    },
    # ============================================
    # Memory Storage & Policy Settings
    # ============================================
    # 记忆存储与策略相关设置：包含记忆提取/压缩所用的 LLM 配置（部分字段已废弃）、
    # 记忆在底层存储中的命名空间、陈旧/清理策略、召回阈值、各类批量操作的并发度，
    # 以及"自动压缩"、"自动捕获"两个后台任务的触发与调度参数。
    # NATIVE_MEMORY_MODEL：记忆提取/捕获/合并（consolidation）阶段使用的模型 ID；
    # 留空则回退使用系统默认模型
    "NATIVE_MEMORY_MODEL": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "llm",
        "description": "settingDesc.NATIVE_MEMORY_MODEL",
        "default": "",
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_COMPACTION_MODEL_ID：记忆压缩（compaction）阶段使用的模型 ID，
    # 需填入系统里已配置好的模型 ID；与上面 NATIVE_MEMORY_MODEL 是两个独立的
    # 模型选择，分别对应"提取/合并"与"压缩"两个不同阶段
    "NATIVE_MEMORY_COMPACTION_MODEL_ID": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "llm",
        "description": "settingDesc.NATIVE_MEMORY_COMPACTION_MODEL_ID",
        "default": "",
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_API_BASE：已废弃字段，当前代码中没有任何地方读取此值——
    # 模型的实际连接信息统一通过上面 NATIVE_MEMORY_MODEL /
    # NATIVE_MEMORY_COMPACTION_MODEL_ID 关联到系统的模型配置来获取；
    # 保留该字段仅为兼容旧版本部署，新环境无需填写
    "NATIVE_MEMORY_API_BASE": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "llm",
        "description": "settingDesc.NATIVE_MEMORY_API_BASE",
        "default": "",
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_API_KEY：同上，已废弃字段，当前无消费代码读取；
    # is_sensitive=True 仅为历史遗留
    "NATIVE_MEMORY_API_KEY": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "llm",
        "description": "settingDesc.NATIVE_MEMORY_API_KEY",
        "default": "",
        "is_sensitive": True,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_STORE_NAMESPACE：记忆正文在 LangGraph Store 中使用的
    # 命名空间前缀（实际 namespace 为该值与用户 ID 等组合而成），
    # 仅用于存放因过长被移出主文档的记忆正文
    "NATIVE_MEMORY_STORE_NAMESPACE": {
        "type": SettingType.STRING,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_STORE_NAMESPACE",
        "default": "memories",
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_STALENESS_DAYS：记忆条目超过多少天未活跃即视为"陈旧"；
    # 陈旧后并不会被删除或影响是否可检索，只是在召回结果中附加陈旧提示，
    # 并在系统提示注入的记忆索引里降低排序权重
    "NATIVE_MEMORY_STALENESS_DAYS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_STALENESS_DAYS",
        "default": 30,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_PRUNE_THRESHOLD：单位是"天"（距上次更新的天数），
    # 与上面的 STALENESS_DAYS 是两条独立通道——staleness 只影响检索端的展示/排序，
    # 而 prune 由后台合并去重任务用来判定是否物理删除长期零访问的自动记忆
    # （非用户手动保存、访问次数很低的记忆），是更彻底的清理策略
    "NATIVE_MEMORY_PRUNE_THRESHOLD": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_PRUNE_THRESHOLD",
        "default": 90,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_RECALL_MIN_SCORE：召回记忆时候选结果的最低分数阈值，
    # 低于该分数会被过滤；注意该分数并非总是向量余弦相似度——
    # 走文本检索命中时是数据库的文本匹配分，只有走向量检索命中时才是相似度分数
    "NATIVE_MEMORY_RECALL_MIN_SCORE": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_RECALL_MIN_SCORE",
        "default": 0.3,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_HYDRATE_CONCURRENCY："hydrate"指记忆正文因过长被移存到
    # Store 后，召回结果返回前把全文取回填充回记忆对象的操作；此值即该批量
    # 取回操作的并发数
    "NATIVE_MEMORY_HYDRATE_CONCURRENCY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_HYDRATE_CONCURRENCY",
        "default": 4,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_CONSOLIDATION_ENRICH_CONCURRENCY："enrich"指合并去重后
    # 对每条结果调用 LLM（或规则兜底）生成摘要/标题/标签的子步骤，
    # 紧跟在合并去重之后；此值即该子步骤的并发数
    "NATIVE_MEMORY_CONSOLIDATION_ENRICH_CONCURRENCY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_CONSOLIDATION_ENRICH_CONCURRENCY",
        "default": 4,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_CONTENT_DELETE_CONCURRENCY：合并去重过程中批量删除旧记忆
    # 在 Store 中的正文内容时的并发数
    "NATIVE_MEMORY_CONTENT_DELETE_CONCURRENCY": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_CONTENT_DELETE_CONCURRENCY",
        "default": 4,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_AUTO_COMPACT_ENABLED：是否启用后台自动压缩记忆任务
    # （无需人工触发，满足条件后自动执行）
    "NATIVE_MEMORY_AUTO_COMPACT_ENABLED": {
        "type": SettingType.BOOLEAN,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_AUTO_COMPACT_ENABLED",
        "default": True,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_AUTO_COMPACT_THRESHOLD：单个用户的非手动记忆条数达到该阈值时，
    # 即触发一次自动压缩
    "NATIVE_MEMORY_AUTO_COMPACT_THRESHOLD": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_AUTO_COMPACT_THRESHOLD",
        "default": 40,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_AUTO_COMPACT_INTERVAL_SECONDS：后台周期任务扫描全体用户、
    # 检查是否需要自动压缩的调度间隔（秒），默认 43200 秒（12 小时）；
    # 同时也作为分布式扫描锁的存活时间
    "NATIVE_MEMORY_AUTO_COMPACT_INTERVAL_SECONDS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_AUTO_COMPACT_INTERVAL_SECONDS",
        "default": 43200,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_AUTO_COMPACT_MIN_INTERVAL_SECONDS：同一用户两次压缩尝试之间
    # 强制的最短冷却时间（秒），无论是写入后立即触发还是周期扫描触发都受此限制，
    # 避免压缩过于频繁地消耗资源
    "NATIVE_MEMORY_AUTO_COMPACT_MIN_INTERVAL_SECONDS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_AUTO_COMPACT_MIN_INTERVAL_SECONDS",
        "default": 900,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_AUTO_CAPTURE_INPUT_MAX_CHARS：自动捕获记忆场景下
    # （每轮对话结束后，后台异步从用户本轮输入中提炼沉淀长期记忆，
    # 区别于 Agent 主动调用记忆工具的显式保存），单次处理输入内容的字符数上限
    "NATIVE_MEMORY_AUTO_CAPTURE_INPUT_MAX_CHARS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_AUTO_CAPTURE_INPUT_MAX_CHARS",
        "default": 8000,
        "depends_on": "ENABLE_MEMORY",
    },
    # NATIVE_MEMORY_AUTO_CAPTURE_MAX_TASKS：自动捕获记忆后台任务的并发数上限，
    # 限制的是"不同用户"同时在跑的任务数量（同一用户任意时刻只会有一个任务在跑），
    # 而非任务队列容量
    "NATIVE_MEMORY_AUTO_CAPTURE_MAX_TASKS": {
        "type": SettingType.NUMBER,
        "category": SettingCategory.MEMORY_STORAGE,
        "subcategory": "policy",
        "description": "settingDesc.NATIVE_MEMORY_AUTO_CAPTURE_MAX_TASKS",
        "default": 8,
        "depends_on": "ENABLE_MEMORY",
    },
}

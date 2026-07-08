"""User-related schemas."""

# 本模块定义用户账号体系相关的数据模型：用户的增删改查视图、
# 数据库内部视图（含密码哈希/验证令牌等敏感字段）、认证相关的
# 登录/注册/找回密码请求，以及 JWT Token 的载荷与响应结构。
# 主要调用方：src/infra/user/manager.py、src/infra/user/storage.py
# （用户增删改查与持久化）、src/infra/auth/jwt.py（JWT 编解码）、
# src/infra/auth/oauth.py（第三方 OAuth 登录）、
# src/api/routes 下的认证与用户管理相关路由。
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.infra.utils.datetime import utc_now


# 支持的第三方 OAuth 登录方式
class OAuthProvider(str, Enum):
    """OAuth provider types."""

    # Google 登录
    GOOGLE = "google"
    # GitHub 登录
    GITHUB = "github"
    # Apple 登录
    APPLE = "apple"


# 用户模型的公共基础字段，供 UserCreate/User 等模型继承复用
class UserBase(BaseModel):
    """Base user schema."""

    # 用户名，登录与展示均可使用
    username: str = Field(..., min_length=1, max_length=50)
    # 邮箱地址，同时用于登录、找回密码、邮箱验证等场景
    email: EmailStr
    # 头像，直接以 data URI 形式内嵌存储（不落地到对象存储）
    avatar_url: Optional[str] = None  # Data URI for avatar (data:image/xxx;base64,...)
    # 第三方登录来源；通过用户名密码常规注册的账号该字段为 None
    oauth_provider: Optional[OAuthProvider] = None  # OAuth provider (google, github, apple)
    # 第三方登录在该 OAuth 提供商处的用户 ID，用于账号关联/去重
    oauth_id: Optional[str] = None  # OAuth provider user ID
    # 用户级偏好设置的自由字典（如界面语言、主题等），不做强类型校验
    metadata: Optional[dict] = None  # User preferences: { language, theme, ... }


# 创建用户的请求体（普通注册接口与管理员创建用户接口共用）
class UserCreate(UserBase):
    """Schema for creating a user."""

    # 登录密码（明文，服务端会做哈希后存储）；OAuth 登录创建的用户可不传密码
    password: Optional[str] = Field(None, min_length=6)  # Optional for OAuth users
    # 分配给该用户的角色名称列表（对应 schemas/role.py 中 Role 的 name），
    # 为空时由系统按"是否为首个用户"决定默认角色（首个用户为 admin，否则为 user）
    roles: List[str] = Field(default_factory=list)
    skip_verification: bool = False  # 跳过邮箱验证（管理员创建时使用）


# 更新用户的请求体：所有字段均为 Optional，仅传入需要修改的字段；
# extra="forbid" 表示传入未声明字段会直接报错（防止拼写错误的字段被静默忽略）。
# 既用于用户自助更新资料，也用于管理员/内部流程更新账号状态与各类令牌。
class UserUpdate(BaseModel):
    """Schema for updating a user."""

    model_config = ConfigDict(extra="forbid")

    # 新用户名
    username: Optional[str] = Field(None, min_length=1, max_length=50)
    # 新邮箱地址
    email: Optional[EmailStr] = None
    # 新密码（明文，服务端会做哈希后存储）
    password: Optional[str] = Field(None, min_length=6)
    avatar_url: Optional[str] = None  # Data URI for avatar (data:image/xxx;base64,...)
    # 覆盖该用户的角色列表
    roles: Optional[List[str]] = None
    # 是否启用该账号；置为 False 即禁止登录
    is_active: Optional[bool] = None
    oauth_provider: Optional[OAuthProvider] = None
    oauth_id: Optional[str] = None
    # 邮箱是否已验证
    email_verified: Optional[bool] = None
    # 邮箱验证令牌（发送验证邮件时生成，验证通过后应清空）
    verification_token: Optional[str] = None
    # 邮箱验证令牌的过期时间
    verification_token_expires: Optional[datetime] = None
    # 密码重置令牌（发起"忘记密码"时生成，重置完成后应清空）
    reset_token: Optional[str] = None
    # 密码重置令牌的过期时间
    reset_token_expires: Optional[datetime] = None


# 对外可见的用户视图（不含密码哈希等敏感字段），用于登录态用户信息、
# 用户列表等大多数 API 响应场景。
class User(UserBase):
    """User model (public view)."""

    # 用户唯一 ID
    id: str
    # 该用户拥有的角色名称列表
    roles: List[str] = Field(default_factory=list)
    # 由角色聚合出的权限标识列表（通常在登录/查询时由角色系统动态计算得到，
    # 而非直接存储字段）
    permissions: List[str] = Field(default_factory=list)
    # 账号是否处于启用状态；禁用后无法登录
    is_active: bool = True
    email_verified: bool = False  # 邮箱是否已验证
    # 账号创建时间
    created_at: datetime = Field(default_factory=utc_now)
    # 账号最近更新时间
    updated_at: datetime = Field(default_factory=utc_now)

    # 兼容 Pydantic v1 风格配置：允许直接从 ORM/对象属性构建模型实例
    class Config:
        from_attributes = True


# 注册接口的响应体
class RegisterResponse(BaseModel):
    """Registration response."""

    # 新注册的用户信息
    user: User
    requires_verification: bool  # 是否需要邮箱验证


# 分页查询用户列表的响应体
class UserListResponse(BaseModel):
    """Paginated user list response."""

    # 当前页的用户列表
    users: List[User]
    # 满足条件的用户总数
    total: int
    # 本次查询跳过的记录数（分页偏移量）
    skip: int
    # 本次查询返回的最大记录数
    limit: int
    # 是否还有更多数据未返回
    has_more: bool


# 数据库内部视图：在 User 公开视图之上附加了密码哈希与各类令牌等敏感字段，
# 仅供存储层/认证逻辑内部使用，绝不能直接作为 API 响应返回给客户端。
class UserInDB(User):
    """User model with sensitive data (database view)."""

    # 密码的哈希值（不存储明文密码）
    password_hash: str
    verification_token: Optional[str] = None  # 邮箱验证令牌
    verification_token_expires: Optional[datetime] = None  # 邮箱验证令牌过期时间
    reset_token: Optional[str] = None  # 密码重置令牌
    reset_token_expires: Optional[datetime] = None  # 密码重置令牌过期时间


# JWT 解码后的载荷结构，由 src/infra/auth/jwt.py 的 verify_token() 构造。
# 注意：access token 实际只编码了 sub/exp/iat（用户名/角色/权限会在每次
# 请求时从数据库动态查询，而不信任 token 中的旧快照），因此解析 access
# token 时 username/roles/permissions 可能为空，仅 refresh token 会
# 额外携带 username。
class TokenPayload(BaseModel):
    """JWT Token payload."""

    sub: str  # user_id
    # 用户名（仅部分 token 类型会携带）
    username: str
    # 角色列表（仅部分 token 类型会携带）
    roles: List[str] = Field(default_factory=list)
    # 权限列表（仅部分 token 类型会携带）
    permissions: List[str] = Field(default_factory=list)
    # 过期时间
    exp: Optional[datetime] = None
    # 签发时间（issued at）
    iat: Optional[datetime] = None


# 登录/刷新成功后返回给客户端的令牌对
class Token(BaseModel):
    """Token response."""

    # 访问令牌，用于后续请求的身份认证（有效期较短）
    access_token: str
    # 刷新令牌，用于在访问令牌过期后换取新的访问令牌（有效期较长）
    refresh_token: str
    # 令牌类型，固定为 "bearer"（配合 HTTP Authorization: Bearer <token> 使用）
    token_type: str = "bearer"
    # 访问令牌的有效期（秒）
    expires_in: int


# 登录请求体
class LoginRequest(BaseModel):
    """Login request (supports username or email)."""

    username: str  # 可以是用户名或邮箱
    # 登录密码（明文，依赖传输层如 HTTPS 保护）
    password: str


# 忘记密码请求体：提交邮箱后系统会发送重置密码邮件
class ForgotPasswordRequest(BaseModel):
    """忘记密码请求."""

    email: EmailStr


# 重置密码请求体：携带找回密码邮件中的令牌以及新密码
class ResetPasswordRequest(BaseModel):
    """重置密码请求."""

    # 密码重置令牌（对应 UserInDB.reset_token）
    token: str
    # 新密码
    new_password: str = Field(..., min_length=6)


# 验证邮箱请求体：携带验证邮件中的令牌完成邮箱验证
class VerifyEmailRequest(BaseModel):
    """验证邮箱请求."""

    # 邮箱验证令牌（对应 UserInDB.verification_token）
    token: str


# 重发验证邮件请求体：用户未收到或验证令牌过期时可重新申请发送
class ResendVerificationRequest(BaseModel):
    """重发验证邮件请求."""

    email: EmailStr

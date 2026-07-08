"""
Core authentication routes (register, login, refresh, me, permissions)
"""

# FastAPI 路由与异常/状态码工具
from fastapi import APIRouter, Depends, HTTPException, Request, status

# 依赖：从请求中解析并要求已登录用户（无有效 token 则直接 401）
from src.api.deps import get_current_user_required
# JWT 相关：签发 access/refresh token、解码校验 token
from src.infra.auth.jwt import create_access_token, create_refresh_token, decode_token
# Cloudflare Turnstile 人机验证服务（防机器人刷注册/登录）
from src.infra.auth.turnstile import get_turnstile_service
from src.infra.logging import get_logger
# 用户业务管理器：封装注册/登录/查询等用户领域逻辑
from src.infra.user.manager import UserManager
from src.kernel.config import settings
from src.kernel.exceptions import ValidationError
# 权限相关的响应模型与构造函数
from src.kernel.schemas.permission import PermissionsResponse, get_permissions_response
# 用户相关的请求/响应数据模型（Pydantic）
from src.kernel.schemas.user import (
    LoginRequest,
    RegisterResponse,
    Token,
    TokenPayload,
    User,
    UserCreate,
    UserUpdate,
)

from .utils import _get_client_ip, _get_frontend_url, _get_language

router = APIRouter()
logger = get_logger(__name__)


# POST /register —— 用户注册接口
# 请求体：UserCreate（用户名/邮箱/密码等）；响应体：RegisterResponse（新用户 + 是否需要邮箱验证）
# 约束：受 ENABLE_REGISTRATION 开关控制；可选 Turnstile 人机验证
# 副作用：若开启邮箱验证，会写入验证令牌并发送验证邮件
@router.post("/register", response_model=RegisterResponse)
async def register(user_data: UserCreate, request: Request):
    """用户注册"""
    # 检查是否允许注册
    if not settings.ENABLE_REGISTRATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="注册已关闭",
        )

    # Turnstile 验证
    turnstile_service = get_turnstile_service()
    # 仅当配置要求"注册时校验"才执行；令牌由前端放在 X-Turnstile-Token 请求头中
    if turnstile_service.require_on_register:
        turnstile_token = request.headers.get("X-Turnstile-Token")
        client_ip = _get_client_ip(request)
        if not await turnstile_service.verify(turnstile_token, client_ip):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="人机验证失败，请重试",
            )

    manager = UserManager()
    try:
        # 执行注册：创建用户（内部会做用户名/邮箱查重、密码哈希等）
        user = await manager.register(user_data)

        # 如果要求邮箱验证，发送验证邮件
        requires_verification = settings.REQUIRE_EMAIL_VERIFICATION
        if requires_verification:
            from src.infra.email import get_email_service

            email_service = await get_email_service()
            if email_service.is_enabled():
                # 生成验证令牌（24小时有效期）
                verify_token = email_service.generate_token()
                verify_token_expires = email_service.get_token_expiry(hours=24)

                # 更新用户的验证令牌
                from src.infra.user.storage import UserStorage

                storage = UserStorage()
                # 把验证令牌与过期时间写回用户记录，供后续 /verify-email 校验使用
                await storage.update(
                    user.id,
                    UserUpdate(
                        verification_token=verify_token,
                        verification_token_expires=verify_token_expires,
                    ),
                )

                # 发送验证邮件
                frontend_url = _get_frontend_url(request)
                lang = _get_language(request)
                await email_service.send_verification_email(
                    to_email=user.email,
                    username=user.username,
                    verify_token=verify_token,
                    base_url=frontend_url,
                    lang=lang,
                )
                logger.info(
                    "[Auth] Verification email sent to %s for new user %s",
                    user.email,
                    user.username,
                )
            else:
                logger.warning("[Auth] Email verification required but email service not enabled")

        # 返回新用户信息，并告知前端是否还需完成邮箱验证（未验证时通常不允许登录）
        return RegisterResponse(user=user, requires_verification=requires_verification)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# POST /login —— 用户登录接口
# 请求体：LoginRequest（用户名 + 密码）；响应体：Token（access_token / refresh_token / 过期秒数）
# 关键逻辑：登录前可选 Turnstile 校验；凭证错误返回 401
# 特殊处理：将"邮箱未验证 / 账户未激活"两类异常转换为 403 并给出中文提示
@router.post("/login", response_model=Token)
async def login(credentials: LoginRequest, request: Request):
    """用户登录"""
    # Turnstile 验证
    turnstile_service = get_turnstile_service()
    # 仅当配置要求"登录时校验"才执行人机验证
    if turnstile_service.require_on_login:
        turnstile_token = request.headers.get("X-Turnstile-Token")
        client_ip = _get_client_ip(request)
        if not await turnstile_service.verify(turnstile_token, client_ip):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="人机验证失败，请重试",
            )

    manager = UserManager()
    try:
        # 校验用户名/密码，成功返回已签发的 Token，失败返回 None
        token = await manager.login(credentials.username, credentials.password)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户名或密码错误",
            )
        return token
    except Exception as e:
        # 登录可能抛出领域异常，这里按异常类名/消息文本判别并转成对应的 HTTP 状态码
        # 处理邮箱未验证错误
        if "EmailNotVerifiedError" in type(e).__name__ or "请先验证邮箱" in str(e):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="请先验证邮箱后再登录",
            )
        # 处理账户未激活错误
        if "AccountNotActiveError" in type(e).__name__ or "账户未激活" in str(e):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="账户未激活，请验证邮箱后登录",
            )
        raise


# POST /refresh —— 刷新令牌接口
# 请求体：JSON { "refresh_token": ... }；响应体：Token（新 access_token + 轮换后的 refresh_token）
# 关键逻辑：解码并校验 refresh token（type 必须为 refresh、用户仍存在），随后重新签发令牌
@router.post("/refresh", response_model=Token)
async def refresh_token(request: Request):
    """刷新令牌"""
    try:
        body = await request.json()
        token = body.get("refresh_token")
        if not token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="缺少刷新令牌",
            )

        # 解码并校验 refresh token 的签名与有效期，取出其载荷（payload）
        payload = decode_token(token)

        # 验证是否是 refresh token
        if payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的刷新令牌",
            )

        user_id = payload.get("sub")
        username = payload.get("username")

        if not user_id or not username:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的令牌内容",
            )

        # 获取用户信息以验证用户仍然存在
        manager = UserManager()
        user = await manager.get_user(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在",
            )

        # 生成新的 access token 和 refresh token（轮换 refresh token）
        # 轮换 refresh token：旧的用后作废，降低泄露后被长期滥用的风险
        access_token = create_access_token(user_id=user_id)
        new_refresh_token = create_refresh_token(
            user_id=user_id,
            username=username or user.username,
        )

        # expires_in 以秒为单位返回 access token 有效时长（配置的小时数 × 3600）
        return Token(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"无效的刷新令牌: {str(e)}",
        )


# GET /me —— 获取当前登录用户信息（含动态权限）
# 认证要求：依赖 get_current_user_required，需携带有效 access token
# 响应体：User；其中 permissions 采用 TokenPayload 中已动态解析的权限，而非数据库里的历史快照
@router.get("/me", response_model=User)
async def get_current_user_info(
    current_user: TokenPayload = Depends(get_current_user_required),
):
    """获取当前用户信息（包含动态权限）"""
    manager = UserManager()
    user = await manager.get_user(current_user.sub)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )
    # 使用 TokenPayload 中已经动态获取的权限
    user.permissions = current_user.permissions
    return user


# GET /permissions —— 获取所有可用权限的分组列表（无需认证）
# 响应体：PermissionsResponse；供前端动态渲染权限选择器
@router.get("/permissions", response_model=PermissionsResponse)
async def get_permissions():
    """
    获取所有可用权限列表

    返回按分组的权限列表，用于前端动态渲染权限选择器。
    此接口无需认证即可访问。
    """
    return get_permissions_response()

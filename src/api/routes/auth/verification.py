"""
Email verification and password reset routes
"""

# hashlib：用于在"用户不存在"分支执行一次等价哈希，抵御时序攻击（timing attack）
import hashlib
from datetime import timezone

from fastapi import APIRouter, HTTPException, Request, status

from src.infra.logging import get_logger
from src.infra.user.manager import UserManager
# utc_now：统一获取带时区的 UTC 当前时间，用于令牌过期比较
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.user import (
    ForgotPasswordRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    UserUpdate,
    VerifyEmailRequest,
)

from .rate_limiter import get_rate_limiter
from .utils import _get_client_ip, _get_frontend_url, _get_language

router = APIRouter()
logger = get_logger(__name__)


# POST /forgot-password —— 请求发送密码重置邮件
# 请求体：ForgotPasswordRequest（email）；无论邮箱是否注册都统一返回成功，防止邮箱枚举
# 限流：同一 IP 每小时最多 5 次、同一邮箱每小时最多 3 次（任一超限返回 429）
# 副作用：邮箱存在时写入重置令牌并发信；不存在时执行等价耗时操作以抵御时序攻击
@router.post("/forgot-password")
async def forgot_password(
    request_data: ForgotPasswordRequest,
    request: Request,
):
    """请求密码重置邮件

    发送包含重置链接的邮件到用户邮箱。
    限流：每个 IP 每小时最多 5 次，每个邮箱每小时最多 3 次。
    """
    from src.infra.email import get_email_service

    email = request_data.email
    client_ip = _get_client_ip(request)

    # Rate limiting
    limiter = get_rate_limiter()

    # IP-based rate limit (5 per hour)
    # 第一层：按客户端 IP 限流，key 形如 ratelimit:forgot-password:ip:<ip>，窗口 3600 秒内最多 5 次
    ip_key = limiter.build_key("ratelimit:forgot-password:ip", client_ip)
    ip_allowed, _ = await limiter.check_rate_limit(ip_key, max_requests=5, window_seconds=3600)
    if not ip_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
        )

    # Email-based rate limit (3 per hour)
    # 第二层：按目标邮箱限流，防止单一邮箱被高频轰炸，窗口 3600 秒内最多 3 次
    email_key = limiter.build_key("ratelimit:forgot-password:email", email)
    email_allowed, _ = await limiter.check_rate_limit(
        email_key, max_requests=3, window_seconds=3600
    )
    if not email_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="该邮箱请求过于频繁，请稍后再试",
        )

    email_service = await get_email_service()
    if not email_service.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="邮件服务未启用",
        )

    manager = UserManager()

    # 获取用户（无论用户是否存在，都返回成功响应以防止邮箱枚举）
    user = await manager.storage.get_by_email(email)

    if user:
        # 生成重置令牌
        reset_token = email_service.generate_token()
        reset_expires = email_service.get_token_expiry()

        # 更新用户的重置令牌
        await manager.storage.update(
            user.id,
            UserUpdate(
                reset_token=reset_token,
                reset_token_expires=reset_expires,
            ),
        )

        # 发送重置邮件
        frontend_url = _get_frontend_url(request)
        lang = _get_language(request)
        await email_service.send_password_reset_email(
            to_email=user.email,
            username=user.username,
            reset_token=reset_token,
            base_url=frontend_url,
            lang=lang,
        )
        logger.info("[Auth] Password reset email sent to %s", email)
    else:
        # 用户不存在时也执行相同操作以防止时序攻击
        # 生成令牌（但不保存或发送）
        _ = email_service.generate_token()
        _ = email_service.get_token_expiry()
        # 执行一次哈希计算以匹配密码重置的时间消耗
        hashlib.sha256(b"timing-resistant-dummy").hexdigest()
        logger.debug("[Auth] Password reset requested for non-existent email: %s", email)

    # 始终返回成功，防止邮箱枚举攻击
    return {"message": "如果邮箱已注册，您将收到密码重置邮件"}


# POST /reset-password —— 使用重置令牌设置新密码
# 请求体：ResetPasswordRequest（token + new_password）
# 校验：令牌无效或过期均返回 400；成功后更新密码并清空重置令牌（令牌一次性）
@router.post("/reset-password")
async def reset_password(request_data: ResetPasswordRequest):
    """重置密码

    使用重置令牌设置新密码。
    """
    token = request_data.token
    new_password = request_data.new_password

    manager = UserManager()

    # 通过重置令牌查找用户
    user = await manager.storage.get_by_reset_token(token)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的重置令牌",
        )

    # 检查令牌是否过期
    if user.reset_token_expires:
        expires_dt = user.reset_token_expires
        # 兼容存储中可能是"无时区(naive)"的时间：补上 UTC 时区后再比较，避免 aware/naive 混用报错
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        if utc_now() > expires_dt:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="重置令牌已过期",
            )

    # 更新密码并清除重置令牌
    await manager.storage.update(
        user.id,
        UserUpdate(
            password=new_password,
            reset_token=None,
            reset_token_expires=None,
        ),
    )

    logger.info("[Auth] Password reset successful for user %s", user.username)

    return {"message": "密码重置成功"}


# POST /verify-email —— 用验证令牌验证邮箱并激活账户
# 请求体：VerifyEmailRequest（token）；令牌无效或过期返回 400（过期检查在存储层完成）
# 副作用：置 email_verified=True、is_active=True；无角色时赋予默认角色，并清空验证令牌
@router.post("/verify-email")
async def verify_email(request_data: VerifyEmailRequest):
    """验证邮箱

    使用验证令牌验证用户邮箱并激活账户。
    令牌过期检查已在 storage.get_by_verification_token 中处理。

    验证成功后：
    - 设置 email_verified=True
    - 设置 is_active=True（激活账户）
    - 如果用户没有角色，赋予默认角色（从 DEFAULT_USER_ROLE 读取）
    """
    token = request_data.token

    manager = UserManager()

    # 通过验证令牌查找用户（已包含过期检查）
    user = await manager.storage.get_by_verification_token(token)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效或过期的验证令牌",
        )

    # 如果用户已有角色（如第一个管理员用户），保留；否则赋予默认角色
    default_role = settings.DEFAULT_USER_ROLE or "user"
    if not user.roles:
        roles = [default_role]
    else:
        roles = user.roles

    # 更新用户状态：邮箱验证 + 账户激活
    await manager.storage.update(
        user.id,
        UserUpdate(
            email_verified=True,
            is_active=True,  # 激活账户
            roles=roles,  # 保留已有角色或赋予默认角色
            verification_token=None,
            verification_token_expires=None,
        ),
    )

    logger.info(
        "[Auth] Email verified and account activated for user %s with roles %s",
        user.username,
        ", ".join(roles) if roles else "none",
    )

    return {"message": "邮箱验证成功，账户已激活"}


# POST /resend-verification —— 重新发送邮箱验证邮件
# 请求体：ResendVerificationRequest（email）；无论邮箱是否存在/是否已验证都统一返回成功，防止枚举
# 限流：同一 IP 每小时最多 5 次、同一邮箱每小时最多 3 次（任一超限返回 429）
# 副作用：仅当用户存在且尚未验证时，才重新生成验证令牌（24h 有效）并发信
@router.post("/resend-verification")
async def resend_verification(
    request_data: ResendVerificationRequest,
    request: Request,
):
    """重发验证邮件

    重新发送邮箱验证邮件。
    限流：每个 IP 每小时最多 5 次，每个邮箱每小时最多 3 次。
    """
    from src.infra.email import get_email_service

    email = request_data.email
    client_ip = _get_client_ip(request)

    # Rate limiting
    limiter = get_rate_limiter()

    # IP-based rate limit (5 per hour)
    # 第一层：按客户端 IP 限流，key 形如 ratelimit:resend-verification:ip:<ip>，3600 秒内最多 5 次
    ip_key = limiter.build_key("ratelimit:resend-verification:ip", client_ip)
    ip_allowed, _ = await limiter.check_rate_limit(ip_key, max_requests=5, window_seconds=3600)
    if not ip_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
        )

    # Email-based rate limit (3 per hour)
    # 第二层：按目标邮箱限流，防止单一邮箱被高频轰炸，3600 秒内最多 3 次
    email_key = limiter.build_key("ratelimit:resend-verification:email", email)
    email_allowed, _ = await limiter.check_rate_limit(
        email_key, max_requests=3, window_seconds=3600
    )
    if not email_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="该邮箱请求过于频繁，请稍后再试",
        )

    email_service = await get_email_service()
    if not email_service.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="邮件服务未启用",
        )

    manager = UserManager()
    user = await manager.storage.get_by_email(email)

    # 仅对"存在且尚未验证"的用户重新发信；已验证或不存在则静默跳过（对外仍统一返回成功）
    if user and not user.email_verified:
        # 生成新的验证令牌（24小时有效期）
        verify_token = email_service.generate_token()
        verify_token_expires = email_service.get_token_expiry(hours=24)

        # 更新用户的验证令牌
        await manager.storage.update(
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
        logger.info("[Auth] Verification email resent to %s", email)

    # 始终返回成功，防止邮箱枚举攻击
    return {"message": "如果邮箱已注册且未验证，您将收到验证邮件"}

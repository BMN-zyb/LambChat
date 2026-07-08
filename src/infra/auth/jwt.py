"""
JWT Token 处理

提供 JWT token 的创建、验证和解码功能。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# PyJWT 库，负责 JWT 的编码（签名）与解码（验签）
import jwt

from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.exceptions import AuthenticationError
from src.kernel.schemas.user import TokenPayload


def create_access_token(
    user_id: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    创建访问令牌

    Args:
        user_id: 用户ID
        expires_delta: 过期时间增量

    Returns:
        JWT 访问令牌（用户信息从 API 动态获取）
    """
    # 未显式指定过期时长时，采用配置中的默认小时数
    if expires_delta is None:
        expires_delta = timedelta(hours=settings.ACCESS_TOKEN_EXPIRE_HOURS)

    # 统一使用 UTC 时间，避免时区偏差导致的过期判断错误
    now = utc_now()
    expire = now + expires_delta

    # 访问令牌 payload 只放最小信息：主体（用户ID）、过期时间、签发时间
    # 角色/权限等信息不写入 token，改由 API 请求时动态查询，便于权限即时生效
    payload = {
        "sub": user_id,
        "exp": expire,
        "iat": now,
    }

    # 使用配置的密钥与算法对 payload 进行签名，生成最终 token 字符串
    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_refresh_token(
    user_id: str,
    username: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    创建刷新令牌

    Args:
        user_id: 用户ID
        username: 用户名
        expires_delta: 过期时间增量

    Returns:
        JWT 刷新令牌
    """
    # 刷新令牌有效期通常远长于访问令牌，默认取配置的天数
    if expires_delta is None:
        expires_delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    now = utc_now()
    expire = now + expires_delta

    # 刷新令牌额外携带 username 与 type="refresh" 标记，
    # 便于在刷新流程中区分令牌类型、防止访问令牌被当作刷新令牌误用
    payload = {
        "sub": user_id,
        "username": username,
        "type": "refresh",
        "exp": expire,
        "iat": now,
    }

    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_token(token: str) -> Dict[str, Any]:
    """
    解码 JWT token

    Args:
        token: JWT token

    Returns:
        解码后的 payload

    Raises:
        AuthenticationError: token 无效或过期
    """
    try:
        # jwt.decode 内部会校验签名与 exp 过期时间，任一不符即抛异常
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return payload
    except jwt.ExpiredSignatureError:
        # 签名有效但已过期：转换为业务层统一的认证异常
        raise AuthenticationError("Token 已过期")
    except jwt.InvalidTokenError as e:
        # 签名错误、格式非法等其他所有无效情况
        raise AuthenticationError(f"无效的 Token: {str(e)}")


def verify_token(token: str) -> TokenPayload:
    """
    验证并解析 token

    Args:
        token: JWT token

    Returns:
        TokenPayload 对象

    Raises:
        AuthenticationError: token 无效或过期
    """
    # 先完成签名与过期校验，拿到原始 payload 字典
    payload = decode_token(token)

    # 验证必要字段存在
    # 防御式校验：即便签名合法，也要确保标准声明齐全，避免后续取值 KeyError
    if "sub" not in payload:
        raise AuthenticationError("Token 缺少 sub 字段")
    if "exp" not in payload:
        raise AuthenticationError("Token 缺少 exp 字段")
    if "iat" not in payload:
        raise AuthenticationError("Token 缺少 iat 字段")

    # 将原始字典转换为结构化的 TokenPayload；
    # roles/permissions 用 get 兜底为空列表（访问令牌不写入这些字段），
    # 时间戳按 UTC 还原为 datetime 对象
    return TokenPayload(
        sub=payload["sub"],
        username=payload.get("username", ""),
        roles=payload.get("roles", []),
        permissions=payload.get("permissions", []),
        exp=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        iat=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
    )

"""
Cloudflare Turnstile verification service
"""

from typing import Optional

import httpx

from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)

# Cloudflare Turnstile 服务端校验接口地址
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class TurnstileService:
    """Service for verifying Cloudflare Turnstile tokens"""

    # 单例引用：整个进程复用同一个服务实例
    _instance: Optional["TurnstileService"] = None

    def __init__(self) -> None:
        # 无状态服务，配置全部实时从 settings 读取
        pass

    @classmethod
    def get_instance(cls) -> "TurnstileService":
        """Get singleton instance"""
        # 懒加载单例
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def is_enabled(self) -> bool:
        """Check if Turnstile is enabled (reads fresh from settings)"""
        # 需同时开启开关且配置了密钥才算真正启用
        return settings.TURNSTILE_ENABLED and bool(settings.TURNSTILE_SECRET_KEY)

    @property
    def site_key(self) -> str:
        """Get the site key for frontend"""
        # 前端渲染 Turnstile 组件所需的公开 site key
        return settings.TURNSTILE_SITE_KEY

    @property
    def require_on_login(self) -> bool:
        """Check if Turnstile is required on login"""
        # 各场景开关均以“功能整体已启用”为前提
        return settings.TURNSTILE_REQUIRE_ON_LOGIN and self.is_enabled

    @property
    def require_on_register(self) -> bool:
        """Check if Turnstile is required on registration"""
        return settings.TURNSTILE_REQUIRE_ON_REGISTER and self.is_enabled

    @property
    def require_on_password_change(self) -> bool:
        """Check if Turnstile is required on password change"""
        return settings.TURNSTILE_REQUIRE_ON_PASSWORD_CHANGE and self.is_enabled

    async def verify(self, token: Optional[str], remote_ip: Optional[str] = None) -> bool:
        """
        Verify a Turnstile token

        Args:
            token: The token from the Turnstile widget
            remote_ip: Optional client IP for additional verification

        Returns:
            True if verification succeeds, False otherwise
        """
        # Always read fresh from settings
        # 未启用时直接放行（返回 True），使调用方无需分支处理
        if not settings.TURNSTILE_ENABLED:
            logger.debug("Turnstile is not enabled, skipping verification")
            return True

        # 启用但缺 token：视为校验失败
        if not token:
            logger.warning("Turnstile token is missing")
            return False

        # 启用但缺服务端密钥属配置错误，保守拒绝
        secret_key = settings.TURNSTILE_SECRET_KEY
        if not secret_key:
            logger.error("Turnstile secret key is not configured")
            return False

        try:
            async with httpx.AsyncClient() as client:
                # 向 Cloudflare 提交 secret + 前端 token 进行服务端二次校验
                data: dict[str, str] = {
                    "secret": secret_key,
                    "response": token,
                }
                # 可选携带客户端 IP，增强校验强度
                if remote_ip:
                    data["remoteip"] = remote_ip

                response = await client.post(
                    TURNSTILE_VERIFY_URL,
                    data=data,
                    timeout=10.0,
                )
                result = response.json()

                # success 为真才算通过
                if result.get("success"):
                    logger.debug("Turnstile verification successful")
                    return True
                else:
                    # 记录 Cloudflare 返回的错误码，便于定位失败原因
                    error_codes = result.get("error-codes", [])
                    logger.warning("Turnstile verification failed: %s", error_codes)
                    return False

        except httpx.TimeoutException:
            # 超时按失败处理，避免因网络问题误放行
            logger.error("Turnstile verification timed out")
            return False
        except Exception as e:
            # 其他异常同样保守判为失败
            logger.error("Turnstile verification error: %s", e)
            return False


def get_turnstile_service() -> TurnstileService:
    """Get the global TurnstileService instance"""
    # 对外暴露的获取单例入口
    return TurnstileService.get_instance()

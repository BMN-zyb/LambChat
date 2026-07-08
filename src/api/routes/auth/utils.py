"""
Utility functions for auth routes
"""

from urllib.parse import urlparse

from fastapi import Request

from src.infra.logging import get_logger
from src.kernel.config import settings

# 复用限流器里的 _safe_key_part 来清洗 state，拼接安全的 Redis key
from .rate_limiter import RateLimiter

logger = get_logger(__name__)


def _get_client_ip(request: Request) -> str:
    """Get client IP address from request, handling reverse proxies.

    Checks X-Forwarded-For header first (for reverse proxy setups),
    then falls back to direct client IP.

    Args:
        request: FastAPI request object

    Returns:
        Client IP address string
    """
    # Check X-Forwarded-For header (comma-separated list, first is original client)
    # 反向代理场景下真实客户端 IP 在 X-Forwarded-For 里，取逗号分隔的第一段（最左为最初客户端）
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # Take the first IP in the chain (original client)
        ips = [ip.strip() for ip in forwarded_for.split(",")]
        if ips:
            return ips[0]

    # Fall back to direct client IP
    if request.client:
        return request.client.host

    return "unknown"


# 发送邮件时支持的语言白名单；请求语言不在其中时回退到英文
SUPPORTED_EMAIL_LANGUAGES = {"en", "zh", "ja", "ko", "ru"}


def _get_language(request: Request) -> str:
    """Extract preferred language from Accept-Language header.

    Returns a 2-letter language code (e.g. 'en', 'zh').
    Falls back to 'en' if the header is missing or unsupported.
    """
    accept_lang = request.headers.get("accept-language", "en")
    # Take the first language code before any comma or semicolon
    # 取 Accept-Language 的首选项，去掉地区后缀（如 zh-CN -> zh）并转小写
    lang = accept_lang.split(",")[0].split("-")[0].strip().lower()
    return lang if lang in SUPPORTED_EMAIL_LANGUAGES else "en"


def _get_frontend_url(request: Request) -> str:
    """从请求中获取前端 URL

    优先使用代理透传的协议和主机信息来构造对外可见的前端 URL。
    - 开发环境：Vite 代理会设置 X-Forwarded-Host
    - 生产环境：Nginx 等代理至少应传递 Host / X-Forwarded-Proto
    """
    # URL 来源优先级：显式配置 APP_BASE_URL > 代理头(X-Forwarded-Host/Proto) > Origin/Referer > 请求 base_url
    configured_base_url = getattr(settings, "APP_BASE_URL", "").rstrip("/")
    if configured_base_url:
        return configured_base_url

    forwarded_host = request.headers.get("x-forwarded-host")
    host = (forwarded_host or request.headers.get("host") or "").split(",")[0].strip()
    if host:
        # 协议优先取代理透传的 X-Forwarded-Proto；缺失时本地地址默认 http、其余默认 https
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        scheme = forwarded_proto or (
            "http" if "localhost" in host or "127.0.0.1" in host else "https"
        )
        return f"{scheme}://{host}"

    # 其次使用 Origin 请求头（适用于 AJAX 请求）
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        # 提取 origin 部分 (scheme + host + port)
        parsed = urlparse(origin)
        return f"{parsed.scheme}://{parsed.netloc}"

    # 回退到请求的 base_url
    base_url = str(request.base_url)
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _store_oauth_state(provider: str, state: str, client_ip: str) -> None:
    """Store OAuth state in Redis for CSRF protection.

    Args:
        provider: OAuth provider name
        state: State token to store
        client_ip: Client IP address retained for backwards-compatible call sites
    """
    from src.infra.storage.redis import get_redis_client

    redis = get_redis_client()
    # Redis key：oauth:state:<provider>:<清洗后的 state>；用 _safe_key_part 过滤非法字符防注入
    key = f"oauth:state:{provider}:{RateLimiter._safe_key_part(state)}"
    # Store state with 10 minute expiry
    # 写入 state 并设 600 秒（10 分钟）过期：授权流程须在此窗口内完成，逾期作废
    await redis.setex(key, 600, state)


async def _verify_oauth_state(provider: str, state: str, client_ip: str) -> bool:
    """Verify OAuth state from Redis for CSRF protection.

    Args:
        provider: OAuth provider name
        state: State token to verify
        client_ip: Client IP address retained for backwards-compatible call sites

    Returns:
        True if state is valid, False otherwise
    """
    from src.infra.storage.redis import get_redis_client

    redis = get_redis_client()
    # 用与写入时完全相同的规则重建 key，才能命中之前存储的 state
    key = f"oauth:state:{provider}:{RateLimiter._safe_key_part(state)}"

    try:
        stored_state = await redis.get(key)
        if stored_state and stored_state == state:
            # Delete the state after successful verification (one-time use)
            # 校验通过后立即删除，保证 state 一次性使用（防止重放攻击）
            await redis.delete(key)
            return True
        return False
    except Exception as e:
        logger.error("[OAuth] Failed to verify state: %s", e)
        return False

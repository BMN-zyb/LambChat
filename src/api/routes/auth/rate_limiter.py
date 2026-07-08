"""
Rate limiting helper for auth routes
"""

import re

from src.infra.logging import get_logger
# Redis 客户端：限流计数存放在 Redis 中，便于多进程/多实例共享同一份限流状态
from src.infra.storage.redis import get_redis_client

logger = get_logger(__name__)


# 基于 Redis 的简易限流器（固定窗口计数法），用于登录/发码等接口防暴力破解与刷量
class RateLimiter:
    """Simple Redis-based rate limiter for email endpoints."""

    @staticmethod
    def _safe_key_part(value: str) -> str:
        """Sanitize value for use in Redis key to prevent injection.

        Args:
            value: Raw input value (IP or email)

        Returns:
            Safe string for Redis key (alphanumeric, dots, hyphens, @ only)
        """
        # 只保留安全字符：字母、数字、点、连字符、@、下划线
        return re.sub(r"[^a-zA-Z0-9.@_-]", "", value)[:100]

    @staticmethod
    def build_key(prefix: str, identifier: str) -> str:
        """Build a safe Redis key from prefix and identifier.

        Args:
            prefix: Key prefix (e.g., "ratelimit:forgot-password:ip")
            identifier: User-provided identifier (IP or email)

        Returns:
            Safe Redis key
        """
        safe_id = RateLimiter._safe_key_part(identifier)
        # 最终 key 形如 "<prefix>:<清洗后的标识符>"，例如 ratelimit:forgot-password:ip:1.2.3.4
        return f"{prefix}:{safe_id}"

    async def check_rate_limit(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        """Check if request is within rate limit.

        Args:
            key: Redis key for rate limiting (should be built with build_key())
            max_requests: Maximum requests allowed in window
            window_seconds: Time window in seconds

        Returns:
            Tuple of (is_allowed, remaining_requests)
        """
        try:
            redis = get_redis_client()
            # 读取该 key 当前窗口内的计数；返回 None 表示本窗口尚无请求
            current = await redis.get(key)

            if current is None:
                # 窗口内首次请求：写入计数 1 并设置过期时间 = window_seconds（这一刻即为固定窗口的起点）
                await redis.setex(key, window_seconds, 1)
                return True, max_requests - 1

            current_count = int(current)
            # 已达上限：拒绝请求，并读取剩余 TTL 记入日志便于排查
            if current_count >= max_requests:
                ttl = await redis.ttl(key)
                logger.warning("[RateLimiter] Rate limit exceeded for %s, TTL=%d", key, ttl)
                return False, 0

            # 未达上限：计数自增（注意不重置过期时间，因此过期时间仍以首次请求为准 = 固定窗口）
            await redis.incr(key)
            return True, max_requests - current_count - 1

        except Exception as e:
            # If Redis fails, allow the request (fail open)
            # Redis 故障时采用"失败放行"(fail open) 策略：宁可放过也不误伤正常用户
            logger.error("[RateLimiter] Redis error: %s", e)
            return True, max_requests

    async def close(self) -> None:
        """No-op: Redis client is managed by get_redis_client() singleton."""
        pass


# 进程内单例限流器实例（懒加载）：由 get_rate_limiter 创建、close_rate_limiter 释放
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get singleton rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


async def close_rate_limiter() -> None:
    """Release the singleton rate limiter without creating it during shutdown."""
    global _rate_limiter
    # 先取出再置空，避免在关闭流程中因访问而意外重新创建实例
    limiter = _rate_limiter
    _rate_limiter = None
    if limiter is not None:
        await limiter.close()

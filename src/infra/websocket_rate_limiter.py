"""WebSocket 速率限制（基于 Redis）"""

from redis.asyncio import Redis

from src.infra.logging import get_logger
from src.infra.storage.redis import create_redis_client

logger = get_logger(__name__)


class WebSocketRateLimiter:
    """WebSocket 连接速率限制器，仅对认证失败计数"""

    def __init__(self, max_failures: int = 5, window_seconds: int = 300):
        # max_failures: 滑动窗口内允许的最大认证失败次数，达到即封禁。
        # window_seconds: 计数窗口(秒)，首次失败时给 Redis 键设置该 TTL。
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        # 本限流器专用的隔离连接池 Redis 客户端(惰性创建)。
        self._redis: Redis | None = None

    def _get_redis(self):
        # 惰性创建隔离连接池的 Redis 客户端。
        if self._redis is None:
            self._redis = create_redis_client(isolated_pool=True)
        return self._redis

    async def check(self, client_ip: str) -> tuple[bool, int]:
        """
        检查 IP 是否被封禁（不修改计数）

        Returns:
            (是否允许连接, 剩余封禁时间秒数)
        """
        # 只读检查：按 IP 取当前失败计数。无记录则放行；达到阈值则返回剩余 TTL 作为封禁时长。
        key = f"ws:auth:fail:{client_ip}"
        redis = self._get_redis()
        count_str = await redis.get(key)
        if count_str is None:
            return True, 0
        count = int(count_str)
        if count >= self.max_failures:
            ttl = await redis.ttl(key)
            return False, max(ttl, 0)
        return True, 0

    async def record_failure(self, client_ip: str) -> tuple[bool, int]:
        """
        记录一次认证失败

        Returns:
            (是否应该封禁, 当前失败次数)
        """
        # 计数 +1；首次计数(count==1)时才设置窗口 TTL，使窗口从第一次失败起算。
        key = f"ws:auth:fail:{client_ip}"
        redis = self._get_redis()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, self.window_seconds)
        should_block = count >= self.max_failures
        if should_block:
            logger.warning(f"[WS] IP {client_ip} blocked after {count} failures")
        return should_block, count

    async def reset(self, client_ip: str) -> None:
        """认证成功时重置失败计数"""
        # 删除该 IP 的失败计数键，认证成功后清零。
        await self._get_redis().delete(f"ws:auth:fail:{client_ip}")

    async def close(self) -> None:
        """Close the dedicated Redis client used by this limiter."""
        # 先置空引用再关闭，避免关闭过程中被并发复用；关闭失败仅告警。
        redis = self._redis
        self._redis = None
        if redis is None:
            return
        try:
            await redis.aclose()
        except Exception as e:
            logger.warning("[WS] Failed to close rate limiter Redis client: %s", e)


# 进程级单例限流器。
_limiter: WebSocketRateLimiter | None = None


def get_ws_rate_limiter() -> WebSocketRateLimiter:
    # 惰性获取(必要时创建)限流器单例。
    global _limiter
    if _limiter is None:
        _limiter = WebSocketRateLimiter()
    return _limiter


async def close_ws_rate_limiter() -> None:
    # 关闭并释放已存在的限流器单例(未创建则不新建)。
    global _limiter
    limiter = _limiter
    _limiter = None
    if limiter is not None:
        await limiter.close()

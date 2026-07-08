"""把 LambChat 的 Redis 配置转换为 arq 所需的 RedisSettings。

arq worker/连接池需要 host/port/db/账号密码等结构化字段，而项目里 Redis 以
单一 REDIS_URL 形式配置，因此这里负责解析 URL 并对接 arq。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlparse

from arq.connections import RedisSettings


# 从 settings.REDIS_URL 解析出 arq 所需的连接参数。
# 关键点：
#   - 路径段（/0、/1...）解析为库号 database，缺省为 0；
#   - 密码优先取显式的 settings.REDIS_PASSWORD，否则回退到 URL 里的密码；
#   - URL 里的账号/密码可能被百分号编码，需 unquote 还原；
#   - scheme 为 rediss 时启用 TLS。
def build_arq_redis_settings(settings: Any) -> RedisSettings:
    """Build arq Redis settings from LambChat's Redis configuration."""
    parsed = urlparse(settings.REDIS_URL)
    database = 0
    if parsed.path and parsed.path != "/":
        database = int(parsed.path.lstrip("/"))

    password = settings.REDIS_PASSWORD or (unquote(parsed.password) if parsed.password else None)
    username = unquote(parsed.username) if parsed.username else None

    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=database,
        username=username,
        password=password,
        ssl=parsed.scheme == "rediss",
    )

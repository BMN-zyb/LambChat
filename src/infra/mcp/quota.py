"""Redis-backed per-user usage quotas for system MCP servers."""

# 使用 __future__ annotations 让类型注解延迟求值（字符串化），
# 从而可以在运行时不导入的情况下使用较新的联合类型写法（如 X | None）
from __future__ import annotations

# hashlib：对 user_id/server_name 做 SHA256 摘要，生成不泄露原文的 Redis key 片段
import hashlib
# json：将配额拒绝结果序列化为工具可读的 JSON 文本
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# run_blocking_io：把阻塞型 CPU/IO 调用（这里是 json.dumps）放到线程池，避免阻塞事件循环
from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
# get_redis_client：获取全局共享的异步 Redis 客户端，配额计数与原子扣减都依赖它
from src.infra.storage.redis import get_redis_client
from src.kernel.schemas.mcp import MCPRoleQuota

logger = get_logger(__name__)

# 哨兵值：limit 为 -1 表示“不限量”，供 Lua 脚本内以 >= 0 判断是否需要限流
_UNLIMITED = -1

# Redis 服务端 Lua 脚本：原子地完成“检查 + 扣减”日/周两个窗口的配额
# 之所以放在 Lua 里执行，是为了保证多副本并发下的读改写原子性（避免竞态导致超发）
# KEYS[1]=日窗口计数键 KEYS[2]=周窗口计数键
# ARGV[1..4]=日限额、周限额、日窗口TTL、周窗口TTL
# 返回：{是否允许(1/0), 触发的周期, 当前值, 限额, TTL, ...}
_CHECK_AND_CONSUME_SCRIPT = """
local daily_key = KEYS[1]
local weekly_key = KEYS[2]
local daily_limit = tonumber(ARGV[1])
local weekly_limit = tonumber(ARGV[2])
local daily_ttl = tonumber(ARGV[3])
local weekly_ttl = tonumber(ARGV[4])

local daily_current = tonumber(redis.call("get", daily_key) or "0")
local weekly_current = tonumber(redis.call("get", weekly_key) or "0")

if daily_limit >= 0 and daily_current >= daily_limit then
    return {0, "daily", daily_current, daily_limit, daily_ttl}
end

if weekly_limit >= 0 and weekly_current >= weekly_limit then
    return {0, "weekly", weekly_current, weekly_limit, weekly_ttl}
end

if daily_limit >= 0 then
    daily_current = redis.call("incr", daily_key)
    if daily_current == 1 then
        redis.call("expire", daily_key, daily_ttl)
    end
end

if weekly_limit >= 0 then
    weekly_current = redis.call("incr", weekly_key)
    if weekly_current == 1 then
        redis.call("expire", weekly_key, weekly_ttl)
    end
end

return {1, "", daily_current, daily_limit, daily_ttl, weekly_current, weekly_limit, weekly_ttl}
"""


@dataclass(frozen=True)
class MCPQuotaResult:
    """Result of a quota check."""

    # allowed：是否放行本次调用（True 表示未超限或无需限流）
    allowed: bool
    # period：被拒绝时命中的周期，取值 "daily" 或 "weekly"
    period: str = ""
    # limit：命中周期的上限值（None 表示该周期不限量）
    limit: int | None = None
    # current：命中周期当前已消耗的计数
    current: int = 0
    # reset_at：配额重置时间点（ISO 字符串），供前端提示用户何时恢复
    reset_at: str = ""


# 将“配额值”统一归一为 MCPRoleQuota 对象：
# 存储层可能返回已是模型的实例，也可能是原始 dict（如从 Mongo 读出）
def _quota_from_value(value: MCPRoleQuota | dict[str, Any]) -> MCPRoleQuota:
    if isinstance(value, MCPRoleQuota):
        return value
    return MCPRoleQuota.model_validate(value)


# 合并多个角色的同一维度限额，取“最宽松”策略：
# - 空列表 -> None（无限制）
# - 只要有任意一个为 None（不限量），合并结果即不限量
# - 否则取所有限额中的最大值（多角色叠加时给用户最高额度）
def _merge_limit(values: list[int | None]) -> int | None:
    if not values:
        return None
    if any(value is None for value in values):
        return None
    return max(value for value in values if value is not None)


def resolve_role_quota(
    role_quotas: Mapping[str, MCPRoleQuota | dict[str, Any]] | None,
    user_roles: list[str] | None,
) -> MCPRoleQuota | None:
    """Resolve the most permissive quota across the user's matching roles."""
    # 无角色配额表或用户无角色时，返回 None 表示“不限流”
    if not role_quotas or not user_roles:
        return None

    # 从配额表中挑出用户实际拥有的角色对应的配额
    matched = [
        _quota_from_value(role_quotas[role_name])
        for role_name in user_roles
        if role_name in role_quotas
    ]
    if not matched:
        return None

    # 跨角色分别合并日/周限额，得到该用户最终适用的配额
    return MCPRoleQuota(
        daily_limit=_merge_limit([quota.daily_limit for quota in matched]),
        weekly_limit=_merge_limit([quota.weekly_limit for quota in matched]),
    )


# 对敏感字符串（user_id、server_name）取 SHA256 前 24 位十六进制作为 Redis key 片段：
# 既避免原始 ID 直接暴露在 key 中，又能保证长度稳定、可复现
def _safe_key_part(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


# 计算当前时刻所处的日/周窗口标识及其剩余 TTL（秒）：
# 日窗口以 UTC 自然日为界，周窗口以 ISO 周（周一为起点）为界
# 返回：(日标识 YYYYMMDD, 日窗口剩余秒数, 周标识 YYYY-Www, 周窗口剩余秒数)
# TTL 用于给 Redis 计数键设置过期，使窗口自然滚动、无需额外清理任务
def _window_info(now: datetime | None = None) -> tuple[str, int, str, int]:
    current = now or datetime.now(UTC)
    # 统一转成 UTC，避免服务器本地时区影响窗口边界
    current = current.astimezone(UTC)

    # 计算“明天零点”作为当日窗口结束点，daily_ttl 即距结束的秒数（至少 1 秒）
    next_day = (current + timedelta(days=1)).date()
    day_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=UTC)
    daily_ttl = max(1, int((day_end - current).total_seconds()))

    # 使用 ISO 日历得到年/周号；以本周周一零点为起点推算周窗口结束点
    iso_year, iso_week, _ = current.isocalendar()
    start_of_week = current - timedelta(days=current.weekday())
    start_of_week = datetime(
        start_of_week.year,
        start_of_week.month,
        start_of_week.day,
        tzinfo=UTC,
    )
    week_end = start_of_week + timedelta(days=7)
    weekly_ttl = max(1, int((week_end - current).total_seconds()))

    return current.strftime("%Y%m%d"), daily_ttl, f"{iso_year}-W{iso_week:02d}", weekly_ttl


class MCPUsageLimiter:
    """Atomic Redis limiter for per-user MCP server calls."""

    # 允许注入外部 Redis 客户端（便于测试）；为 None 时延迟到首次使用再获取全局客户端
    def __init__(self, redis: Any | None = None) -> None:
        self._redis = redis

    # 懒加载 Redis 客户端：只有真正需要限流时才建立连接
    @property
    def redis(self) -> Any:
        if self._redis is None:
            self._redis = get_redis_client()
        return self._redis

    # 核心方法：检查并消费一次配额（原子操作）
    # 入参均为关键字参数，避免调用处参数错位
    # - user_id/server_name：定位配额归属；tool_name 可选，用于按工具粒度细分配额
    # - quota：已解析出的该用户适用的日/周限额
    # 返回 MCPQuotaResult，allowed=False 时携带命中周期与重置时间
    async def check_and_consume(
        self,
        *,
        user_id: str,
        server_name: str,
        quota: MCPRoleQuota,
        tool_name: str | None = None,
    ) -> MCPQuotaResult:
        # 日/周均无限制时直接放行，省去一次 Redis 往返
        if quota.daily_limit is None and quota.weekly_limit is None:
            return MCPQuotaResult(allowed=True)

        # 计算当前日/周窗口标识与 TTL
        day_id, daily_ttl, week_id, weekly_ttl = _window_info()
        # 对用户与作用域做摘要，拼装 Redis 计数键
        user_key = _safe_key_part(user_id)
        # 若指定了 tool_name，则配额作用域细化到“服务器:工具”，否则按整个服务器计
        quota_scope = f"{server_name}:{tool_name}" if tool_name else server_name
        server_key = _safe_key_part(quota_scope)
        daily_key = f"mcp:usage:{user_key}:{server_key}:daily:{day_id}"
        weekly_key = f"mcp:usage:{user_key}:{server_key}:weekly:{week_id}"

        # 调用 Lua 脚本原子完成检查+扣减；None 限额传入哨兵 -1 表示不限量
        raw = await self.redis.eval(
            _CHECK_AND_CONSUME_SCRIPT,
            2,
            daily_key,
            weekly_key,
            quota.daily_limit if quota.daily_limit is not None else _UNLIMITED,
            quota.weekly_limit if quota.weekly_limit is not None else _UNLIMITED,
            daily_ttl,
            weekly_ttl,
        )
        # 返回数组首位为 1 表示放行成功
        if int(raw[0]) == 1:
            return MCPQuotaResult(allowed=True)

        # 被拒绝：解析命中周期/当前值/限额/TTL，换算出重置时间点返回
        period, current, limit, ttl = raw[1], raw[2], raw[3], raw[4]
        reset_at = datetime.now(UTC) + timedelta(seconds=int(ttl))
        return MCPQuotaResult(
            allowed=False,
            period=str(period),
            current=int(current),
            limit=int(limit),
            reset_at=reset_at.isoformat(),
        )


# 面向“已知角色配额”的便捷入口：解析用户适用配额并消费一次
# is_admin=True、缺少 user_id/server_name 时一律放行（管理员不受限流约束）
async def check_and_consume_mcp_quota(
    *,
    user_id: str | None,
    server_name: str | None,
    tool_name: str | None = None,
    user_roles: list[str] | None,
    role_quotas: Mapping[str, MCPRoleQuota | dict[str, Any]] | None,
    is_admin: bool = False,
) -> MCPQuotaResult:
    """Resolve and consume quota for a known MCP server policy."""
    # 管理员或关键信息缺失时直接放行
    if is_admin or not user_id or not server_name:
        return MCPQuotaResult(allowed=True)

    # 解析该用户最终适用的配额；无匹配配额则不限流
    quota = resolve_role_quota(role_quotas, user_roles)
    if quota is None:
        return MCPQuotaResult(allowed=True)

    try:
        return await MCPUsageLimiter().check_and_consume(
            user_id=user_id,
            server_name=server_name,
            tool_name=tool_name,
            quota=quota,
        )
    except Exception as exc:
        # 容错原则：Redis 故障不应阻断正常业务调用，记录错误后放行（fail-open）
        logger.error("[MCP Quota] Redis quota check failed: %s", exc)
        return MCPQuotaResult(allowed=True)


# 解析用户的角色名列表以及是否拥有 MCP 管理员权限（mcp:admin）
# 返回 (角色名列表, 是否管理员)；出现异常时安全降级为 ([], False)
async def resolve_user_mcp_access(user_id: str) -> tuple[list[str], bool]:
    """Resolve user's role names and whether they have MCP admin permission."""
    try:
        # 局部导入，避免与 user/role 存储层产生模块级循环依赖
        from src.infra.role.storage import RoleStorage
        from src.infra.user.storage import UserStorage

        user = await UserStorage().get_by_id(user_id)
        if not user or not user.roles:
            return [], False

        # 逐个角色收集角色名与权限集合，权限可能是字符串或枚举，统一取字符串值
        role_storage = RoleStorage()
        roles = await role_storage.get_by_names(user.roles)
        resolved_roles: list[str] = []
        permissions: set[str] = set()
        for role in roles:
            resolved_roles.append(role.name)
            for permission in role.permissions:
                permissions.add(permission if isinstance(permission, str) else permission.value)
        return resolved_roles, "mcp:admin" in permissions
    except Exception as exc:
        # 解析失败时降级为无角色、非管理员，交由上层决定放行策略
        logger.warning("[MCP Quota] Failed to resolve user MCP access: %s", exc)
        return [], False


# 面向“已持久化的系统级 MCP 服务器”的入口：
# 先取出服务器上的 role_quotas 配置，再结合用户角色/管理员身份进行限流
async def check_and_consume_system_mcp_quota(
    *,
    user_id: str | None,
    server_name: str | None,
) -> MCPQuotaResult:
    """Resolve and consume quota for a persisted system MCP server."""
    if not user_id or not server_name:
        return MCPQuotaResult(allowed=True)

    try:
        # 局部导入避免循环依赖（storage 亦会引用本模块的配额能力）
        from src.infra.mcp.storage import MCPStorage

        # 服务器不存在（可能是用户级或临时服务器）则不限流
        server = await MCPStorage().get_system_server(server_name)
        if not server:
            return MCPQuotaResult(allowed=True)

        # 解析用户角色与管理员身份，交由通用入口完成检查+扣减
        user_roles, is_admin = await resolve_user_mcp_access(user_id)
        return await check_and_consume_mcp_quota(
            user_id=user_id,
            server_name=server_name,
            user_roles=user_roles,
            role_quotas=server.role_quotas,
            is_admin=is_admin,
        )
    except Exception as exc:
        # 同样遵循 fail-open：任何异常都放行，避免限流逻辑本身影响可用性
        logger.error("[MCP Quota] Failed to check system MCP quota: %s", exc)
        return MCPQuotaResult(allowed=True)


# 将配额拒绝结果序列化为工具可读的 JSON 文本（同步版本）
# ensure_ascii=False 以保留可读的非 ASCII 内容
def quota_error_json(server_name: str, result: MCPQuotaResult) -> str:
    """Serialize a quota denial in a tool-friendly shape."""
    return json.dumps(
        {
            "error": "MCP quota exceeded",
            "server": server_name,
            "period": result.period,
            "limit": result.limit,
            "current": result.current,
            "reset_at": result.reset_at,
        },
        ensure_ascii=False,
    )


# 异步版本：在 MCP 工具异步调用路径中使用，通过 run_blocking_io 将
# json.dumps 放入线程池，避免大 payload 序列化阻塞事件循环
async def quota_error_json_async(server_name: str, result: MCPQuotaResult) -> str:
    """Serialize a quota denial without blocking async MCP tool calls."""
    payload = {
        "error": "MCP quota exceeded",
        "server": server_name,
        "period": result.period,
        "limit": result.limit,
        "current": result.current,
        "reset_at": result.reset_at,
    }
    return await run_blocking_io(json.dumps, payload, ensure_ascii=False)

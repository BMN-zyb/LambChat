"""
MCP 服务器连接池

按服务器名称缓存 MCP 连接，多个用户共享相同的连接。
大幅减少重复连接的创建时间和资源消耗。
"""

import asyncio
import inspect
import time
from typing import Any, Optional, Set

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)

# 连接池：server_name -> PooledConnection
# 进程内全局字典，按服务器名缓存已建立的连接，供多用户复用
_connection_pool: dict[str, "PooledConnection"] = {}

# 连接池锁
# 保护 _connection_pool 的并发读写，避免多协程同时增删连接导致状态错乱
_pool_lock = asyncio.Lock()

# 后台任务追踪集合
# 持有异步关闭任务的强引用，防止其在完成前被 GC 回收（asyncio 的已知陷阱）
_background_tasks: Set[asyncio.Task] = set()

# 清理计数器
# 记录 get 调用次数，达到阈值时触发一次过期连接清理（惰性清理策略）
_cleanup_counter = 0

# 清理检查间隔
CLEANUP_CHECK_INTERVAL = 20

# 连接过期时间（秒），默认 15 分钟
CONNECTION_TTL = 900
# 记录默认值，供 _get_connection_ttl 判断"是否被测试等场景显式改写过模块常量"
_DEFAULT_CONNECTION_TTL = CONNECTION_TTL

# 最大连接数，防止大量动态 MCP server name 让进程内连接池无限增长
MAX_CONNECTIONS = 100
# 同上，保存默认值用于配置来源的优先级判断
_DEFAULT_MAX_CONNECTIONS = MAX_CONNECTIONS


def _track_background_task(task: asyncio.Task) -> None:
    """追踪后台任务，完成后自动从集合中移除"""
    # 加入集合保持强引用；完成回调负责把自己移除，避免集合无限膨胀
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _close_client(client: MultiServerMCPClient) -> None:
    # 尽力关闭底层 MCP 客户端：兼容同步/异步的 close() 或异步上下文的 __aexit__()
    try:
        if hasattr(client, "close"):
            result = client.close()
            # close 可能返回协程，需要 await
            if inspect.isawaitable(result):
                await result
        elif hasattr(client, "__aexit__"):
            result = client.__aexit__(None, None, None)  # type: ignore[func-returns-value]
            if inspect.isawaitable(result):
                await result
    except Exception as e:
        # 关闭失败不抛出，仅调试日志：清理阶段的异常不应影响主流程
        logger.debug(f"[MCP Pool] Error closing client: {e}")


def _get_connection_ttl() -> int:
    # 解析实际生效的连接 TTL：
    # 若模块常量 CONNECTION_TTL 被显式改写（如测试 monkeypatch）而 settings 仍是默认值，
    # 则优先使用模块常量；否则以 settings 配置为准，最后回退到常量。结果至少为 1 秒
    configured = getattr(settings, "MCP_POOL_TTL_SECONDS", None)
    if CONNECTION_TTL != _DEFAULT_CONNECTION_TTL and configured == _DEFAULT_CONNECTION_TTL:
        return max(int(CONNECTION_TTL), 1)
    return max(int(configured if configured is not None else CONNECTION_TTL), 1)


def _get_max_connections() -> int:
    # 解析实际生效的最大连接数，优先级判断逻辑同 _get_connection_ttl
    configured = getattr(settings, "MCP_POOL_MAX_CONNECTIONS", None)
    if MAX_CONNECTIONS != _DEFAULT_MAX_CONNECTIONS and configured == _DEFAULT_MAX_CONNECTIONS:
        return max(int(MAX_CONNECTIONS), 1)
    return max(int(configured if configured is not None else MAX_CONNECTIONS), 1)


class PooledConnection:
    """池化的 MCP 连接"""

    def __init__(
        self,
        server_name: str,
        server_config: dict[str, Any],
        config_hash: str,
        client: MultiServerMCPClient,
        tools: list[BaseTool],
    ):
        self.server_name = server_name
        self.server_config = server_config
        # config_hash：服务器配置的指纹，用于判断复用时配置是否发生变化
        self.config_hash = config_hash
        self.client = client
        self.tools = tools
        # created_at 用于 TTL 过期判断；last_access 用于超额时的 LRU 淘汰
        self.created_at = time.time()
        self.last_access = time.time()

    def is_expired(self, ttl: float | None = None) -> bool:
        """检查连接是否过期"""
        # 以创建时间为基准判断是否超过 TTL（注意：不是以最后访问时间为基准）
        if ttl is None:
            ttl = _get_connection_ttl()
        return time.time() - self.created_at > ttl

    def touch(self):
        """更新最后访问时间"""
        # 每次复用时刷新，供 LRU 淘汰排序使用
        self.last_access = time.time()


def _compute_server_hash(server_config: dict[str, Any]) -> str:
    """计算服务器配置的哈希值"""
    # 对配置做稳定序列化（键排序）后取 MD5，作为配置指纹；仅用于比较，非安全用途
    import hashlib
    import json

    config_str = json.dumps(server_config, sort_keys=True, default=str)
    return hashlib.md5(config_str.encode()).hexdigest()


async def get_pooled_connection(
    server_name: str,
    server_config: dict[str, Any],
) -> tuple[Optional[MultiServerMCPClient], list[BaseTool]]:
    """
    获取池化的 MCP 连接（如果可用）

    Args:
        server_name: 服务器名称
        server_config: 服务器配置

    Returns:
        tuple: (client, tools) - 客户端和工具列表
    """
    # 定期清理过期连接
    await _maybe_cleanup()
    # 在锁外计算哈希（可能较慢），减少持锁时间
    current_hash = await run_blocking_io(_compute_server_hash, server_config)

    async with _pool_lock:
        # 检查连接池
        if server_name in _connection_pool:
            pooled = _connection_pool[server_name]

            # 检查配置是否匹配
            # 仅当"未过期且配置指纹一致"时才复用，避免配置变更后仍用旧连接
            if not pooled.is_expired() and pooled.config_hash == current_hash:
                pooled.touch()
                logger.debug(
                    f"[MCP Pool] Reusing connection for server '{server_name}', "
                    f"{len(pooled.tools)} tools"
                )
                return pooled.client, pooled.tools

        # 没有可用连接
        return None, []


async def add_pooled_connection(
    server_name: str,
    server_config: dict[str, Any],
    client: MultiServerMCPClient,
    tools: list[BaseTool],
) -> None:
    """
    添加连接到连接池

    Args:
        server_name: 服务器名称
        server_config: 服务器配置
        client: MCP 客户端
        tools: 工具列表
    """
    # to_close 收集所有需要在释放锁后关闭的旧/多余客户端（关闭是 I/O，不宜持锁执行）
    to_close: list[MultiServerMCPClient] = []
    reuse_existing = False
    config_hash = await run_blocking_io(_compute_server_hash, server_config)
    async with _pool_lock:
        # 如果已存在且未过期，不覆盖
        if server_name in _connection_pool:
            pooled = _connection_pool[server_name]
            if not pooled.is_expired():
                # 已有可用连接：保留旧连接，若本次新建了不同的 client 则安排关闭它
                reuse_existing = True
                if pooled.client is not client:
                    to_close.append(client)
            else:
                # 已过期：安排关闭旧 client，随后用新连接替换
                to_close.append(pooled.client)

        if not reuse_existing:
            _connection_pool[server_name] = PooledConnection(
                server_name=server_name,
                server_config=server_config,
                config_hash=config_hash,
                client=client,
                tools=tools,
            )
            logger.info(
                f"[MCP Pool] Added connection for server '{server_name}', "
                f"{len(tools)} tools, pool size: {len(_connection_pool)}"
            )

            # 超过上限时按 LRU（last_access 最早）淘汰多余连接
            max_connections = _get_max_connections()
            if len(_connection_pool) > max_connections:
                sorted_entries = sorted(_connection_pool.items(), key=lambda x: x[1].last_access)
                for oldest_name, oldest in sorted_entries[
                    : len(_connection_pool) - max_connections
                ]:
                    # 保护刚加入的连接：避免把本次新增的条目又立刻淘汰掉
                    if oldest_name == server_name and len(_connection_pool) <= max_connections:
                        continue
                    removed = _connection_pool.pop(oldest_name, None)
                    if removed:
                        to_close.append(removed.client)

    # 释放锁后统一关闭，避免在持锁期间执行网络 I/O
    for stale_client in to_close:
        await _close_client(stale_client)


async def cleanup_expired_connections() -> int:
    """清理过期的连接，返回清理的数量"""
    async with _pool_lock:
        expired_servers = [name for name, conn in _connection_pool.items() if conn.is_expired()]

        for server_name in expired_servers:
            pooled = _connection_pool.pop(server_name, None)
            if pooled:
                # 过期连接的关闭放到后台任务异步执行，避免阻塞当前清理循环与持锁时间
                try:
                    task = asyncio.create_task(_close_client(pooled.client))
                    _track_background_task(task)
                except Exception as e:
                    logger.debug(f"[MCP Pool] Error cleaning up client for {server_name}: {e}")

        if expired_servers:
            logger.info(f"[MCP Pool] Cleaned up {len(expired_servers)} expired connections")

        return len(expired_servers)


async def _maybe_cleanup() -> None:
    """定期清理过期连接"""
    # 惰性清理：每 CLEANUP_CHECK_INTERVAL 次调用触发一次全量过期检查，
    # 用计数取代独立的定时器，简单且无需额外后台循环
    global _cleanup_counter
    _cleanup_counter += 1
    if _cleanup_counter >= CLEANUP_CHECK_INTERVAL:
        _cleanup_counter = 0
        await cleanup_expired_connections()


async def get_pool_stats() -> dict[str, Any]:
    """获取连接池统计信息"""
    # 返回连接池概览（总数 + 每个连接的存活时长/过期状态），用于监控与调试
    async with _pool_lock:
        servers_list: list[dict[str, Any]] = []

        for server_name, conn in _connection_pool.items():
            servers_list.append(
                {
                    "server_name": server_name,
                    "tools_count": len(conn.tools),
                    "age_seconds": int(time.time() - conn.created_at),
                    "is_expired": conn.is_expired(),
                }
            )

        stats: dict[str, Any] = {
            "total_connections": len(_connection_pool),
            "servers": servers_list,
        }

        return stats


async def close_all_connections() -> None:
    """Close every pooled MCP connection and clear process-local pool state."""
    # 进程关闭/重置时调用：先在锁内快照并清空连接池，再在锁外逐个关闭
    async with _pool_lock:
        pooled_connections = list(_connection_pool.values())
        _connection_pool.clear()

    for pooled in pooled_connections:
        await _close_client(pooled.client)

    # 等待所有在途的后台关闭任务结束，确保资源彻底释放
    if _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)

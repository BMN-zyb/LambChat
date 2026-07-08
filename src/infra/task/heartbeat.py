# src/infra/task/heartbeat.py
"""
Background Task Manager - Heartbeat Mechanism

Manages task heartbeat for detecting stale/failed tasks in distributed scenarios.
"""

import asyncio
from collections.abc import Awaitable, Callable

from src.infra.logging import get_logger
from src.infra.storage.redis import get_redis_client
from src.infra.utils.datetime import utc_now_iso

from .constants import HEARTBEAT_INTERVAL, HEARTBEAT_PREFIX, HEARTBEAT_TIMEOUT
from .startup_cleanup import _gather_limited

logger = get_logger(__name__)


class TaskHeartbeat:
    """
    任务心跳管理类

    负责启动和停止任务的心跳机制，用于检测任务是否存活。
    """

    # _heartbeat_tasks 维护本进程内每个 run_id 对应的心跳后台协程句柄，
    # 便于停止时精确取消对应的循环。
    def __init__(self) -> None:
        self._heartbeat_tasks: dict[str, asyncio.Task] = {}  # run_id -> heartbeat Task

    # 为某个 run 启动心跳循环。
    # 作用：周期性向 Redis 写入带 TTL 的心跳 key，作为「任务仍存活」的证据；
    # 同时刷新并发限流 Sorted Set 里该 run 的分数，防止被误判过期而清理。
    # 幂等：同一 run_id 已有心跳时直接返回，避免重复循环。
    async def start(self, run_id: str, user_id: str | None = None) -> None:
        """启动任务心跳"""
        if run_id in self._heartbeat_tasks:
            logger.warning(f"Heartbeat already exists for run_id={run_id}")
            return

        # 心跳循环本体：每 HEARTBEAT_INTERVAL 秒写一次。TTL 取超时阈值的 2 倍，
        # 这样即使偶尔漏写一拍也不会立刻过期；一旦进程崩溃、循环停摆，key 会在
        # 至多 2*TIMEOUT 后自动消失，其他实例据此识别出「僵尸任务」并接管恢复。
        async def heartbeat_loop():
            try:
                redis_client = get_redis_client()
                while True:
                    try:
                        # 设置心跳，带 TTL（超时时间的 2 倍）
                        await redis_client.set(
                            f"{HEARTBEAT_PREFIX}{run_id}",
                            utc_now_iso(),
                            ex=HEARTBEAT_TIMEOUT * 2,
                        )
                        # 刷新并发限制的 Sorted Set 分数（保持条目活跃）
                        if user_id:
                            try:
                                from src.infra.task.concurrency import get_concurrency_limiter

                                limiter = get_concurrency_limiter()
                                await limiter.refresh(user_id, run_id)
                            except Exception:
                                pass
                    except Exception as e:
                        logger.warning(f"Heartbeat write failed for run_id={run_id}: {e}")
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
            # 被 stop() 取消是正常收尾路径，静默吞掉
            except asyncio.CancelledError:
                pass
            # 无论何种方式退出，都从本地表移除自身，避免悬挂引用
            finally:
                self._heartbeat_tasks.pop(run_id, None)

        self._heartbeat_tasks[run_id] = asyncio.create_task(heartbeat_loop())

    # 停止某个 run 的心跳：取消本地循环协程，并立即删除 Redis 心跳 key，
    # 使其他实例尽快感知该 run 已结束（不必等 TTL 自然过期）。
    async def stop(self, run_id: str) -> None:
        """停止任务心跳"""
        # 取消心跳任务
        if run_id in self._heartbeat_tasks:
            task = self._heartbeat_tasks.pop(run_id)
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # 删除 Redis 中的心跳 key
        try:
            redis_client = get_redis_client()
            await redis_client.delete(f"{HEARTBEAT_PREFIX}{run_id}")
        except Exception as e:
            logger.warning(f"Failed to delete heartbeat for run_id={run_id}: {e}")

    # 停止本进程内所有心跳（服务关闭时调用）。用 _gather_limited 做并发但限流的
    # 批量停止，避免一次性对 Redis 发起过多删除请求。这里用默认参数把 run_id
    # 绑定进闭包，规避「循环变量延迟绑定」的经典坑。
    async def stop_all(self) -> None:
        """停止所有心跳任务"""
        run_ids = list(self._heartbeat_tasks.keys())
        stop_factories: list[Callable[[], Awaitable[None]]] = []
        for run_id in run_ids:

            async def _stop_current(run_id: str = run_id) -> None:
                await self.stop(run_id)

            stop_factories.append(_stop_current)

        await _gather_limited(stop_factories)

    # 检查 Redis 中是否存在某 run 的心跳 key。
    # 关键用途：判断任务是否正运行在「其他实例」上——本地没有该任务、但心跳
    # 仍在，说明别的进程还活着，不能贸然接管/恢复。
    async def check_exists(self, run_id: str) -> bool:
        """
        检查心跳是否存在

        用于判断任务是否在其他实例上运行。
        """
        try:
            redis_client = get_redis_client()
            heartbeat_key = f"{HEARTBEAT_PREFIX}{run_id}"
            heartbeat = await redis_client.get(heartbeat_key)
            return heartbeat is not None
        except Exception as e:
            logger.warning(f"Failed to check heartbeat for run_id={run_id}: {e}")
            return False

    # 仅检查「本进程」是否在跑该 run 的心跳（同步，无 IO），区别于 check_exists
    # 的跨实例 Redis 判断。
    def is_running(self, run_id: str) -> bool:
        """检查本地心跳任务是否在运行"""
        return run_id in self._heartbeat_tasks

# src/infra/task/cancellation.py
"""
Background Task Manager - Task Cancellation

Handles task interruption and cancellation logic including in-memory flags,
Redis-based distributed cancellation, and agent cleanup.
"""

import asyncio
import json
import time
from typing import Any, Dict, Optional

from src.infra.async_utils.blocking import run_blocking_io
from src.infra.logging import get_logger
from src.infra.session.storage import SessionStorage
from src.infra.session.trace_storage import get_trace_storage
from src.infra.storage.redis import get_redis_client
from src.infra.utils.datetime import utc_now_iso
from src.kernel.schemas.session import SessionUpdate

from .constants import CANCEL_CHANNEL, INTERRUPT_PREFIX
from .exceptions import TaskInterruptedError

logger = get_logger(__name__)

# 内存中的中断标志集合（用于快速检查）
# run_id -> 加入时间戳，支持定期清理过期条目
# 进程内的「中断标志」表：run_id -> 打标时间戳。用于极低成本地快速判断某个
# run 是否已被要求中断（check_interrupt_fast 只查这里，不做任何 IO）。分布式
# 场景下，其他实例通过 Redis pub/sub 把取消信号同步过来后也会写入此表。
_interrupted_runs: Dict[str, float] = {}

# 清理参数
_INTERRUPT_MAX_AGE = 600  # 10 分钟
_INTERRUPT_CLEANUP_INTERVAL = 1000  # 每 1000 次检查触发一次清理
# 优雅取消的等待窗口：取消后先给任务这么久自行收尾，超时才强制 cancel()。
_GRACEFUL_CANCEL_TIMEOUT = 2.0
# 调用 agent.close() 的超时，避免个别 agent 卡死拖垮取消流程。
_AGENT_CLOSE_CANCEL_TIMEOUT = 2.0
_interrupt_check_counter = 0


# JSON 序列化放到线程池执行，避免阻塞事件循环。
async def _cancel_payload_json_dumps(payload: dict[str, Any]) -> str:
    return await run_blocking_io(json.dumps, payload)


# 带超时地调用 agent.close(run_id) 以中止底层 graph 执行；超时返回 False 而非
# 抛错，保证取消主流程能继续走下去。
async def _close_agent_safely(agent: Any, run_id: str) -> bool:
    try:
        await asyncio.wait_for(agent.close(run_id), timeout=_AGENT_CLOSE_CANCEL_TIMEOUT)
        return True
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out waiting for agent.close during cancel: run_id=%s, timeout=%.1fs",
            run_id,
            _AGENT_CLOSE_CANCEL_TIMEOUT,
        )
        return False


class TaskCancellation:
    """
    任务取消和中断管理类

    处理任务的取消、中断信号管理和清理工作。
    """

    def __init__(self, lock: asyncio.Lock, tasks: Dict[str, asyncio.Task]):
        """
        初始化任务取消管理器

        Args:
            lock: 异步锁，用于保护共享状态
            tasks: 任务字典，run_id -> asyncio.Task
        """
        self._lock = lock
        self._tasks = tasks

    # 分布式取消的核心入口，采用「多级、尽力而为」的策略，逐层收紧：
    #   1) 立刻写内存中断标志（最快，本进程内高频检查点即可感知）；
    #   2) 写 Redis 中断信号（供任务真正运行的那个实例的检查点感知）；
    #   3) 若任务恰在本进程，走优雅取消（先等一会，超时再强制 cancel）；
    #   4) 调用 agent.close() 中止底层 graph；
    #   5) 通过 pub/sub 广播，让运行该任务的其他实例就地取消；
    #   6) 释放 Redis 并发槽位。
    # 只要中断信号成功设置就算 success，即使任务实际在别的实例上运行。
    async def cancel_run(
        self,
        run_id: str,
        publish: bool = True,
        user_id: Optional[str] = None,
        run_info: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        取消特定 run 的任务（支持分布式）

        Args:
            run_id: 运行 ID
            publish: 是否通过 Redis pub/sub 广播取消信号（用于分布式场景）
            user_id: 取消任务的用户 ID
            run_info: 运行信息字典 {session_id, trace_id, agent_id}

        Returns:
            {
                "success": bool,  # 中断信号是否成功设置
                "cancelled_locally": bool,  # 是否在本地实例取消
                "run_id": str,  # 被取消的 run_id
                "message": str  # 状态信息
            }
        """
        cancelled_locally = False
        interrupt_signal_set = False

        # 1. 立即设置内存中的中断标志（最快）
        _interrupted_runs[run_id] = time.time()
        logger.info(f"Memory interrupt flag set for run_id={run_id}")

        # 2. 设置 Redis 中断信号（用于分布式场景）
        try:
            redis_client = get_redis_client()
            await redis_client.set(
                f"{INTERRUPT_PREFIX}{run_id}",
                utc_now_iso(),
                ex=300,  # 5 分钟过期
            )
            interrupt_signal_set = True
            logger.info(f"Redis interrupt signal set for run_id={run_id}")
        except Exception as e:
            logger.warning(f"Failed to set interrupt signal: {e}")

        task_to_cancel: asyncio.Task | None = None
        async with self._lock:
            if run_id in self._tasks:
                task = self._tasks[run_id]
                if not task.done():
                    task_to_cancel = task

        if user_id and run_info:
            session_id = run_info.get("session_id")
            if session_id:
                try:
                    await SessionStorage().update(
                        session_id,
                        SessionUpdate(
                            metadata={
                                "task_recoverable": False,
                                "task_error_code": "cancelled",
                            }
                        ),
                    )
                except Exception as e:
                    logger.warning(f"Failed to persist cancel recovery metadata: {e}")

        # If this process owns the task, run_task will persist terminal events
        # in the right order. Only fall back here when no local task can do it.
        if run_info and task_to_cancel is None:
            trace_id = run_info.get("trace_id")
            if trace_id:
                try:
                    if run_info.get("session_id"):
                        try:
                            from src.infra.session.dual_writer import get_dual_writer

                            await get_dual_writer().flush_mongo_buffer()
                        except Exception as flush_error:
                            logger.warning(
                                f"Failed to flush events before trace completion: {flush_error}"
                            )
                    trace_storage = get_trace_storage()
                    success = await trace_storage.complete_trace(
                        trace_id,
                        status="error",
                        metadata={"cancel_reason": "Task cancelled by user"},
                        ensure_token_usage=False,
                    )
                    logger.info(
                        f"MongoDB trace status updated: trace_id={trace_id}, success={success}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to update trace status: {e}")

        # 4. 调用 agent.close(run_id) 取消 graph 执行
        if run_info:
            agent_id = run_info.get("agent_id")
            if agent_id:
                try:
                    from src.agents.core.base import AgentFactory

                    agent = await AgentFactory.get(agent_id)
                    await _close_agent_safely(agent, run_id)
                    logger.info(f"Agent.close({run_id}) called for agent={agent_id}")
                except Exception as e:
                    logger.warning(f"Failed to call agent.close: {e}")

        # 本进程持有该任务时的优雅取消：用 shield 包住，先给它
        # _GRACEFUL_CANCEL_TIMEOUT 秒自行收尾（此时 run_task 会按正确顺序落
        # 终态事件）；超时仍未结束才真正 cancel() 并等待其退出。
        if task_to_cancel is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task_to_cancel),
                    timeout=_GRACEFUL_CANCEL_TIMEOUT,
                )
                logger.info(f"Task completed during graceful cancel: run_id={run_id}")
            except asyncio.TimeoutError:
                task = task_to_cancel
                if not task.done():
                    task.cancel()
                    cancelled_locally = True
                    logger.info(f"Task cancelled locally: run_id={run_id}")
                    await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The task may finish with TaskInterruptedError or another terminal error
                # during the graceful window; run_task handles persistence and status.
                logger.info(f"Task finished during graceful cancel: run_id={run_id}")

        # 如果本地没有这个任务，或者需要广播给其他实例
        if publish:
            try:
                redis_client = get_redis_client()
                agent_id = run_info.get("agent_id") if run_info else None
                session_id = run_info.get("session_id") if run_info else None
                trace_id = run_info.get("trace_id") if run_info else None
                payload = await _cancel_payload_json_dumps(
                    {
                        "run_id": run_id,
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "trace_id": trace_id,
                        "timestamp": utc_now_iso(),
                    }
                )

                await redis_client.publish(
                    CANCEL_CHANNEL,
                    payload,
                )
                logger.info(
                    f"Published cancel signal for run_id={run_id}, agent_id={agent_id}, session_id={session_id}"
                )
            except Exception as e:
                logger.warning(f"Failed to publish cancel signal: {e}")

        # 释放 Redis 并发槽位（无论本地还是远程取消都需要）
        user_id = run_info.get("user_id") if run_info else None
        interrupt_success = interrupt_signal_set or run_id in _interrupted_runs
        if user_id and (cancelled_locally or interrupt_success):
            try:
                from src.infra.task.concurrency import get_concurrency_limiter

                limiter = get_concurrency_limiter()
                await limiter.release(user_id, run_id)
                logger.info(f"Concurrency slot released for run_id={run_id}")
            except Exception as e:
                logger.warning(f"Failed to release concurrency slot on cancel: {e}")

        # 构建返回结果
        # success: 中断信号成功设置即认为成功（即使任务在其他实例运行）
        success = interrupt_signal_set or run_id in _interrupted_runs

        if cancelled_locally:
            message = "任务已取消"
        elif success:
            message = "取消信号已发送，任务将在下次检查点中断"
        else:
            message = "取消信号设置失败"

        return {
            "success": success,
            "cancelled_locally": cancelled_locally,
            "run_id": run_id,
            "message": message,
        }

    # 极低成本的中断检查：只查内存标志，不做任何 IO，适合在主循环里高频调用。
    # 每调用 _INTERRUPT_CLEANUP_INTERVAL 次顺带触发一次过期条目清理，摊薄成本。
    @staticmethod
    def check_interrupt_fast(run_id: str) -> bool:
        """
        快速检查中断信号（仅内存，无 IO）

        用于高频调用的场景（如主循环），避免 Redis IO 开销。
        对于分布式场景，依赖 Redis pub/sub 将信号同步到本地内存。

        Args:
            run_id: 运行 ID

        Returns:
            True 如果任务被中断
        """
        global _interrupted_runs, _interrupt_check_counter

        # 定期清理过期条目
        _interrupt_check_counter += 1
        if _interrupt_check_counter >= _INTERRUPT_CLEANUP_INTERVAL:
            _interrupt_check_counter = 0
            _cleanup_stale_interrupts()

        return run_id in _interrupted_runs

    # 供 agent 在执行过程中调用的中断检查点：命中则抛 TaskInterruptedError。
    # 先查内存标志（带 _INTERRUPT_MAX_AGE 时效，防止老标志误伤新任务），
    # 再查 Redis（覆盖「取消发生在别的实例」的分布式场景）。
    @staticmethod
    async def check_interrupt(run_id: str) -> None:
        """
        检查是否有中断信号，如果有则抛出 TaskInterruptedError

        供 agent 在执行过程中调用，实现优雅中断。
        优先检查内存标志（最快），其次检查 Redis（分布式场景）。

        Args:
            run_id: 运行 ID

        Raises:
            TaskInterruptedError: 如果任务被中断
        """
        global _interrupted_runs

        # 1. 首先检查内存标志（最快，无 IO）
        if (
            run_id in _interrupted_runs
            and time.time() - _interrupted_runs[run_id] < _INTERRUPT_MAX_AGE
        ):
            logger.info(f"Memory interrupt detected for run_id={run_id}")
            raise TaskInterruptedError(f"Task interrupted: run_id={run_id}")

        # 2. 检查 Redis（分布式场景）
        try:
            redis_client = get_redis_client()
            interrupted = await redis_client.get(f"{INTERRUPT_PREFIX}{run_id}")
            if interrupted:
                logger.info(f"Redis interrupt detected for run_id={run_id}")
                raise TaskInterruptedError(f"Task interrupted: run_id={run_id}")
        except TaskInterruptedError:
            raise
        except Exception as e:
            logger.warning(f"Failed to check Redis interrupt signal: {e}")

    # 清除中断信号（内存标志 + Redis key）。任务终态收尾时调用，避免遗留标志
    # 影响后续复用同一 run_id 的场景。
    @staticmethod
    async def clear_interrupt(run_id: str) -> None:
        """
        清除中断信号

        Args:
            run_id: 运行 ID
        """
        global _interrupted_runs

        # 1. 清除内存标志
        _interrupted_runs.pop(run_id, None)

        # 2. 清除 Redis 标志
        try:
            redis_client = get_redis_client()
            await redis_client.delete(f"{INTERRUPT_PREFIX}{run_id}")
        except Exception as e:
            logger.warning(f"Failed to clear interrupt signal: {e}")


# 清理内存中断标志表里超过 _INTERRUPT_MAX_AGE 的过期条目，防止无限增长导致
# 内存泄漏。由 check_interrupt_fast 按调用次数周期性触发。
def _cleanup_stale_interrupts() -> None:
    """清理超过 _INTERRUPT_MAX_AGE 的过期中断条目"""
    global _interrupted_runs
    now = time.time()
    expired = [rid for rid, t in _interrupted_runs.items() if now - t > _INTERRUPT_MAX_AGE]
    for rid in expired:
        _interrupted_runs.pop(rid, None)
    if expired:
        logger.info(f"Cleaned up {len(expired)} stale interrupt entries")

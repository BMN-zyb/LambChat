# src/infra/task/pubsub.py
"""
Background Task Manager - Redis Pub/Sub

Handles Redis pub/sub for distributed task cancellation signals.
"""

import asyncio
import json
from typing import Any, Callable, Dict, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.pubsub_hub import get_pubsub_hub

from .constants import CANCEL_CHANNEL

logger = get_logger(__name__)


# JSON 反序列化放到线程池执行，避免阻塞事件循环。
async def _cancel_message_json_loads(raw_value: Any) -> Any:
    return await run_blocking_io(json.loads, raw_value)


_AGENT_CLOSE_CANCEL_TIMEOUT = 2.0


# 带超时地调用 agent.close(run_id)：收到取消广播后用于中止底层 graph 执行，
# 超时返回 False 而不抛错，避免个别 agent 卡死阻塞消息处理。
async def _close_agent_safely(agent: Any, run_id: str) -> bool:
    try:
        await asyncio.wait_for(agent.close(run_id), timeout=_AGENT_CLOSE_CANCEL_TIMEOUT)
        return True
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out waiting for agent.close via pub/sub: run_id=%s, timeout=%.1fs",
            run_id,
            _AGENT_CLOSE_CANCEL_TIMEOUT,
        )
        return False


class TaskPubSub:
    """
    Redis Pub/Sub 管理类

    处理任务取消信号的发布和订阅。
    """

    # lock/tasks 由 manager 传入并与之共享：收到取消广播后要就地 cancel 本进程
    # 里对应的 asyncio 任务，因此需要访问同一份任务表和保护它的锁。
    def __init__(self, lock: asyncio.Lock, tasks: Dict[str, asyncio.Task]):
        """
        初始化 Pub/Sub 管理器

        Args:
            lock: 异步锁，用于保护共享状态
            tasks: 任务字典，run_id -> asyncio.Task
        """
        self._lock = lock
        self._tasks = tasks
        self._subscription_token: Optional[str] = None
        self._on_message: Optional[Callable[[Dict[str, Any]], None]] = None
        self._running = False

    # 启动取消信号监听：向共享的 pubsub hub 订阅 CANCEL_CHANNEL 频道。应在应用
    # 启动时调用一次；幂等（已在运行则直接返回）。
    async def start_listener(
        self,
        on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """
        启动 Redis pub/sub 监听器，用于接收分布式取消信号

        应在应用启动时调用

        Args:
            on_message: 消息回调函数，接收解析后的消息字典
        """
        if self._running:
            return

        self._on_message = on_message
        hub = get_pubsub_hub()
        self._subscription_token = hub.subscribe(
            CANCEL_CHANNEL,
            self._handle_hub_message,
        )
        await hub.start()
        self._running = True
        logger.info(f"Started listening on Redis channel: {CANCEL_CHANNEL}")

    # hub 消息回调的薄封装：转交给真正的处理逻辑。
    async def _handle_hub_message(self, message: Dict[str, Any]) -> None:
        await self._handle_cancel_message(message, self._on_message)

    # 处理一条取消广播消息（取消信号的「接收侧」核心）。按以下顺序尽力取消：
    #   1) 执行可选的自定义回调 on_message；
    #   2) 调用 agent.close(run_id) 中止底层 graph；
    #   3) 若该任务恰在本进程，就地 cancel 对应 asyncio 任务；
    #   4) 否则若带 trace_id，则直接把 trace 落为 error 终态（因为本进程管不到
    #      那个任务，只能保证持久化状态一致）。
    async def _handle_cancel_message(
        self,
        message: Dict[str, Any],
        on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        """处理取消消息"""
        try:
            data = await _cancel_message_json_loads(message["data"])
            run_id = data.get("run_id")
            agent_id = data.get("agent_id")
            session_id = data.get("session_id")
            trace_id = data.get("trace_id")
            if run_id:
                logger.info(
                    f"Received cancel signal for run_id={run_id}, agent_id={agent_id}, session_id={session_id}"
                )

                # 调用自定义回调
                if on_message:
                    try:
                        await on_message(data)  # type: ignore[misc]
                    except Exception as e:
                        logger.warning(f"Error in on_message callback: {e}")

                # 调用 agent.close(run_id) 取消 graph
                if agent_id:
                    try:
                        from src.agents.core.base import AgentFactory

                        agent = await AgentFactory.get(agent_id)
                        await _close_agent_safely(agent, run_id)
                        logger.info(
                            f"Agent.close({run_id}) called via pub/sub for agent={agent_id}"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to call agent.close via pub/sub: {e}")

                # 尝试本地取消 asyncio Task
                task_to_cancel = None
                async with self._lock:
                    if run_id in self._tasks:
                        task = self._tasks[run_id]
                        if not task.done():
                            task_to_cancel = task
                if task_to_cancel is not None:
                    task_to_cancel.cancel()
                    logger.info(f"Task cancelled via pub/sub: run_id={run_id}")
                elif trace_id:
                    try:
                        from src.infra.session.dual_writer import get_dual_writer
                        from src.infra.session.trace_storage import get_trace_storage

                        try:
                            await get_dual_writer().flush_mongo_buffer()
                        except Exception as flush_error:
                            logger.warning(
                                f"Failed to flush events before pub/sub trace completion: {flush_error}"
                            )
                        trace_storage = get_trace_storage()
                        success = await trace_storage.complete_trace(
                            trace_id,
                            status="error",
                            metadata={"cancel_reason": "Task cancelled via pub/sub"},
                            ensure_token_usage=False,
                        )
                        logger.info(
                            f"MongoDB trace status updated via pub/sub: trace_id={trace_id}, success={success}"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to update trace status via pub/sub: {e}")
        except json.JSONDecodeError:
            logger.warning(f"Invalid cancel message format: {message['data']}")
        except Exception as e:
            logger.error(f"Error processing cancel message: {e}")

    # 停止监听并退订频道；若 hub 已无其他订阅则顺带停掉。应在应用关闭时调用。
    async def stop_listener(self) -> None:
        """
        停止 Redis pub/sub 监听器

        应在应用关闭时调用
        """
        self._running = False
        self._on_message = None

        if self._subscription_token:
            hub = get_pubsub_hub()
            hub.unsubscribe(self._subscription_token)
            self._subscription_token = None
            await hub.stop_if_idle()

        logger.info("Pub/sub listener stopped")

    @property
    def is_running(self) -> bool:
        """检查监听器是否正在运行"""
        return self._running

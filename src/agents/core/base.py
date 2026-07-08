"""
Graph Agent 基类

每个 Agent 就是一个 CompiledGraph，流式请求接入 graph，
节点通过 config 获取 Presenter 并输出 SSE 事件。
"""

# ============================================================================
# 模块背景：Agent 运行时的"双层 graph 嵌套"架构
# ----------------------------------------------------------------------------
# 本文件是所有 Agent 的共享基础设施，核心设计是"双层 graph 嵌套"：
#   - 外层 graph（就在本文件的 GraphBuilder / BaseGraphAgent 里组装）：
#     结构极简，只有 START -> agent_node -> END，是一层"薄壳"。
#   - 内层 graph（在各 agent 的 nodes.py 里，由 deepagents 的
#     create_deep_agent(...) 构建）：才是真正的 ReAct 循环
#     （推理 -> 调用工具 -> 观察 -> 再推理）。
# 外层的 agent_node 只是把请求"委托"给内层 deep agent，并把事件透传出来。
# 之所以保留这层薄壳，是为了给所有 agent 统一挂上 checkpointer、
# recursion_limit、Presenter 注入、SSE 事件流、中断检查等运行时能力，
# 而把"怎么思考、怎么用工具"这件事完全交给 deepagents 的内层图。
#
# 真正注册的 agent 有 3 个：search / fast / team，
# 它们通过 @register_agent("id") 装饰器注册进下方的 _AGENT_REGISTRY。
# ============================================================================
import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Type

from langchain_core.runnables import RunnableConfig
# LangGraph 图原语：START/END 是特殊的入口/出口节点，StateGraph 用于声明式地组装图
from langgraph.graph import END, START, StateGraph

# AgentEventProcessor：把内层 graph 的原始事件（astream_events 产出）转换成 SSE 事件
from src.infra.agent import AgentEventProcessor
from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.utils.datetime import utc_now
# Presenter：事件输出中枢，节点通过它调用 present_* 产出前端可见的 SSE 事件
from src.infra.writer.present import Presenter, PresenterConfig
from src.kernel.config import settings

logger = get_logger(__name__)

# ============================================================================
# Agent 注册表
# ============================================================================

# 全局注册表：agent id（如 "search"/"fast"/"team"）-> Agent 实现类。
# register_agent 装饰器在各 agent 模块被 import 时把映射写入这里；
# 之后 AgentFactory / get_agent_class 再按 id 取出并实例化。是进程内共享的单一映射。
_AGENT_REGISTRY: Dict[str, Type[Any]] = {}


def _coerce_checkpoint_time(ts_raw: Any, cutoff_time: datetime) -> datetime | None:
    """将 checkpoint 的原始时间戳规整为可比较的 datetime（失败返回 None）。

    为什么需要：不同 checkpointer 存的 ts 类型不一致（可能是 ISO 字符串，也可能是
    epoch 秒），而 TTL 清理逻辑要拿它和 cutoff_time 做大小比较，必须先统一成 datetime。
    解析失败一律返回 None，让调用方安全跳过这条脏数据；另外若解析出的时间是 naive
    （无时区）而 cutoff 带时区，则补上 UTC，避免 naive 与 aware 相减/比较时抛 TypeError。
    """
    checkpoint_time = None
    # 情况一：ISO 8601 字符串，用 fromisoformat 解析
    if isinstance(ts_raw, str):
        try:
            checkpoint_time = datetime.fromisoformat(ts_raw)
        except (ValueError, TypeError):
            return None
    # 情况二：数值型 epoch 时间戳，用 fromtimestamp 解析
    else:
        try:
            checkpoint_time = datetime.fromtimestamp(float(ts_raw))
        except (TypeError, ValueError):
            return None

    # 统一时区：naive 时间补上 UTC，保证后续能与带时区的 cutoff 正确比较
    if checkpoint_time.tzinfo is None and cutoff_time.tzinfo is not None:
        checkpoint_time = checkpoint_time.replace(tzinfo=timezone.utc)
    return checkpoint_time


def _prune_memory_saver_storage(storage: Any, cutoff_time: datetime) -> int:
    """遍历 MemorySaver 的内存存储，删除早于 cutoff_time 的过期线程，返回删除数量。

    仅在没有 MongoDB checkpointer、退化用 MemorySaver 时才需要：MemorySaver 把所有
    checkpoint 常驻进程内存，不主动清理会造成内存无限增长（内存泄漏）。这里以"每个线程
    最新一条 checkpoint 的时间"作为该线程的活跃时间，若它都早于 cutoff 则整条删除；
    空线程也一并删除。所有异常都被吞掉，保证后台清理任务不会因单条脏数据而崩溃退出。
    """
    to_delete = []

    for thread_id in list(storage.keys()):
        try:
            checkpoints = storage.get(thread_id, {})
            # 该线程没有任何 checkpoint，视为无用数据直接标记删除
            if not checkpoints:
                to_delete.append(thread_id)
                continue

            # 取该线程里时间戳最新的一条 checkpoint，作为线程的"最近活跃时间"
            latest_checkpoint = max(checkpoints.values(), key=lambda x: getattr(x, "ts", 0))
            checkpoint_time = _coerce_checkpoint_time(
                getattr(latest_checkpoint, "ts", "0"),
                cutoff_time,
            )

            # 连最新的 checkpoint 都过期了，说明整个线程都可以清理
            if checkpoint_time is not None and checkpoint_time < cutoff_time:
                to_delete.append(thread_id)
        except Exception:
            pass

    # 先收集待删列表、再统一删除，避免在遍历 storage 的同时修改它
    deleted_count = 0
    for thread_id in to_delete:
        try:
            del storage[thread_id]
            deleted_count += 1
        except Exception:
            pass
    return deleted_count


def register_agent(agent_id: str):
    """
    Agent 注册装饰器

    用法:
        @register_agent("search")
        class SearchAgent:
            ...
    """

    # 真正的装饰器：接收被装饰的 Agent 类，登记到注册表后原样返回
    def decorator(cls: Type[Any]) -> Type[Any]:
        # 建立 id -> 类 的映射，供工厂按 id 查找实现类
        _AGENT_REGISTRY[agent_id] = cls
        # 反向在类上记下自己的 id，供实例通过 agent_id 属性读取
        cls._agent_id = agent_id
        return cls

    return decorator


# ============================================================================
# BaseGraphAgent - Graph Agent 基类
# ============================================================================


class BaseGraphAgent(ABC):
    """
    Graph Agent 基类

    参考 LangGraph 设计，每个 Agent 就是一个 CompiledGraph。

    流程:
    1. 流式请求进入 -> 创建 Presenter
    2. Presenter 注入到 config.configurable["presenter"]
    3. 节点从 config 获取 presenter，调用 present_* 方法
    4. astream_events 捕获 LLM/Tool 事件
    5. 所有事件转换为 SSE 格式 yield 给前端

    子类实现:
        - build_graph(builder): 构建 graph 结构
        - state_class: 状态类 (可选)

    示例节点:
        def my_node(state: dict, config: RunnableConfig) -> dict:
            presenter = config["configurable"]["presenter"]
            presenter.present_text("Hello")
            return {"output": "done"}
    """

    # 以下为类级默认元数据，子类可覆盖；_agent_id 还会被 register_agent 装饰器回填。
    # 这些字段主要供前端展示（名称/描述/排序/选项）与工厂 list_agents 时读取。
    _agent_id: str = "base"
    _agent_name: str = "Base Agent"
    _description: str = ""
    _version: str = "0.1.0"
    # 排序权重（数值越小越靠前）
    _sort_order: int = 100
    # Agent 选项配置（供前端渲染）
    # 格式: {"option_name": {"type": "boolean", "default": False, "label": "...", "description": "..."}}
    _options: Dict[str, Dict[str, Any]] = {}

    def __init__(self, recursion_limit: int | None = None, enable_checkpointer: bool = True):
        """初始化 Agent 运行时状态（此时并不构建 graph，真正构建推迟到 initialize()）。

        Args:
            recursion_limit: 外层+内层 graph 的递归步数上限，默认取全局配置
                SESSION_MAX_RUNS_PER_SESSION，用于防止 ReAct 循环无限执行。
            enable_checkpointer: 是否启用 checkpointer（持久化会话状态，支持多轮上下文）。
        """
        self.recursion_limit = recursion_limit or settings.SESSION_MAX_RUNS_PER_SESSION
        self.enable_checkpointer = enable_checkpointer
        # graph 与 checkpointer 延迟到 initialize() 才真正创建（懒加载，避免构造即建图）
        self._graph: Any = None
        self._checkpointer: Any = None
        self._initialized = False
        # 记录每个 run_id 对应的流任务，close() 时可据此精确取消某一次运行
        self._stream_tasks: Dict[str, asyncio.Task] = {}  # run_id -> Task

    # 对外暴露 agent 的唯一 id（由 register_agent 回填，或用类默认值 "base"）
    @property
    def agent_id(self) -> str:
        return self._agent_id

    # 对外暴露 agent 的展示名（供前端与 trace/presenter 元数据使用）
    @property
    def name(self) -> str:
        return self._agent_name

    @property
    def options(self) -> Dict[str, Dict[str, Any]]:
        """获取 Agent 支持的选项配置"""
        return self._options

    # 图状态的类型：内层 deep agent 一般用 dict 即可；子类可返回自定义 TypedDict 状态类
    @property
    def state_class(self) -> type:
        """状态类，子类可覆盖"""
        return dict

    # 抽象方法：子类必须实现，用 builder 声明外层 graph 的节点与边。
    # 对 search/fast/team 而言，这里通常只加一个 agent_node，连成 START -> agent_node -> END，
    # 而 agent_node 内部再委托给 deepagents 的内层 ReAct 图。
    @abstractmethod
    def build_graph(self, builder: "GraphBuilder") -> None:
        """
        构建 Graph

        子类实现此方法，使用 builder 添加节点和边。

        示例:
            def build_graph(self, builder):
                builder.add_node("agent", self.agent_node)
                builder.set_entry_point("agent")
                builder.add_edge("agent", END)
        """
        pass

    async def initialize(self) -> None:
        """初始化 Agent"""
        # 幂等保护：已初始化则直接返回，避免重复建图/重复起清理任务
        if self._initialized:
            return

        # 创建 checkpointer（优先 MongoDB，fallback 到 MemorySaver）
        if self.enable_checkpointer:
            from src.infra.storage.checkpoint import get_mongo_checkpointer

            self._checkpointer = get_mongo_checkpointer()
            # MongoDB 不可用时退化到进程内 MemorySaver（仅单进程有效、重启即丢，故需 TTL 清理）
            if self._checkpointer is None:
                from langgraph.checkpoint.memory import MemorySaver

                self._checkpointer = MemorySaver()

                # 启动后台清理任务，防止内存泄漏
                self._cleanup_task = asyncio.create_task(self._cleanup_memory_saver())
                self._cleanup_task.add_done_callback(lambda t: None)  # prevent GC

                logger.warning(
                    f"[Agent {self._agent_id}] Using MemorySaver with TTL cleanup (1 hour)"
                )
            else:
                logger.info(f"[Agent {self._agent_id}] Using MongoDB checkpointer")

        # 构建 graph
        builder = GraphBuilder(self.state_class)
        # 委托子类把节点/边填进 builder（构造外层薄壳的结构）
        self.build_graph(builder)
        # 编译外层 graph 成 CompiledGraph：挂上 checkpointer 做状态持久化，
        # 并设定递归步数上限（recursion_limit）防止 ReAct 循环死循环
        self._graph = builder.compile(
            checkpointer=self._checkpointer,
            recursion_limit=self.recursion_limit,
        )

        # 标记初始化完成，后续 stream()/invoke() 不再重复构建
        self._initialized = True

    async def _cleanup_memory_saver(self) -> None:
        """定期清理 MemorySaver 中的旧数据，防止内存泄漏"""
        from langgraph.checkpoint.memory import MemorySaver

        # 后台守护循环：只在退化用 MemorySaver 时运行，周期性回收过期 checkpoint
        while True:
            try:
                await asyncio.sleep(3600)  # 每小时清理一次

                # checkpointer 已被换成别的类型（或已清空），无需再清理，退出循环
                if not isinstance(self._checkpointer, MemorySaver):
                    break

                storage = self._checkpointer.storage
                if not storage:
                    continue

                # 清理 1 小时前的 checkpoint
                cutoff_time = utc_now() - timedelta(hours=1)
                # 遍历+删除是同步阻塞操作，丢到线程池执行，避免阻塞事件循环
                deleted_count = await run_blocking_io(
                    _prune_memory_saver_storage,
                    storage,
                    cutoff_time,
                )

                if deleted_count:
                    logger.info(
                        f"[Agent {self._agent_id}] Cleaned {deleted_count} old checkpoints "
                        f"(total remaining: {len(storage)})"
                    )

            # 任务被取消（一般发生在 close() 时）：正常退出循环，不当作错误
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Agent {self._agent_id}] Failed to cleanup MemorySaver: {e}")

    async def close(self, run_id: Optional[str] = None) -> None:
        """
        清理资源

        Args:
            run_id: 可选，指定要取消的运行 ID。如果不指定，则清理所有资源。
        """
        # 两种模式：给定 run_id 只取消这一次运行（不影响其它会话）；
        # 不给 run_id 则整体关闭该 agent（取消所有运行、停清理任务、释放 graph）
        if run_id is not None:
            # 取消特定的 stream_task
            task = self._stream_tasks.pop(run_id, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info(f"[Agent {self.agent_id}] Cancelled stream task: run_id={run_id}")
        else:
            # 取消所有正在运行的 stream_task
            for _, task in list(self._stream_tasks.items()):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            self._stream_tasks.clear()

            # 取消后台清理任务（MemorySaver fallback 时会创建）
            cleanup_task = getattr(self, "_cleanup_task", None)
            if cleanup_task is not None and not cleanup_task.done():
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if hasattr(self, "_cleanup_task"):
                self._cleanup_task = None  # type: ignore[assignment]

            # 清理 graph 和 checkpointer
            self._graph = None
            self._checkpointer = None
            self._initialized = False
            logger.info(f"[Agent {self.agent_id}] Closed and cleaned up all resources")

    # ==================== 流式执行 ====================

    def stream(
        self,
        message: str,
        session_id: str | None = None,
        user_id: Optional[str] = None,
        **kwargs,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        流式执行 Agent，yield SSE 事件字典

        这是主要的对外接口，返回格式:
            {"event": "message:chunk", "data": {"content": "..."}}
        """
        # 未显式传 session_id 就生成一个新的；它同时用作 checkpointer 的 thread_id
        if session_id is None:
            session_id = str(uuid.uuid4())
        return self._stream(message, session_id, user_id=user_id, **kwargs)

    async def _stream(
        self,
        message: str,
        session_id: str,
        user_id: Optional[str] = None,
        **kwargs,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """内部流式执行"""
        # 提取 goal 相关参数，用于在 done 之前正确发出 goal:end 事件
        active_goal = kwargs.get("active_goal")
        goal_started_at = kwargs.get("goal_started_at")

        # 首次调用时惰性初始化：构建并编译 graph、准备好 checkpointer
        if not self._initialized:
            await self.initialize()

        # 优先使用传入的 presenter（来自 TaskManager，带有正确的 run_id）
        # 如果没有传入，则创建新的 Presenter
        presenter = kwargs.get("presenter")
        if presenter is None:
            presenter = Presenter(
                PresenterConfig(
                    session_id=session_id,
                    agent_id=self.agent_id,
                    agent_name=self.name,
                    user_id=user_id,
                    enable_storage=kwargs.get("enable_storage", True),
                )
            )
            logger.info(f"[Agent] Created new presenter: run_id={presenter.run_id}")
        else:
            logger.info(f"[Agent] Using passed presenter: run_id={presenter.run_id}")

        # 设置请求上下文（供工具使用）
        from src.infra.logging.context import TraceContext

        logger.info(
            f"[Agent] Setting TraceContext: session_id={session_id}, run_id={presenter.run_id}"
        )
        try:
            # 保留当前 span 链，只把 trace_id 换成本次 presenter 的，串起分布式追踪
            current_trace = TraceContext.get()
            TraceContext.set(
                trace_id=presenter.trace_id,
                span_id=current_trace.span_id,
                parent_span_id=current_trace.parent_span_id,
            )
            TraceContext.set_request_context(
                session_id=session_id,
                run_id=presenter.run_id,
                user_id=user_id,
                trace_id=presenter.trace_id,
            )

            # 确保 trace 在数据库中创建（绑定 user_id）
            await presenter._ensure_trace()

            # 发送元数据（由 manager.py 保存）
            meta_evt = presenter.metadata()
            yield meta_evt

            # 构建 config，注入 presenter
            agent_options = kwargs.get("agent_options", {}) or {}
            langsmith_context = {
                "agent_options": agent_options,
                "disabled_tools": kwargs.get("disabled_tools"),
                "disabled_skills": kwargs.get("disabled_skills"),
                "enabled_skills": kwargs.get("enabled_skills"),
                "persona_system_prompt": kwargs.get("persona_system_prompt"),
                "disabled_mcp_tools": kwargs.get("disabled_mcp_tools"),
                "base_url": kwargs.get("base_url", ""),
                "team_id": kwargs.get("team_id"),
                "active_goal": kwargs.get("active_goal"),
                "recommendation_input": kwargs.get("recommendation_input"),
                "attachments": kwargs.get("attachments", []),
            }
            langsmith_metadata = await build_presenter_langsmith_metadata(
                presenter,
                langsmith_context,
            )
            # 组装 RunnableConfig 传给内层 graph：
            # - thread_id 是 checkpointer 存取会话状态的键（多轮上下文靠它续接）
            # - presenter 注入 configurable，内层节点用 get_presenter(config) 取出并产出事件
            # - **kwargs 把上层透传的所有参数一并带下去，供节点/工具读取
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": session_id,
                    "session_id": session_id,
                    "presenter": presenter,
                    "trace_id": presenter.trace_id,
                    **kwargs,
                },
                "metadata": langsmith_metadata,
                "recursion_limit": self.recursion_limit,
            }

            # 初始状态
            initial_state = {
                "input": message,
                "session_id": session_id,
                "messages": [],
                "context": kwargs,
                "attachments": kwargs.get("attachments", []),
            }

            # 导入中断检查函数
            from src.infra.task.manager import (
                BackgroundTaskManager,
                TaskInterruptedError,
            )

            # 使用队列来传递事件（限制大小防止消费者慢时内存无限增长）
            event_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
            stream_error = None
            stream_done = False

            # 放入"终止信号"（done/error）时绝不能因队列满而阻塞：
            # 队列满就丢弃一个最旧的事件腾出位置，确保唤醒主循环的信号一定进得去
            def put_terminal_queue_item(item: tuple[str, Any]) -> None:
                """Put terminal wake-up items without blocking on a full event queue."""
                while True:
                    try:
                        event_queue.put_nowait(item)
                        return
                    except asyncio.QueueFull:
                        try:
                            event_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            continue

            async def run_stream():
                """运行 graph 流并将事件放入队列"""
                nonlocal stream_error, stream_done
                try:
                    # 真正驱动外层 graph 执行；由于 agent_node 内部委托给了内层 deep agent，
                    # 这里会连带把内层 ReAct 循环的 LLM/工具事件一并以流的形式吐出来
                    # 使用 astream_events API
                    async for event in self._graph.astream_events(
                        initial_state,
                        config,
                        version="v2",
                    ):
                        # 在生产事件时检查中断
                        await BackgroundTaskManager.check_interrupt(presenter.run_id)
                        await event_queue.put(("event", event))
                except TaskInterruptedError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    stream_error = e
                    put_terminal_queue_item(("error", e))
                finally:
                    stream_done = True
                    # 放入终止信号，唤醒主循环（避免 await get() 永久阻塞）
                    put_terminal_queue_item(("done", None))

            # 启动流任务
            stream_task = asyncio.create_task(run_stream())
            # 注册任务以便 close 可以取消
            self._stream_tasks[presenter.run_id] = stream_task

            # 中断检查间隔（秒）- 1 秒是性能和响应速度的平衡点
            interrupt_check_interval = 1.0

            # 创建事件处理器
            event_processor = AgentEventProcessor(presenter, base_url=kwargs.get("base_url", ""))
            agent_options = kwargs.get("agent_options") or {}

            # 收尾时补发一次 token 用量事件（token:usage）；失败只告警，不影响主流程
            async def emit_usage_once() -> None:
                try:
                    await event_processor.emit_token_usage(
                        model_id=agent_options.get("model_id"),
                        model=agent_options.get("model"),
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit token:usage event during cleanup: {e}")

            # 异常/取消时尽力把队列里已产出的事件"排干"处理掉（带条数上限与单事件超时，
            # 避免收尾阶段被卡死），尽量不丢失已经生成的内容
            async def drain_event_queue(
                *,
                max_events: int = 100,
                per_event_timeout: float = 0.05,
            ) -> None:
                drained = 0
                while not event_queue.empty() and drained < max_events:
                    try:
                        item_type, item_data = event_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item_type == "event" and item_data:
                        try:
                            await asyncio.wait_for(
                                event_processor.process_event(item_data),
                                timeout=per_event_timeout,
                            )
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            break
                        except Exception:
                            pass
                    drained += 1

            try:
                terminal_error: BaseException | None = None
                while True:
                    # 使用 wait_for 定期检查中断信号
                    # 即使 LLM 请求阻塞，也能响应取消
                    try:
                        item = await asyncio.wait_for(
                            event_queue.get(), timeout=interrupt_check_interval
                        )
                    except asyncio.TimeoutError:
                        # 超时时使用快速内存检查（无 IO 开销）
                        if BackgroundTaskManager.check_interrupt_fast(presenter.run_id):
                            raise TaskInterruptedError(
                                f"Task interrupted: run_id={presenter.run_id}"
                            )
                        continue

                    item_type, item_data = item

                    # 收到终止信号：正常结束前再快速检查一次是否被中断
                    if item_type == "done":
                        if BackgroundTaskManager.check_interrupt_fast(presenter.run_id):
                            raise TaskInterruptedError(
                                f"Task interrupted: run_id={presenter.run_id}"
                            )
                        break

                    # 流任务内部出错：把异常重新抛到主循环，交给下面的 except 统一处理
                    if item_type == "error":
                        raise item_data

                    # 使用 AgentEventProcessor 处理事件
                    await event_processor.process_event(item_data)

            # 被取消/中断，或运行出错：先记下异常、尽量排干队列，再把异常原样抛出
            except (asyncio.CancelledError, TaskInterruptedError) as exc:
                terminal_error = exc
                await drain_event_queue()
                raise
            except Exception as exc:
                terminal_error = exc
                await drain_event_queue()
                raise
            finally:
                # 注销并取消流任务
                self._stream_tasks.pop(presenter.run_id, None)
                if not stream_task.done():
                    stream_task.cancel()
                    try:
                        await stream_task
                    except (asyncio.CancelledError, TaskInterruptedError):
                        pass
                # Flush pending chunks and clear event_processor memory
                await event_processor.finalize()
                await emit_usage_once()
                # goal:end 必须在 done 之前发出，保证事件顺序正确
                if active_goal is not None:
                    goal_end_data = {
                        "goal": active_goal,
                        "started_at": goal_started_at,
                        "ended_at": datetime.now(timezone.utc).isoformat(),
                    }
                    goal_end_evt = {
                        "event": "goal:end",
                        "data": goal_end_data,
                    }
                    await presenter.emit(goal_end_evt)
                # 只有正常结束（无异常）才在这里发 done；异常路径交给上层 manager.py 收尾
                if terminal_error is None:
                    await presenter.emit(presenter.done())

            # 先 yield goal:end，再 yield done
            if active_goal is not None:
                goal_end_data = {
                    "goal": active_goal,
                    "started_at": goal_started_at,
                    "ended_at": datetime.now(timezone.utc).isoformat(),
                }
                yield {"event": "goal:end", "data": goal_end_data}
            # 发送完成
            yield presenter.done()

        except asyncio.CancelledError:
            # 任务被取消，yield 队列中剩余的事件（由 manager.py 保存）
            raise

        # 其他异常（TaskInterruptedError, Exception）直接抛给 manager.py 处理
        finally:
            # 无论成功或失败都要清理请求上下文，避免 contextvar 泄漏污染后续请求
            TraceContext.clear_request_context()
            TraceContext.clear()

    async def invoke(self, message: str, session_id: str | None = None, **kwargs) -> str:
        """非流式执行，返回最终结果"""
        # 同 stream()：缺 session_id 就临时生成一个作为 thread_id
        if session_id is None:
            session_id = str(uuid.uuid4())
        # 同样惰性初始化，保证 graph 已构建
        if not self._initialized:
            await self.initialize()

        config: RunnableConfig = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": self.recursion_limit,
        }

        # 直接 ainvoke 跑完整个 graph，取最终状态里的 output（不产出 SSE 事件流）
        result = await self._graph.ainvoke(
            {"input": message, "session_id": session_id, "messages": []},
            config,
        )
        return result.get("output", "")


# ============================================================================
# GraphBuilder - 增强的 Graph 构建器
# ============================================================================


class GraphBuilder:
    """
    Graph 构建器

    封装 LangGraph StateGraph，提供流畅的 API。

    用法:
        builder = GraphBuilder(MyState)
        builder.add_node("agent", agent_node)
        builder.set_entry_point("agent")
        builder.add_edge("agent", END)
        graph = builder.compile()
    """

    def __init__(self, state_class: type = dict):
        """先把节点/边/入口/条件边的声明收集起来，等 compile() 时一次性组装成 StateGraph。"""
        self._state_class = state_class
        # 节点名 -> 处理函数
        self._nodes: Dict[str, Callable] = {}
        # 普通边列表：元素为 (from_node, to_node)
        self._edges: List[tuple] = []
        # 入口节点名（compile 时连一条 START -> entry_point）
        self._entry_point: Optional[str] = None
        # 条件边列表：元素为 (from_node, 判定函数, 分支->目标节点的映射)
        self._conditional_edges: List[tuple] = []

    def add_node(self, name: str, func: Callable, description: str = "") -> "GraphBuilder":
        """添加节点"""
        self._nodes[name] = func
        return self

    def add_edge(self, from_node: str, to_node: str) -> "GraphBuilder":
        """添加边"""
        self._edges.append((from_node, to_node))
        return self

    def set_entry_point(self, node_name: str) -> "GraphBuilder":
        """设置入口点"""
        self._entry_point = node_name
        return self

    def add_conditional_edges(
        self,
        from_node: str,
        condition: Callable,
        path_map: Dict[str, str],
    ) -> "GraphBuilder":
        """添加条件边"""
        self._conditional_edges.append((from_node, condition, path_map))
        return self

    def compile(self, checkpointer=None, recursion_limit: int | None = None) -> Any:
        """编译 graph"""
        # 组装外层"薄壳"graph：结构固定为 START -> (子类加的少量节点) -> END。
        # 真正的 ReAct 循环在节点内部委托给 deepagents 的内层 graph，这层不掺和推理逻辑。
        graph: StateGraph = StateGraph(self._state_class)

        # 添加节点
        for name, func in self._nodes.items():
            graph.add_node(name, func)

        # 设置入口点
        if self._entry_point:
            # 把 START 连到入口节点，形成 START -> agent_node
            graph.add_edge(START, self._entry_point)

        # 添加边
        for from_node, to_node in self._edges:
            # 允许用字符串 "END" 表示结束，这里归一化成 LangGraph 的 END 哨兵常量
            target = END if to_node == "END" else to_node
            graph.add_edge(from_node, target)

        # 添加条件边
        for from_node, condition, path_map in self._conditional_edges:
            # 同理，把条件分支目标里的 "END" 也归一化成 END 常量
            normalized = {k: END if v == "END" else v for k, v in path_map.items()}
            graph.add_conditional_edges(from_node, condition, normalized)

        # 编译成可执行的 CompiledGraph：传入 checkpointer 即获得状态持久化能力。
        # 注意 recursion_limit 不在编译期设置，而是运行时通过 config 传入（见 initialize/_stream）。
        return graph.compile(checkpointer=checkpointer)


# ============================================================================
# 辅助函数 - 节点内获取 Presenter
# ============================================================================


def get_presenter(config: RunnableConfig) -> Presenter:
    """从 config 中获取 Presenter"""
    presenter = config.get("configurable", {}).get("presenter")
    # 取不到说明没走 stream() 的注入流程，直接报错避免节点后续静默失败
    if presenter is None:
        raise RuntimeError(
            "Presenter not found in config. Make sure to use BaseGraphAgent.stream()"
        )
    return presenter


# ============================================================================
# Agent 工厂
# ============================================================================


class AgentFactory:
    """Agent 工厂，管理实例创建和缓存"""

    # id -> 已初始化的单例实例；同一个 agent 在整个进程内复用同一份，避免重复建图
    _instances: Dict[str, BaseGraphAgent] = {}
    # 保护并发初始化：多个请求同时首次访问同一 agent 时，只允许一个真正去构建
    _lock = asyncio.Lock()

    @classmethod
    async def get(cls, agent_id: str) -> BaseGraphAgent:
        """获取 Agent 实例（单例）"""
        # 快路径：已缓存则直接返回，不必加锁
        if agent_id in cls._instances:
            return cls._instances[agent_id]

        async with cls._lock:
            # 双重检查：拿到锁后再确认一次，避免等锁期间已被别的协程创建好
            if agent_id in cls._instances:
                return cls._instances[agent_id]

            # 注册表里没有 → 触发一次 agent 发现（import 各 agent 模块以执行 @register_agent）
            if agent_id not in _AGENT_REGISTRY:
                from src.agents import discover_agents

                discover_agents()

            # 发现之后仍然没有 → 确实未注册，报错并列出当前可用的 id
            if agent_id not in _AGENT_REGISTRY:
                raise ValueError(f"Agent '{agent_id}' 未注册。可用: {list(_AGENT_REGISTRY.keys())}")

            # 从注册表取实现类，实例化并 initialize（构建/编译 graph），再存入缓存
            agent_cls = _AGENT_REGISTRY[agent_id]
            agent = agent_cls()
            await agent.initialize()
            cls._instances[agent_id] = agent
            return agent

    @classmethod
    def list_agents(cls, default_agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出所有可用 Agent（包含选项配置），按 sort_order 和名称排序，默认 agent 排在最前面"""
        # 如果注册表为空，尝试发现 agents
        if not _AGENT_REGISTRY:
            from src.agents import discover_agents

            discover_agents()

        # 把注册表里每个类的元数据摊平成前端需要的字典（用 getattr 提供兜底默认值，
        # 优先取 *_key 形式的 i18n 键，没有再退回普通字段）
        agents = [
            {
                "id": aid,
                "name": getattr(agent_cls, "_name_key", None)
                or getattr(agent_cls, "_agent_name", aid.title()),
                "description": getattr(agent_cls, "_description_key", None)
                or getattr(agent_cls, "_description", ""),
                "version": getattr(agent_cls, "_version", "0.1.0"),
                "sort_order": getattr(agent_cls, "_sort_order", 100),
                "icon": getattr(agent_cls, "_icon", "Bot"),
                "labels": {},
                "supports_sandbox": getattr(agent_cls, "_supports_sandbox", False),
                "options": getattr(agent_cls, "_options", {}),
            }
            for aid, agent_cls in _AGENT_REGISTRY.items()
        ]

        # 排序：默认 agent 放最前面，其余按 sort_order 和名称排序
        def sort_key(agent):
            is_default = agent["id"] == default_agent_id
            return (0 if is_default else 1, agent["sort_order"], agent["name"])

        agents.sort(key=sort_key)
        return agents

    @classmethod
    async def get_filtered_agents(
        cls,
        user_roles: List[str],
        role_agent_map: dict,
        default_agent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取用户可用的 Agents（根据全局配置和角色配置过滤）

        过滤规则:
        1. 全局配置存在 → 以全局启用列表为基准
        2. 全局配置不存在 → 默认所有 agent 启用
        3. 角色配置存在（含空列表） → 取角色允许与全局启用的交集
        4. 角色配置不存在 → 使用全局配置
        """
        from src.infra.agent.config_storage import get_agent_config_storage

        logger.info(
            f"[get_filtered_agents] user_roles={user_roles}, role_agent_map={role_agent_map}"
        )

        # 获取所有注册 agents
        all_agents = cls.list_agents(default_agent_id)
        all_agent_ids = {a["id"] for a in all_agents}

        # 获取全局配置
        storage = get_agent_config_storage()
        catalog_configs = (
            await storage.get_catalog_config() if hasattr(storage, "get_catalog_config") else []
        )
        catalog_map = {config.id: config for config in catalog_configs}
        global_configs = [] if catalog_configs else await storage.get_global_config()

        if catalog_configs:
            enabled_agent_ids = {a.id for a in catalog_configs if a.enabled}
            logger.info(f"[get_filtered_agents] catalog config exists, enabled={enabled_agent_ids}")
        elif global_configs:
            # 全局配置已保存过 → 以它为准（即使全部禁用也尊重）
            enabled_agent_ids = {a.id for a in global_configs if a.enabled}
            logger.info(f"[get_filtered_agents] global config exists, enabled={enabled_agent_ids}")
        else:
            # 从未配置过全局设置 → 默认全部启用
            enabled_agent_ids = all_agent_ids
            logger.info("[get_filtered_agents] no global config yet, using all agents")

        # 收集角色允许的 agents
        role_allowed: Optional[set] = None
        for role_id in user_roles:
            role_config = role_agent_map.get(role_id)
            if role_config is not None:
                # 角色有配置（包括空列表）
                if role_allowed is None:
                    role_allowed = set()
                role_allowed.update(role_config)

        if role_allowed is None:
            # 所有角色都未配置 → 使用全局配置
            final_ids = enabled_agent_ids
            logger.info("[get_filtered_agents] no role config, using global config")
        else:
            # 至少一个角色有配置 → 取交集
            final_ids = role_allowed & enabled_agent_ids
            logger.info(
                f"[get_filtered_agents] role_config intersect global: {role_allowed} & {enabled_agent_ids} = {final_ids}"
            )

        # 按最终允许集合过滤，并用 catalog 配置覆盖展示字段（名称/描述/图标/排序/多语言标签）
        filtered = []
        for agent in all_agents:
            if agent["id"] not in final_ids:
                continue
            catalog = catalog_map.get(agent["id"])
            if catalog:
                agent = {
                    **agent,
                    "name": catalog.name,
                    "description": catalog.description,
                    "icon": catalog.icon,
                    "sort_order": catalog.sort_order,
                    "labels": {
                        locale: label.model_dump() for locale, label in catalog.labels.items()
                    },
                }
            filtered.append(agent)
        filtered.sort(
            key=lambda agent: (
                0 if agent["id"] == default_agent_id else 1,
                agent.get("sort_order", 100),
                agent.get("name", ""),
            )
        )
        logger.info(f"[get_filtered_agents] filtered count={len(filtered)}")
        return filtered

    @classmethod
    async def close_all(cls) -> None:
        """关闭所有 Agent 实例"""
        for agent_id, agent in cls._instances.items():
            try:
                await agent.close()
            except Exception as e:
                logger.warning(f"Error closing Agent '{agent_id}': {e}")
        cls._instances.clear()


# ============================================================================
# 便捷函数
# ============================================================================


def get_agent_class(agent_id: str) -> Type[BaseGraphAgent]:
    """获取已注册的 Agent 类"""
    if agent_id not in _AGENT_REGISTRY:
        raise ValueError(f"Agent '{agent_id}' 未注册")
    return _AGENT_REGISTRY[agent_id]


def resolve_agent_name(agent_id: str) -> str:
    """Resolve a stable display name for trace and presenter metadata."""
    # 注册表里没有就尝试发现一次；发现本身若抛异常，退回用 id 的 Title 形式兜底
    if agent_id not in _AGENT_REGISTRY:
        try:
            from src.agents import discover_agents

            discover_agents()
        except Exception:
            return agent_id.title()

    agent_cls = _AGENT_REGISTRY.get(agent_id)
    if agent_cls is None:
        return agent_id.title()
    return getattr(agent_cls, "_agent_name", agent_id.title())


async def build_presenter_langsmith_metadata(
    presenter: Any,
    context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build LangSmith metadata while remaining compatible with older presenter fakes."""
    try:
        return await presenter.build_langsmith_metadata(context or {})
    # 兼容旧的 presenter 假实现：其 build_langsmith_metadata 不接受 context 参数
    except TypeError:
        return await presenter.build_langsmith_metadata()


def list_registered_agents() -> List[str]:
    """列出所有已注册的 Agent ID"""
    return list(_AGENT_REGISTRY.keys())

"""
Fast Agent - 基于 LangGraph 的快速 Agent

特点：
- 无沙箱（使用内存 backend）
- 支持 Skills
- 快速响应

架构:
    START -> fast_agent_node -> END
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict

from langchain_core.runnables import RunnableConfig

from src.agents.core.base import (
    BaseGraphAgent,
    GraphBuilder,
    build_presenter_langsmith_metadata,
    register_agent,
)
from src.agents.fast_agent.context import FastAgentContext
from src.agents.fast_agent.nodes import fast_agent_node
from src.agents.fast_agent.state import FastAgentState
from src.infra.backend.context import set_user_context
from src.infra.logging import get_logger
from src.infra.task.exceptions import TaskInterruptedError
from src.infra.writer.present import Presenter, PresenterConfig
from src.kernel.config import settings

logger = get_logger(__name__)


# ============================================================================
# FastAgent 类
# ============================================================================


@register_agent("fast")
class FastAgent(BaseGraphAgent):
    """
    Fast Agent - 快速响应，无沙箱

    适用于：
    - 快速对话
    - 无需文件系统操作的场景
    - 低延迟要求的场景
    """

    # 以下为类级元数据，供 @register_agent 注册与前端展示读取：
    # _agent_id 是注册表主键（须与装饰器 "fast" 一致）；_agent_name/_description 为默认展示名与描述；
    # _name_key/_description_key 为 i18n 文案键；_version 版本号；_sort_order 列表排序权重（越小越靠前）；
    # _supports_sandbox 标明是否提供沙箱能力（Fast Agent 明确不提供）。
    _agent_id = "fast"
    _agent_name = "Fast Agent"
    _name_key = "agents.fast.name"
    _description = "快速响应的 AI 助手，无沙箱，支持 Skills"
    _description_key = "agents.fast.description"
    _version = "1.0.0"
    _sort_order = 2  # 排序权重，数值越小越靠前
    _supports_sandbox = False  # 不支持沙箱环境

    # _options 声明该 agent 暴露给前端的可调选项（随 agent 元数据下发给 UI 渲染）：
    # enable_thinking 控制思考强度（off/low/medium/high/max，仅支持思考的模型生效）；
    # enable_code_interpreter 开关轻量 JS/TS 代码解释器。
    # 用户所选值经 agent_options 一路透传到 nodes.py，用于构建 thinking 配置与相应中间件。
    _options = {
        "enable_thinking": {
            "type": "string",
            "default": "off",
            "label": "Thinking",
            "label_key": "agentOptions.enableThinking.label",
            "description": "Control thinking intensity (supported models only)",
            "description_key": "agentOptions.enableThinking.description",
            "icon": "Brain",
            "options": [
                {"value": "off", "label_key": "agentOptions.enableThinking.options.off"},
                {"value": "low", "label_key": "agentOptions.enableThinking.options.low"},
                {"value": "medium", "label_key": "agentOptions.enableThinking.options.medium"},
                {"value": "high", "label_key": "agentOptions.enableThinking.options.high"},
                {"value": "max", "label_key": "agentOptions.enableThinking.options.max"},
            ],
        },
        "enable_code_interpreter": {
            "type": "boolean",
            "default": False,
            "label": "Code Interpreter",
            "label_key": "agentOptions.enableCodeInterpreter.label",
            "description": "Run lightweight JavaScript/TypeScript in an isolated interpreter",
            "description_key": "agentOptions.enableCodeInterpreter.description",
            "icon": "Settings",
        },
    }

    # 向 BaseGraphAgent 暴露外层 graph 的状态 schema 类型，用于编译 graph。
    # 单行函数体，按约定用上方注释而非 docstring。
    @property
    def state_class(self) -> type:
        return FastAgentState

    def build_graph(self, builder: GraphBuilder) -> None:
        """
        构建 Graph

        当前结构: START -> fast_agent_node -> END
        """
        # 外层 graph 只是薄壳：唯一业务节点是 fast_agent_node，真正的 ReAct 循环在其内部由
        # deepagents.create_deep_agent 装配。这里只把结构连成 START -> agent -> END。
        builder.add_node("agent", fast_agent_node)
        # 设为入口节点（等价于从 START 连一条边到 "agent"）。
        builder.set_entry_point("agent")
        # 唯一出边指向 END：节点执行完即结束整个外层 graph。
        builder.add_edge("agent", "END")

    async def initialize(self) -> None:
        """初始化 Agent"""
        if self._initialized:
            return

        # Keep the outer graph stateless for now: it only wraps one agent node, while
        # conversation history is persisted by the inner deep agent checkpointer.
        # If this outer graph grows into a multi-node workflow that needs resume or
        # per-node recovery, add an outer checkpointer with an isolated namespace or
        # thread id so it cannot collide with the inner graph's message state.
        builder = GraphBuilder(self.state_class)
        self.build_graph(builder)
        # 编译外层 graph：checkpointer=None —— 外层刻意保持无状态（对话历史由内层 deep agent 的
        # checkpointer 持久化，避免两层 message state 互相覆盖）；recursion_limit 以会话级步数上限兜底。
        self._graph = builder.compile(
            checkpointer=None,
            recursion_limit=settings.SESSION_MAX_RUNS_PER_SESSION,
        )

        self._initialized = True
        logger.info(f"{self.name} initialized (no sandbox, no checkpointer)")

    async def _stream(
        self,
        message: str,
        session_id: str,
        user_id: str | None = None,
        presenter=None,
        **kwargs,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        执行 graph
        """
        # 懒初始化：首次流式调用时才编译外层 graph。
        if not self._initialized:
            await self.initialize()

        # 把当前 user_id/session_id 绑定到上下文变量，供下游 backend、日志、工具调用读取。
        set_user_context(user_id or "default", session_id)

        # Presenter 负责把内部事件转换为前端 SSE 事件流，并按需落库；未传入时按会话新建一个。
        if presenter is None:
            presenter = Presenter(
                PresenterConfig(
                    session_id=session_id,
                    agent_id=self.agent_id,
                    agent_name=self.name,
                    user_id=user_id,
                    enable_storage=True,
                )
            )

        # 创建并初始化 FastAgentContext
        disabled_tools = kwargs.get("disabled_tools")
        disabled_skills = kwargs.get("disabled_skills")
        enabled_skills = kwargs.get("enabled_skills")
        disabled_mcp_tools = kwargs.get("disabled_mcp_tools")
        context = FastAgentContext(
            session_id=session_id,
            agent_id=self.agent_id,
            user_id=user_id,
            disabled_tools=disabled_tools,
            disabled_skills=disabled_skills,
            enabled_skills=enabled_skills,
            disabled_mcp_tools=disabled_mcp_tools,
        )
        # setup 装配内置工具与技能；MCP 工具此时不加载，留到节点内首次用到时再懒加载。
        await context.setup()

        # 发送 metadata
        yield presenter.metadata()

        # 构建 config
        agent_options = kwargs.get("agent_options", {})
        logger.info(f"[FastAgent] agent_options: {agent_options}")

        # 汇总用于 LangSmith 追踪的上下文（选项、过滤名单、persona、附件等），便于回溯每轮请求。
        langsmith_context = {
            "agent_options": agent_options,
            "disabled_tools": disabled_tools,
            "disabled_skills": disabled_skills,
            "enabled_skills": enabled_skills,
            "persona_system_prompt": kwargs.get("persona_system_prompt"),
            "disabled_mcp_tools": disabled_mcp_tools,
            "base_url": kwargs.get("base_url", ""),
            "active_goal": kwargs.get("active_goal"),
            "recommendation_input": kwargs.get("recommendation_input"),
            "attachments": kwargs.get("attachments", []),
        }
        langsmith_metadata = await build_presenter_langsmith_metadata(
            presenter,
            langsmith_context,
        )

        # 组装传给 graph 的 RunnableConfig：configurable 里塞入 presenter、context 与各类选项，
        # 节点 fast_agent_node 通过 config.get("configurable") 读取它们；thread_id 用 session_id 串联会话。
        config: RunnableConfig = {
            "configurable": {
                "thread_id": session_id,
                "presenter": presenter,
                "context": context,
                "agent_options": agent_options,
                "disabled_skills": disabled_skills,
                "enabled_skills": enabled_skills,
                "persona_system_prompt": kwargs.get("persona_system_prompt"),
                "disabled_mcp_tools": disabled_mcp_tools,
                "base_url": kwargs.get("base_url", ""),
                "active_goal": kwargs.get("active_goal"),
                "recommendation_input": kwargs.get("recommendation_input"),
            },
            "metadata": langsmith_metadata,
            "recursion_limit": settings.SESSION_MAX_RUNS_PER_SESSION,
        }

        # 初始状态
        attachments = kwargs.get("attachments", [])
        initial_state = {
            "input": message,
            "session_id": session_id,
            "messages": [],
            "output": "",
            "attachments": attachments,
        }
        logger.info(
            f"[FastAgent] initial_state attachments: {len(attachments) if attachments else 0} items"
        )

        try:
            # 用 ensure_future 包装 graph 执行并登记到 _stream_tasks[run_id]，
            # 使外部（如前端“停止”按钮）能通过取消该任务来中断本轮运行。
            graph_task = asyncio.ensure_future(self._graph.ainvoke(initial_state, config))
            self._stream_tasks[presenter.run_id] = graph_task

            await graph_task

        # 被取消：确保底层 graph_task 也被取消并 await 干净，再向上重新抛出。
        except asyncio.CancelledError:
            if not graph_task.done():
                graph_task.cancel()
                try:
                    await graph_task
                except (asyncio.CancelledError, TaskInterruptedError):
                    pass
            raise

        # 任务被中断（如会话超时或主动停止）：同样先让 graph_task 干净收尾再重新抛出。
        except TaskInterruptedError:
            if not graph_task.done():
                graph_task.cancel()
                try:
                    await graph_task
                except (asyncio.CancelledError, TaskInterruptedError):
                    pass
            raise

        # 其它异常：先向前端发出 error 事件（携带异常类名）让 UI 感知失败，再向上抛出。
        except Exception as e:
            yield presenter.error(str(e), type(e).__name__)
            raise

        finally:
            # goal:end 必须在 done 之前发出，保证事件顺序正确
            # 放在 finally 中确保即使异常也能发出
            active_goal = kwargs.get("active_goal")
            goal_started_at = kwargs.get("goal_started_at")
            if active_goal is not None:
                yield {
                    "event": "goal:end",
                    "data": {
                        "goal": active_goal,
                        "started_at": goal_started_at,
                        "ended_at": datetime.now(timezone.utc).isoformat(),
                    },
                }
            # 无论成功或异常都注销任务登记并关闭 context；
            # context.close() 不会关闭全局缓存的 mcp_manager（由其缓存机制自行回收）。
            self._stream_tasks.pop(presenter.run_id, None)
            await context.close()

        # 正常收尾：发出 done 事件标记本轮流式结束。
        yield presenter.done()

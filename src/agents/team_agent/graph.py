"""
Team Agent - 基于角色的团队路由 Agent

特点：
- 无沙箱（使用内存 backend）
- 支持团队配置，按角色分派子代理
- 无团队时回退到单代理模式

架构:
    START -> team_router_node -> END
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict

from langchain_core.runnables import RunnableConfig

# 外层 graph 薄壳所需的核心原语：Agent 基类、graph 构建器、LangSmith 元数据工具、注册装饰器。
from src.agents.core.base import (
    BaseGraphAgent,
    GraphBuilder,
    build_presenter_langsmith_metadata,
    register_agent,
)
from src.agents.team_agent.context import TeamAgentContext
# 真正的 ReAct 循环（deepagents 内层 graph）装配在 team_router_node 里——本文件只是把它挂进外层壳。
from src.agents.team_agent.nodes import team_router_node
from src.agents.team_agent.state import TeamAgentState
from src.infra.backend.context import set_user_context
from src.infra.logging import get_logger
from src.infra.task.exceptions import TaskInterruptedError
from src.infra.writer.present import Presenter, PresenterConfig
from src.kernel.config import settings

logger = get_logger(__name__)


# ============================================================================
# TeamAgent 类
# ============================================================================


# 以 "team" 为 agent_id 把本类注册进全局 Agent 注册表（AgentFactory 据此发现并实例化）。
# fast / search / team 三个 agent 都用同一套 @register_agent 机制注册。
@register_agent("team")
class TeamAgent(BaseGraphAgent):
    """
    Team Agent - 团队路由，角色分派

    适用于：
    - 多角色协作场景
    - 任务分解与分派
    - 无团队时回退到单代理模式
    """

    _agent_id = "team"
    _agent_name = "Team Agent"
    _name_key = "agents.team.name"
    _description = "团队路由 Agent，按角色分派子代理，无团队时回退到单代理模式"
    _description_key = "agents.team.description"
    _version = "1.0.0"
    _sort_order = 3  # 排序权重，数值越小越靠前
    # 声明本 agent 支持沙箱；是否真正启用还取决于全局 settings.ENABLE_SANDBOX（判定在 nodes.py）。
    _supports_sandbox = True

    # 前端可调的运行时选项声明，会随 metadata 下发给 UI 渲染成控件：
    # enable_thinking 控制思考强度（仅支持的模型生效），enable_code_interpreter 开关轻量代码解释器。
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

    # 声明外层 graph 的状态通道 schema（见 state.py）。单行返回，故用行上方注释而非 docstring。
    @property
    def state_class(self) -> type:
        return TeamAgentState

    def build_graph(self, builder: GraphBuilder) -> None:
        """
        构建 Graph

        当前结构: START -> team_router_node -> END
        """
        # 绑定唯一节点到 team_router_node（内层 deep agent 就在该节点内装配并运行）。
        builder.add_node("agent", team_router_node)
        builder.set_entry_point("agent")
        # 单节点直接连到 END，外层不做多节点编排（薄壳）。
        builder.add_edge("agent", "END")

    async def initialize(self) -> None:
        """初始化 Agent"""
        # 幂等保护：已初始化则直接返回，避免重复编译 graph。
        if self._initialized:
            return

        # Keep the outer graph stateless for now: it only wraps one router node, while
        # conversation history is persisted by the inner deep agent checkpointer.
        # If this outer graph grows into a multi-node workflow that needs resume or
        # per-node recovery, add an outer checkpointer with an isolated namespace or
        # thread id so it cannot collide with the inner graph's message state.
        builder = GraphBuilder(self.state_class)
        self.build_graph(builder)
        # 外层保持无状态：checkpointer=None（对话历史由内层 deep agent 的 checkpointer 负责）；
        # recursion_limit 用会话级上限兜底，防止异常情况下 graph 无限递归。
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
        # 惰性初始化：首次执行时才编译外层 graph。
        if not self._initialized:
            await self.initialize()

        # 把 用户 / 会话 写入 contextvar，供 backend 等下游按多租户隔离读取。
        set_user_context(user_id or "default", session_id)

        # 未传入外部 presenter 时自建一个：presenter 负责把运行事件转成流式输出并按需落库。
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

        # 创建并初始化 TeamAgentContext
        disabled_tools = kwargs.get("disabled_tools")
        disabled_skills = kwargs.get("disabled_skills")
        enabled_skills = kwargs.get("enabled_skills")
        disabled_mcp_tools = kwargs.get("disabled_mcp_tools")
        team_id = kwargs.get("team_id")
        # 团队模式下技能改为按角色在节点内解析，故这里把 router 级 enabled_skills 置空，避免重复注入。
        context_enabled_skills = None if team_id else enabled_skills
        context = TeamAgentContext(
            session_id=session_id,
            agent_id=self.agent_id,
            user_id=user_id,
            disabled_tools=disabled_tools,
            disabled_skills=disabled_skills,
            enabled_skills=context_enabled_skills,
            disabled_mcp_tools=disabled_mcp_tools,
            auto_mode=kwargs.get("auto_mode", False),
        )
        # 预加载工具 / 技能等上下文资源（MCP 工具可能在此延迟加载）。
        await context.setup()

        # 发送 metadata
        yield presenter.metadata()

        # 构建 config
        agent_options = kwargs.get("agent_options", {})
        logger.info(f"[TeamAgent] agent_options: {agent_options}")

        # 汇总一批 LangSmith 追踪用的上下文（仅供可观测性，不影响业务逻辑）。
        langsmith_context = {
            "agent_options": agent_options,
            "disabled_skills": disabled_skills,
            "enabled_skills": context_enabled_skills,
            "persona_system_prompt": kwargs.get("persona_system_prompt"),
            "disabled_mcp_tools": disabled_mcp_tools,
            "base_url": kwargs.get("base_url", ""),
            "team_id": team_id,
            "active_goal": kwargs.get("active_goal"),
            "recommendation_input": kwargs.get("recommendation_input"),
            "attachments": kwargs.get("attachments", []),
        }
        langsmith_metadata = await build_presenter_langsmith_metadata(
            presenter,
            langsmith_context,
        )

        config: RunnableConfig = {
            # configurable 里的每个键都会在 team_router_node 内经 config["configurable"] 读取，
            # 这是外层 graph 向节点传参的主要通道（presenter、context、团队与技能设置等）。
            "configurable": {
                "thread_id": session_id,
                "presenter": presenter,
                "context": context,
                "agent_options": agent_options,
                "disabled_skills": disabled_skills,
                "enabled_skills": context_enabled_skills,
                "persona_system_prompt": kwargs.get("persona_system_prompt"),
                "disabled_mcp_tools": disabled_mcp_tools,
                "base_url": kwargs.get("base_url", ""),
                "team_id": team_id,
                "active_goal": kwargs.get("active_goal"),
                "auto_mode": kwargs.get("auto_mode", False),
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
            f"[TeamAgent] initial_state attachments: {len(attachments) if attachments else 0} items"
        )

        try:
            # 用 ensure_future 把 graph 执行包成任务并登记到 _stream_tasks，
            # 便于外部（如用户中断）通过 run_id 找到并取消它。
            graph_task = asyncio.ensure_future(self._graph.ainvoke(initial_state, config))
            self._stream_tasks[presenter.run_id] = graph_task

            await graph_task

        # 被取消 / 被中断时：主动取消底层 graph 任务并等它收尾，再把异常继续抛出，保证资源不泄漏。
        except asyncio.CancelledError:
            if not graph_task.done():
                graph_task.cancel()
                try:
                    await graph_task
                except (asyncio.CancelledError, TaskInterruptedError):
                    pass
            raise

        # 任务被显式中断（如会话超限 / 用户停止）：同样先取消底层任务再抛出。
        except TaskInterruptedError:
            if not graph_task.done():
                graph_task.cancel()
                try:
                    await graph_task
                except (asyncio.CancelledError, TaskInterruptedError):
                    pass
            raise

        # 其他异常：先向前端发一条 error 事件，再抛出交给上层处理。
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
            # 清理任务登记并释放上下文资源（无论成功或异常都要执行）。
            self._stream_tasks.pop(presenter.run_id, None)
            await context.close()

        # 正常结束：发出 done 事件收尾流。
        yield presenter.done()

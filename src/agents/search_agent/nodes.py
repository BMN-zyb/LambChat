"""
Search Agent 节点

LangGraph 节点函数，使用 deep agent 执行任务。
后续可扩展：retrieve_node, summarize_node 等。
"""

import time
import uuid
from typing import Any, Dict

# deepagents.create_deep_agent：装配"内层 ReAct graph"的工厂，是本文件的核心。
# 外层 graph（core/base.py）只是薄壳，真正的"推理-行动"循环由它提供。
from deepagents import create_deep_agent
# 子 agent 类型：SubAgent 为声明式配置，CompiledSubAgent 为已编译好的子图
from deepagents.middleware.subagents import CompiledSubAgent, SubAgent
from langchain_core.runnables import RunnableConfig

from src.agents.core.base import get_presenter
# 节点共享工具：消息构建、嵌套 graph 配置、附件内联（含 SSRF 防护）、
# 模型能力（vision / fallback / image_url_to_base64）解析等
from src.agents.core.node_utils import (
    build_human_message,
    build_nested_graph_configurable,
    emit_token_usage,
    inline_image_attachments_as_data_urls,
    isolated_nested_graph_run,
    resolve_fallback_model,
    resolve_model_image_url_to_base64,
    resolve_model_supports_vision,
)
from src.agents.core.persona import build_persona_prompt_sections
from src.agents.core.subagent_prompts import (
    AUTO_MODE_PROMPT_SECTION,
    CODEBASE_INVESTIGATOR_PROMPT,
    IMPLEMENTATION_WORKER_PROMPT,
    MAIN_AGENT_PROMPT_SECTIONS,
    RESEARCH_SUBAGENT_PROMPT,
    SPECIALIZED_SUBAGENT_DESCRIPTIONS,
    SUBAGENT_PROMPT,
    VERIFICATION_RUNNER_PROMPT,
    get_memory_guide,
)
# 把 enable_thinking 选项解析成模型可用的思考（thinking）配置
from src.agents.core.thinking import build_thinking_config
from src.agents.search_agent.context import SearchAgentContext
from src.agents.search_agent.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    SANDBOX_RUNTIME_SECTION,
    SANDBOX_SYSTEM_PROMPT,
)
from src.infra.agent import AgentEventProcessor
# 内层 graph 的中间件集合：重试、MCP 配额、二进制结果、附件投递、提示分段注入、
# 沙箱 / 环境变量提示、提示缓存、子 agent 活动与结果交接、工具搜索、代码解释器等
from src.infra.agent.middleware import (
    ArtifactDeliveryMiddleware,
    EnvVarPromptMiddleware,
    ImageUrlToBase64Middleware,
    MainAgentContextMiddleware,
    MCPQuotaMiddleware,
    PromptCachingMiddleware,
    SandboxMCPMiddleware,
    SectionPromptMiddleware,
    SubagentActivityMiddleware,
    SubagentResultHandoffMiddleware,
    ToolResultBinaryMiddleware,
    create_code_interpreter_middleware,
    create_retry_middleware,
)
# Backend 工厂：持久化后端（PostgreSQL / MongoDB）与沙箱后端
from src.infra.backend import (
    create_persistent_backend_factory,
    create_sandbox_backend_factory,
)
# Goal（目标）相关：目标输入构建、目标提示分段、目标评分（rubric）中间件
from src.infra.goal import (
    build_goal_input,
    build_goal_prompt_section,
    create_goal_rubric_middleware,
)
from src.infra.llm.client import LLMClient
from src.infra.logging import get_logger
from src.infra.sandbox.session_manager import get_session_sandbox_manager
from src.infra.skill.loader import build_skills_prompt
# 内层 graph 的异步 checkpointer（用 MongoDB 持久化多轮消息历史）
from src.infra.storage.checkpoint import get_async_checkpointer
from src.infra.storage.mongodb_store import acreate_store
from src.infra.writer.present import Presenter
from src.kernel.config import settings

logger = get_logger(__name__)


# ============================================================================
# 节点函数
# ============================================================================


async def agent_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
    """
    Agent 主节点

    创建 deep agent (内层 graph) 并执行，通过 presenter 流式发送事件。
    历史消息从内层 graph 的 checkpoint 获取（MongoDB持久化）。
    """
    start_time = time.time()

    # 从 config.configurable 取出 presenter（事件输出器）、上下文与本轮请求参数
    presenter = get_presenter(config)
    configurable = config.get("configurable", {})
    context: SearchAgentContext = configurable.get("context", SearchAgentContext())

    # 获取 agent_options
    agent_options = configurable.get("agent_options") or {}
    selected_model = agent_options.get("model")  # Per-request model override
    model_id = agent_options.get("model_id")  # Model config ID for specific channel/provider
    resolved_model_config = agent_options.get("_resolved_model_config")
    # 解析思考模式：把 enable_thinking 选项转成模型的 thinking 配置
    thinking_config = build_thinking_config(agent_options)
    logger.info(f"agent_options: {agent_options}")

    # 获取附件
    attachments = state.get("attachments", [])

    # 创建 LLM
    llm_start = time.time()
    # 按选定模型 / 渠道创建 LLM 客户端，并带上思考配置
    llm = await LLMClient.get_model(
        model=selected_model,
        model_id=model_id,
        model_config=resolved_model_config,
        thinking=thinking_config,
    )
    llm_init_time = time.time() - llm_start
    logger.debug(f"[Agent] LLM init: {llm_init_time * 1000:.3f}ms")

    # 查询 fallback_model 配置
    fallback_model_value = agent_options.get("_resolved_fallback_model")
    # 优先复用上游预解析的 fallback 值；键不存在时才回退到查库解析
    if "_resolved_fallback_model" not in agent_options:
        fallback_model_value = await resolve_fallback_model(
            model_id, selected_model, log_prefix="[Agent]"
        )
    # 模型能力：是否支持图片输入（vision）。优先用上游预解析值，缺失才查模型库
    supports_vision = agent_options.get("_resolved_supports_vision")
    if supports_vision is None:
        supports_vision = await resolve_model_supports_vision(
            model_id, selected_model, log_prefix="[Agent]"
        )
    supports_vision = bool(supports_vision)
    # 模型能力：是否需把 image_url 转成 base64 data URL（部分渠道不接受外链图片）
    image_url_to_base64 = agent_options.get("_resolved_image_url_to_base64")
    if image_url_to_base64 is None:
        image_url_to_base64 = await resolve_model_image_url_to_base64(
            model_id, selected_model, log_prefix="[Agent]"
        )
    image_url_to_base64 = bool(image_url_to_base64)

    # 多租户隔离
    tenant_id = context.user_id or "default"
    # 用 user_id 拼出 assistant_id，作为持久化 backend 的命名空间，隔离不同用户的文件
    assistant_id = f"assistant-{tenant_id}"
    logger.info(f"tenant_id: {tenant_id}")

    # 创建 Backend 工厂和获取系统提示
    backend_start = time.time()
    (
        backend_factory,
        system_prompt,
        store,
        sandbox_backend,
        sandbox_work_dir,
    ) = await _create_backend_and_prompt(
        state=state,
        context=context,
        presenter=presenter,
        assistant_id=assistant_id,
    )
    backend_init_time = time.time() - backend_start
    logger.debug(f"[Agent] Backend init: {backend_init_time * 1000:.3f}ms")
    # backend_factory 可能是"工厂函数"或直接的 backend 实例，这里统一成实例
    backend = backend_factory(None) if callable(backend_factory) else backend_factory

    # 构建 persona + skills 提示（使用预加载的 skills，避免重复数据库查询）
    persona_sections = build_persona_prompt_sections(configurable.get("persona_system_prompt"))

    skills_prompt = ""
    if settings.ENABLE_SKILLS and context.skills:
        try:
            skills_prompt = await build_skills_prompt(context.skills)
        except Exception as e:
            logger.warning(f"Failed to build skills prompt: {e}")

    # 构建记忆系统提示
    memory_guide = get_memory_guide() if settings.ENABLE_MEMORY else ""

    # 过滤工具（懒加载 MCP 工具）
    filtered_tools = None
    if settings.ENABLE_MCP:
        # 触发 MCP 工具懒加载，再按黑名单 / auto_mode 过滤出最终工具集
        await context.get_tools()
        filtered_tools = context.filter_tools() or None

        # 延迟加载模式：将 search_tools 注册到 ToolNode
        # 这样 ToolNode 的 tools_by_name 里也有 search_tools，
        # 避免 "not a valid tool" 错误
        if context.deferred_manager is not None and filtered_tools is not None:
            from src.infra.tool.tool_search_tool import ToolSearchTool

            search_tool = ToolSearchTool(
                manager=context.deferred_manager,
                search_limit=settings.DEFERRED_TOOL_SEARCH_LIMIT,
            )
            filtered_tools.append(search_tool)

    # 创建内层 graph (deep agent)
    checkpointer_start = time.time()
    # 内层 graph 的 checkpointer：以 session_id 为 thread_id，把多轮消息历史持久化到 MongoDB
    inner_checkpointer = await get_async_checkpointer(thread_id=state.get("session_id"))
    checkpointer_init_time = time.time() - checkpointer_start
    logger.debug(f"[Agent] Checkpointer init: {checkpointer_init_time * 1000:.3f}ms")

    # 创建 graph（带计时）
    graph_compile_start = time.time()

    # 自定义子代理配置 - 强制将所有中间信息保存到文件
    search_base_url = configurable.get("base_url", "")
    # 子 agent 复用主 agent 的 persona / skills / memory 提示片段（过滤掉空串）
    subagent_prompt_sections = [s for s in (*persona_sections, skills_prompt, memory_guide) if s]
    if sandbox_backend and sandbox_work_dir:
        subagent_prompt_sections.append(SANDBOX_RUNTIME_SECTION.format(work_dir=sandbox_work_dir))

    def _build_subagent_middleware(subagent_type: str) -> list:
        # 为每个子 agent 单独构建一套中间件栈，顺序与主 agent 大体一致：
        # 稳定块在前、动态块在后，最后用 PromptCachingMiddleware 打缓存断点。
        # retry 打头以包住后续中间件；子 agent 独享 fork 出的工具作用域，避免互相污染。
        mw = [
            *create_retry_middleware(fallback_model=fallback_model_value, thinking=thinking_config),
            MCPQuotaMiddleware(user_id=context.user_id),
            ToolResultBinaryMiddleware(base_url=search_base_url),
            ArtifactDeliveryMiddleware(workspace_path=sandbox_work_dir),
            SubagentActivityMiddleware(backend=backend),
        ]
        # 渠道不接受外链图片时，加入把 image_url 转 base64 的中间件
        if image_url_to_base64:
            mw.append(ImageUrlToBase64Middleware())
        # 有提示片段才注入（同类中间件只能有一个实例）
        if subagent_prompt_sections:
            mw.append(SectionPromptMiddleware(sections=subagent_prompt_sections))
        # 沙箱模式追加环境变量提示
        if sandbox_backend:
            mw.append(EnvVarPromptMiddleware(user_id=context.user_id or "default"))
        # 延迟工具加载：子 agent 用 fork 出的独立作用域，接入工具搜索中间件
        if context.deferred_manager is not None:
            from src.infra.agent.middleware import ToolSearchMiddleware

            subagent_deferred_manager = context.deferred_manager.fork_for_scope(
                f"subagent:{subagent_type}"
            )
            mw.append(
                ToolSearchMiddleware(
                    deferred_manager=subagent_deferred_manager,
                    search_limit=settings.DEFERRED_TOOL_SEARCH_LIMIT,
                )
            )
        # 缓存断点放最后：所有注入完成后才标记可缓存前缀
        mw.append(PromptCachingMiddleware())
        return mw

    # 声明可供主 agent 委派的子 agent：通用型 + 若干专精型（代码调查 / 实现 / 验证 / 研究）。
    # 主 agent 通过 task 工具把子任务交给它们推进，各自带独立中间件与系统提示。
    custom_subagents: list[SubAgent | CompiledSubAgent] = [
        {
            "name": "general-purpose",
            "description": "General-purpose agent for researching complex questions, searching for files and content, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. This agent has access to all tools as the main agent.",
            "system_prompt": SUBAGENT_PROMPT,
            "middleware": _build_subagent_middleware("general-purpose"),
        },
        {
            "name": "codebase-investigator",
            "description": SPECIALIZED_SUBAGENT_DESCRIPTIONS["codebase-investigator"],
            "system_prompt": CODEBASE_INVESTIGATOR_PROMPT,
            "middleware": _build_subagent_middleware("codebase-investigator"),
        },
        {
            "name": "implementation-worker",
            "description": SPECIALIZED_SUBAGENT_DESCRIPTIONS["implementation-worker"],
            "system_prompt": IMPLEMENTATION_WORKER_PROMPT,
            "middleware": _build_subagent_middleware("implementation-worker"),
        },
        {
            "name": "verification-runner",
            "description": SPECIALIZED_SUBAGENT_DESCRIPTIONS["verification-runner"],
            "system_prompt": VERIFICATION_RUNNER_PROMPT,
            "middleware": _build_subagent_middleware("verification-runner"),
        },
        {
            "name": "researcher",
            "description": SPECIALIZED_SUBAGENT_DESCRIPTIONS["researcher"],
            "system_prompt": RESEARCH_SUBAGENT_PROMPT,
            "middleware": _build_subagent_middleware("researcher"),
        },
    ]

    # 构建中间件栈：retry → binary → skills+memory → sandbox runtime/tools → memory_index → tool search → cache tag
    # Order: stable → semi-stable → dynamic → cache breakpoint
    # 中间件顺序的核心原则：把"内容稳定"的放前面、"每轮变化"的放后面，最后再打 KV 缓存断点——
    # 这样系统提示前缀尽量不变，最大化命中提示缓存、降低成本。
    # retry 打头：它包住后续所有中间件，模型失败时可切 fallback_model 重试。
    user_middleware = create_retry_middleware(
        fallback_model=fallback_model_value, thinking=thinking_config
    )
    user_middleware.append(MCPQuotaMiddleware(user_id=context.user_id))
    user_middleware.append(ToolResultBinaryMiddleware(base_url=search_base_url))
    user_middleware.append(ArtifactDeliveryMiddleware(workspace_path=sandbox_work_dir))
    if image_url_to_base64:
        user_middleware.append(ImageUrlToBase64Middleware())
    # Prompt sections: one SectionPromptMiddleware instance, multiple ordered blocks.
    # Duplicate middleware classes are rejected by langchain's agent factory.
    _prompt_sections = [
        s
        for s in (*MAIN_AGENT_PROMPT_SECTIONS, *persona_sections, skills_prompt, memory_guide)
        if s
    ]
    # Sandbox runtime is user/session-specific; keep it after global-stable blocks.
    if sandbox_backend:
        if sandbox_work_dir:
            _prompt_sections.append(SANDBOX_RUNTIME_SECTION.format(work_dir=sandbox_work_dir))
    active_goal = configurable.get("active_goal")
    goal_section = build_goal_prompt_section(active_goal)
    if goal_section:
        _prompt_sections.append(goal_section)
    # 自动模式下追加 AUTO_MODE 提示，指导 agent 更自主地连续行动
    if configurable.get("auto_mode"):
        _prompt_sections.append(AUTO_MODE_PROMPT_SECTION)
    if _prompt_sections:
        user_middleware.append(SectionPromptMiddleware(sections=_prompt_sections))
    # Sandbox tool/env prompts are user/session-specific and are appended after static sections.
    # 沙箱工具 / 环境变量提示是"随用户 / 会话变化"的动态内容，放在静态分段之后追加
    if sandbox_backend:
        user_middleware.append(
            SandboxMCPMiddleware(backend=sandbox_backend, user_id=context.user_id or "default")
        )
        user_middleware.append(EnvVarPromptMiddleware(user_id=context.user_id or "default"))
    if settings.ENABLE_MEMORY and settings.NATIVE_MEMORY_INDEX_ENABLED and context.user_id:
        from src.infra.agent.middleware import MemoryIndexMiddleware

        user_middleware.append(MemoryIndexMiddleware(user_id=context.user_id))

    # Tool search: per-turn dynamic content
    # 工具搜索属于"每轮动态内容"，安排在静态分段之后接入
    if context.deferred_manager is not None:
        from src.infra.agent.middleware import ToolSearchMiddleware

        user_middleware.append(
            ToolSearchMiddleware(
                deferred_manager=context.deferred_manager,
                search_limit=settings.DEFERRED_TOOL_SEARCH_LIMIT,
            )
        )
        logger.info("[SearchAgent] Tool search middleware enabled (deferred MCP loading)")

    # 按选项决定是否接入代码解释器中间件（对应 enable_code_interpreter）
    user_middleware.extend(create_code_interpreter_middleware(agent_options))
    # 目标评分中间件：设置了 active_goal 时按 rubric 对产出打分（无目标时返回 None）
    rubric_middleware = create_goal_rubric_middleware(
        model=llm,
        goal=active_goal,
        fallback_model=fallback_model_value,
        thinking=thinking_config,
    )
    if rubric_middleware is not None:
        user_middleware.append(rubric_middleware)

    # 主 agent 专属：注入主 agent 上下文，并负责把子 agent 的结果交接回主流程
    user_middleware.append(MainAgentContextMiddleware(backend=backend))
    user_middleware.append(SubagentResultHandoffMiddleware(backend=backend))

    # KV cache: tag final system block + last tool AFTER all dynamic injection
    # 缓存断点必须放最后：所有动态注入完成后，才标记最终系统块与最后一个工具为可缓存前缀
    user_middleware.append(PromptCachingMiddleware())

    # 用 deepagents 装配"内层 ReAct graph"：这是整个 agent 的推理-行动主体。
    # 依次传入模型、系统提示、backend（文件系统）、工具集、checkpointer（历史持久化）、
    # store（长期记忆）、自定义子 agent，以及上面按顺序拼好的中间件栈。
    inner_graph = create_deep_agent(
        model=llm,
        system_prompt=system_prompt,
        backend=backend,
        tools=filtered_tools,
        checkpointer=inner_checkpointer,
        store=store,  # 传递 PostgresStore
        skills=None,  # 禁用 SkillsMiddleware，使用 build_skills_prompt 代替
        subagents=custom_subagents,
        middleware=user_middleware,
    )
    graph_compile_time = time.time() - graph_compile_start
    logger.debug(f"[Agent] Graph compile: {graph_compile_time * 1000:.3f}ms")

    # 为"手动嵌套调用"的内层 graph 构建 config：注入 checkpointer / backend / presenter 等，
    # 并用同一个 session_id 作为 thread_id，让内层历史与本会话对齐
    inner_config: RunnableConfig = {
        "configurable": build_nested_graph_configurable(
            thread_id=state.get("session_id", str(uuid.uuid4())),
            checkpointer=inner_checkpointer,
            backend=backend,
            context=context,  # 传递 context 以便工具访问 user_id
            disabled_skills=configurable.get("disabled_skills"),
            enabled_skills=configurable.get("enabled_skills"),
            base_url=configurable.get("base_url", ""),  # 传递 base_url 给工具使用
            session_id=state.get("session_id"),
            trace_id=getattr(presenter, "trace_id", None),
            presenter=presenter,  # 传递 presenter 给工具调用
            attachments=attachments,
        ),
        "recursion_limit": config.get("recursion_limit", settings.SESSION_MAX_RUNS_PER_SESSION),
    }

    # 构建传入的新消息（包含附件）
    # 注意：checkpointer + add_messages reducer 会自动维护历史消息，
    # 只需传入新消息，避免与 checkpoint 中的历史消息重复。
    user_input = state.get("input", "")
    recommendation_input = configurable.get("recommendation_input") or user_input
    # 仅当模型支持视觉时才内联图片附件：把图片转成 data URL / 直链；
    # 底层下载器会拒绝私网 / 内网地址（SSRF 防护，见 node_utils._is_private_url）
    if supports_vision:
        attachments = await inline_image_attachments_as_data_urls(
            attachments,
            base_url=configurable.get("base_url", ""),
            force_data_url=image_url_to_base64,
        )
    new_message = build_human_message(user_input, attachments, supports_vision=supports_vision)

    # 创建事件处理器（使用 AgentEventProcessor 处理 astream_events）
    logger.info("[SearchAgent] Creating AgentEventProcessor")
    # 事件处理器：把内层 graph 的 astream_events 原始事件转换成前端 SSE 事件，再经 presenter 发出
    event_processor = AgentEventProcessor(presenter, base_url=configurable.get("base_url", ""))

    # 可选：基于本轮输入异步生成"推荐追问"，不阻塞主流程
    if recommendation_input and settings.ENABLE_RECOMMEND_QUESTIONS:
        from src.agents.core.recommendations import schedule_recommend_questions_from_state

        schedule_recommend_questions_from_state(
            presenter,
            recommendation_input,
            inner_graph,
            inner_config,
        )

    logger.info("[SearchAgent] Starting astream_events")
    # 流式处理事件（不重试，直接调用）
    try:
        # isolated_nested_graph_run：切断父 graph 的运行时 config，避免手动嵌套调用时
        # 继承到外层的任务配置而相互干扰
        async with isolated_nested_graph_run():
            # 事件驱动的流式：逐个消费内层 graph 的 astream_events(v2) 事件交给 event_processor
            # 转成前端事件；build_goal_input 会在设置了目标时包装输入
            async for event in inner_graph.astream_events(  # type: ignore[call-overload]
                build_goal_input(new_message, active_goal, rubric_middleware=rubric_middleware),
                inner_config,
                version="v2",
            ):
                await event_processor.process_event(event)
    finally:
        # 无论正常结束还是异常，都要 flush 残留事件并汇报 token 用量
        await event_processor.flush()
        await emit_token_usage(
            event_processor,
            presenter,
            start_time,
            model_id=model_id,
            model=selected_model,
        )
    logger.info("[SearchAgent] astream_events completed")

    # 可选：异步抓取本轮记忆（写入长期记忆），不阻塞返回
    if settings.ENABLE_MEMORY and context.user_id:
        from src.infra.memory.tools import schedule_auto_memory_capture

        schedule_auto_memory_capture(context.user_id, user_input)

    # 持久化已发现的延迟工具名（跨 turn 恢复，分布式安全）
    session_id = state.get("session_id", "")
    if context.deferred_manager is not None and context.deferred_manager.discovered_count > 0:
        try:
            from src.infra.tool.deferred_manager import persist_discovered_tools

            await persist_discovered_tools(
                session_id,
                context.deferred_manager.discovered_names,
            )
        except Exception:
            pass  # 非关键路径，失败静默

    # 取出本轮聚合的输出文本，清理处理器内存后写回外层 State 的 output
    output_text = event_processor.output_text
    event_processor.clear()

    return {"output": output_text}


async def _create_backend_and_prompt(
    state: Dict[str, Any],
    context: SearchAgentContext,
    presenter: Presenter,
    assistant_id: str,
) -> tuple[Any, str, Any, Any, str | None]:
    """
    创建 Backend 工厂函数和系统提示

    根据是否启用沙箱模式，返回相应的 Backend 工厂和系统提示。
    skills 和 memory_guide 的注入由 SectionPromptMiddleware 在请求时完成（KV cache 友好）。

    Args:
        state: 状态字典
        context: Agent 上下文
        presenter: 输出处理器
        assistant_id: 助手 ID

    Returns:
        (backend_factory, system_prompt, store, sandbox_backend, sandbox_work_dir) 元组。
        sandbox_backend 在沙箱模式下为 CompositeBackend 实例，否则为 None。
    """
    # 创建 store（优先 PostgreSQL → MongoDB fallback）
    store = await acreate_store()

    # 获取 user_id
    user_id = context.user_id or "default"

    if not settings.ENABLE_SANDBOX:
        # 非沙箱模式：使用持久化 backend（PostgreSQL 或 MongoDB，由 store 决定）
        logger.info(f"Sandbox disabled, using PersistentBackend for assistant: {assistant_id}")
        backend_factory = create_persistent_backend_factory(
            assistant_id,
            user_id=user_id,
            session_id=state.get("session_id", str(uuid.uuid4())),
        )
        prompt = DEFAULT_SYSTEM_PROMPT
        # 非沙箱：返回值里 sandbox_backend / sandbox_work_dir 均为 None
        return backend_factory, prompt, store, None, None

    # 沙箱模式
    # 沙箱模式必须有已登录用户（沙箱按 user_id + session_id 隔离）
    if not context.user_id:
        raise ValueError("Sandbox requires authenticated user (user_id is required)")

    sandbox_manager = get_session_sandbox_manager()

    # 发送沙箱开始初始化事件
    try:
        await presenter.emit_sandbox_starting()
    except Exception as e:
        logger.warning(f"Failed to emit sandbox:starting event: {e}")

    try:
        # 按会话获取或创建沙箱，返回复合 backend 与其绝对工作目录
        sandbox_backend, work_dir = await sandbox_manager.get_or_create(
            session_id=state.get("session_id", str(uuid.uuid4())),
            user_id=context.user_id,
        )

        # 发送沙箱就绪事件
        try:
            # 获取 sandbox_id：CompositeBackend.default 可能是 SandboxBackendProtocol
            # 需要安全地访问 id 属性
            sandbox_id = getattr(sandbox_backend.default, "id", "unknown")
            await presenter.emit_sandbox_ready(
                sandbox_id=sandbox_id,
                work_dir=work_dir,
            )
        except Exception as e:
            logger.warning(f"Failed to emit sandbox:ready event: {e}")

        logger.info(f"Sandbox enabled, using sandbox backend for assistant: {assistant_id}")

        # 沙箱模式返回：沙箱 backend 工厂 + 沙箱系统提示 + store + 复合 backend + 工作目录
        return (
            create_sandbox_backend_factory(sandbox_backend.default, assistant_id, user_id=user_id),
            SANDBOX_SYSTEM_PROMPT,
            store,
            sandbox_backend,
            work_dir,
        )

    except Exception as e:
        # 发送沙箱初始化失败事件
        try:
            await presenter.emit_sandbox_error(f"沙箱初始化失败: {str(e)}")
        except Exception as emit_err:
            logger.warning(f"Failed to emit sandbox:error event: {emit_err}")
        raise

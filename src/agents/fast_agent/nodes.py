"""
Fast Agent 节点 - 无沙箱，快速响应

基于 deep_agent/nodes.py 简化，移除沙箱相关逻辑。
"""

import time
import uuid
from typing import Any, Dict

from deepagents import create_deep_agent
from deepagents.middleware.subagents import CompiledSubAgent, SubAgent
from langchain_core.runnables import RunnableConfig

from src.agents.core.base import get_presenter
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
    CODEBASE_INVESTIGATOR_PROMPT,
    IMPLEMENTATION_WORKER_PROMPT,
    MAIN_AGENT_PROMPT_SECTIONS,
    RESEARCH_SUBAGENT_PROMPT,
    SPECIALIZED_SUBAGENT_DESCRIPTIONS,
    SUBAGENT_PROMPT,
    VERIFICATION_RUNNER_PROMPT,
    get_memory_guide,
)
from src.agents.core.thinking import build_thinking_config
from src.agents.fast_agent.context import FastAgentContext
from src.agents.fast_agent.prompt import FAST_SYSTEM_PROMPT
from src.infra.agent import AgentEventProcessor
from src.infra.agent.middleware import (
    ArtifactDeliveryMiddleware,
    ImageUrlToBase64Middleware,
    MainAgentContextMiddleware,
    PromptCachingMiddleware,
    SectionPromptMiddleware,
    SubagentActivityMiddleware,
    SubagentResultHandoffMiddleware,
    ToolResultBinaryMiddleware,
    create_code_interpreter_middleware,
    create_retry_middleware,
)
from src.infra.backend.deepagent import create_persistent_backend_factory
from src.infra.goal import (
    build_goal_input,
    build_goal_prompt_section,
    create_goal_rubric_middleware,
)
from src.infra.llm.client import LLMClient
from src.infra.logging import get_logger
from src.infra.skill.loader import build_skills_prompt
from src.infra.storage.checkpoint import get_async_checkpointer
from src.infra.storage.mongodb_store import acreate_store
from src.kernel.config import settings

logger = get_logger(__name__)


# ============================================================================
# 节点函数
# ============================================================================


async def fast_agent_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
    """
    Fast Agent 主节点 - 无沙箱，快速响应

    特点：
    - 不使用沙箱（直接使用内存 backend）
    - 支持技能（Skills）
    - 支持长期存储（可选）
    - 流式输出
    """
    # 记录起始时间，用于最后统计本轮总耗时与 token 速率。
    start_time = time.time()

    # 从 config.configurable 取出外层 graph 注入的 presenter 与 FastAgentContext；
    # context 缺省时兜底 new 一个空实例，保证异常/单测路径下也不会 KeyError。
    presenter = get_presenter(config)
    configurable = config.get("configurable", {})
    context: FastAgentContext = configurable.get("context", FastAgentContext())

    # 获取 agent_options
    agent_options = configurable.get("agent_options") or {}
    selected_model = agent_options.get("model")  # Per-request model override
    model_id = agent_options.get("model_id")  # Model config ID for specific channel/provider
    resolved_model_config = agent_options.get("_resolved_model_config")
    # 思考模式接入：把 enable_thinking 选项（off/low/medium/high/max）翻译成底层模型的 thinking 配置，
    # 后续既传给 LLMClient，也传给 retry 中间件；仅对支持思考的模型生效，其余模型忽略。
    thinking_config = build_thinking_config(agent_options)

    # 获取附件
    attachments = state.get("attachments", [])

    # 创建 LLM
    llm_start = time.time()
    llm = await LLMClient.get_model(
        model=selected_model,
        model_id=model_id,
        model_config=resolved_model_config,
        thinking=thinking_config,
    )
    llm_init_time = time.time() - llm_start
    logger.debug(f"[FastAgent] LLM init: {llm_init_time * 1000:.3f}ms")

    # 模型能力解析（fallback / 视觉 / image_url 转 base64）统一遵循同一策略：
    # 优先复用上游预解析好的 _resolved_* 值，缺失时才回查模型档案（DB），以减少每轮请求的 DB 访问。
    # fallback_model：主模型调用失败时用于重试的备用模型 value（供 retry 中间件使用）。
    # 查询 fallback_model 配置
    fallback_model_value = agent_options.get("_resolved_fallback_model")
    if "_resolved_fallback_model" not in agent_options:
        fallback_model_value = await resolve_fallback_model(
            model_id, selected_model, log_prefix="[FastAgent]"
        )
    # supports_vision：模型是否支持图像输入，决定附件是否作为多模态图片块拼进消息。
    supports_vision = agent_options.get("_resolved_supports_vision")
    if supports_vision is None:
        supports_vision = await resolve_model_supports_vision(
            model_id, selected_model, log_prefix="[FastAgent]"
        )
    supports_vision = bool(supports_vision)
    # image_url_to_base64：模型是否要求把图片外链先下载并转成 base64 data URL 再发送
    #（某些渠道不支持直接传外部 URL）；下面附件内联时据此决定 force_data_url。
    image_url_to_base64 = agent_options.get("_resolved_image_url_to_base64")
    if image_url_to_base64 is None:
        image_url_to_base64 = await resolve_model_image_url_to_base64(
            model_id, selected_model, log_prefix="[FastAgent]"
        )
    image_url_to_base64 = bool(image_url_to_base64)

    # 多租户隔离
    # 以 user_id 生成每租户独立的 assistant_id，用于隔离持久化 backend 的命名空间，
    # 避免不同用户的虚拟文件/状态互相串扰（无 user_id 时归入 "default"）。
    tenant_id = context.user_id or "default"
    assistant_id = f"assistant-{tenant_id}"

    # 构建 persona + skills 提示
    # persona 拆成 0-2 个提示段（角色 + 行为），稍后由 SectionPromptMiddleware 动态注入，
    # 而非拼进基础 system_prompt——目的是让基础提示词逐字节稳定以命中 KV 缓存。
    persona_sections = build_persona_prompt_sections(configurable.get("persona_system_prompt"))

    # 技能提示：把已加载技能渲染成一段提示文本（同样作为 section 注入，不进基础提示词）。
    skills_prompt = ""
    if settings.ENABLE_SKILLS and context.skills:
        try:
            skills_start = time.time()
            skills_prompt = await build_skills_prompt(context.skills)
            skills_init_time = time.time() - skills_start
            logger.debug(f"[FastAgent] Skills prompt init: {skills_init_time * 1000:.3f}ms")
        except Exception as e:
            logger.warning(f"Failed to build skills prompt: {e}")

    # 构建记忆系统提示
    memory_guide = get_memory_guide() if settings.ENABLE_MEMORY else ""

    # 构建系统提示（persona 由 SectionPromptMiddleware 注入，保持基础提示词稳定以优化 KV 缓存）
    system_prompt = FAST_SYSTEM_PROMPT

    # 创建 backend（无沙箱，PostgreSQL 或 MongoDB 由 store 决定）
    # 无沙箱模式：用“持久化 backend”承载 Agent 的虚拟文件系统（/workflow、/skills 等），
    # 按 assistant_id + user_id + session_id 隔离；工厂可能返回可调用对象，故下方做 callable 判断。
    backend_start = time.time()
    session_id = state.get("session_id", str(uuid.uuid4()))
    backend_factory = create_persistent_backend_factory(
        assistant_id=assistant_id,
        user_id=context.user_id,
        session_id=session_id,
    )
    backend = backend_factory(None) if callable(backend_factory) else backend_factory
    logger.info(f"[FastAgent] Using PersistentBackend for assistant: {assistant_id}")
    backend_init_time = time.time() - backend_start
    logger.debug(f"[FastAgent] Backend init: {backend_init_time * 1000:.3f}ms")

    # 创建 store（优先 PostgreSQL → MongoDB fallback）
    store = await acreate_store()

    # 过滤工具（懒加载 MCP 工具）
    filtered_tools = None
    if settings.ENABLE_MCP:
        # 首次访问触发 MCP 懒加载，再按 disabled_tools/disabled_mcp_tools 过滤；空列表统一转为 None。
        await context.get_tools()
        filtered_tools = context.filter_tools() or None

        # 若启用了延迟工具管理器，额外挂一个 ToolSearchTool：模型可用它按需搜索并“解锁”延迟工具，
        # 从而无需把全部 MCP 工具一次性塞进 prompt（控制上下文体积、稳定 KV 缓存）。
        if context.deferred_manager is not None and filtered_tools is not None:
            from src.infra.tool.tool_search_tool import ToolSearchTool

            search_tool = ToolSearchTool(
                manager=context.deferred_manager,
                search_limit=settings.DEFERRED_TOOL_SEARCH_LIMIT,
            )
            filtered_tools.append(search_tool)

    # Diagnostic: log tool names passed to the LLM
    if filtered_tools is not None:
        tool_names = [getattr(t, "name", str(t)) for t in filtered_tools]
        has_sched = any("scheduled_task" in n for n in tool_names)
        logger.info(
            "[FastAgent] Passing %d tools to create_deep_agent (scheduled_task=%s): %s",
            len(filtered_tools),
            has_sched,
            tool_names,
        )
    else:
        logger.warning("[FastAgent] filtered_tools is None — no tools will be passed to LLM!")

    # 创建内层 graph (deep agent)
    # 内层 deep agent 的 checkpointer：以 session_id 为 thread_id 持久化对话/消息状态，
    # 使多轮历史由内层图维护（外层图无 checkpointer）。这正是后面“只需传新消息”的前提。
    checkpointer_start = time.time()
    inner_checkpointer = await get_async_checkpointer(thread_id=state.get("session_id"))
    checkpointer_init_time = time.time() - checkpointer_start
    logger.debug(f"[FastAgent] Checkpointer init: {checkpointer_init_time * 1000:.3f}ms")

    graph_compile_start = time.time()

    # 自定义子代理配置 - 强制将所有中间信息保存到文件
    subagent_base_url = configurable.get("base_url", "")
    subagent_prompt_sections = [s for s in (*persona_sections, skills_prompt, memory_guide) if s]

    # 子代理共用的中间件工厂。装配顺序体现“稳定→动态→缓存断点”的分层思想：
    #   1) retry（含 fallback/thinking）——最外层，负责失败重试并携带思考配置；
    #   2) ToolResultBinaryMiddleware——把工具返回的二进制/大内容上传后替换为可访问链接；
    #   3) ArtifactDeliveryMiddleware——把产物投递给前端；
    #   4) SubagentActivityMiddleware——把子代理活动写回 backend 供主 agent 汇总；
    #   5) 可选 ImageUrlToBase64——按模型要求把图片 URL 转 base64；
    #   6) 可选 SectionPromptMiddleware——注入 persona/skills/memory 提示段；
    #   7) 可选 ToolSearchMiddleware——延迟工具按需搜索（用 fork 出的独立作用域，避免跨子代理串扰）；
    #   8) PromptCachingMiddleware——放最后，在所有动态注入完成后再打 KV 缓存断点。
    def _build_subagent_middleware(subagent_type: str) -> list:
        mw = [
            *create_retry_middleware(fallback_model=fallback_model_value, thinking=thinking_config),
            ToolResultBinaryMiddleware(base_url=subagent_base_url),
            ArtifactDeliveryMiddleware(),
            SubagentActivityMiddleware(backend=backend),
        ]
        if image_url_to_base64:
            mw.append(ImageUrlToBase64Middleware())
        if subagent_prompt_sections:
            mw.append(SectionPromptMiddleware(sections=subagent_prompt_sections))
        if context.deferred_manager is not None:
            from src.infra.agent.middleware import ToolSearchMiddleware

            # fork 出子代理专属作用域，让每个子代理独立记录自己“发现”的延迟工具，彼此互不影响。
            subagent_deferred_manager = context.deferred_manager.fork_for_scope(
                f"subagent:{subagent_type}"
            )
            mw.append(
                ToolSearchMiddleware(
                    deferred_manager=subagent_deferred_manager,
                    search_limit=settings.DEFERRED_TOOL_SEARCH_LIMIT,
                )
            )
        mw.append(PromptCachingMiddleware())
        return mw

    # 五类内置子代理，供主 agent 通过 task 工具委派子任务：
    #   general-purpose（通用检索/多步任务，可用与主 agent 相同的全部工具）、
    #   codebase-investigator（代码库调查）、implementation-worker（实现改动）、
    #   verification-runner（验证/测试）、researcher（联网研究）。
    # 各自 system_prompt 与 description 取自 subagent_prompts，中间件由上面的工厂按类型生成。
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

    # 构建中间件栈：retry → binary upload → skills+memory → memory_index → tool search → cache tag
    # Order: stable → semi-stable → dynamic → cache breakpoint
    # retry 打头：它包裹整条链，主模型失败时切到 fallback_model，并把 thinking 配置一并带入。
    user_middleware = create_retry_middleware(
        fallback_model=fallback_model_value, thinking=thinking_config
    )
    # 二进制结果转链接：工具产出的二进制/大内容上传后以 URL 回填，避免撑爆上下文。
    user_middleware.append(ToolResultBinaryMiddleware(base_url=subagent_base_url))
    # 产物投递：把生成的文件/产物推送给前端展示。
    user_middleware.append(ArtifactDeliveryMiddleware())
    # 图片 URL 转 base64（仅当模型要求）：放在提示注入之前，确保消息里的图片是模型可接受的形态。
    if image_url_to_base64:
        user_middleware.append(ImageUrlToBase64Middleware())
    # Skills + memory guide: session-static (one SectionPromptMiddleware, multiple blocks)
    # persona_sections returns 0-2 blocks (role + behavior) for fine-grained KV cache
    _prompt_sections = [
        s
        for s in (*MAIN_AGENT_PROMPT_SECTIONS, *persona_sections, skills_prompt, memory_guide)
        if s
    ]
    # 目标（goal）提示段：若本轮带有 active_goal，则把目标说明追加到 section 列表一并注入。
    active_goal = configurable.get("active_goal")
    goal_section = build_goal_prompt_section(active_goal)
    if goal_section:
        _prompt_sections.append(goal_section)
    if _prompt_sections:
        user_middleware.append(SectionPromptMiddleware(sections=_prompt_sections))
    # 记忆索引：把用户长期记忆的轻量索引注入提示（半动态，随用户记忆增删而变），
    # 让模型先看到“有哪些记忆”，再按需 recall 完整内容。
    if settings.ENABLE_MEMORY and settings.NATIVE_MEMORY_INDEX_ENABLED and context.user_id:
        from src.infra.agent.middleware import MemoryIndexMiddleware

        user_middleware.append(MemoryIndexMiddleware(user_id=context.user_id))

    # 延迟工具搜索中间件：让主 agent 能在运行过程中搜索并解锁延迟工具（与前面挂的 ToolSearchTool 呼应）。
    if context.deferred_manager is not None:
        from src.infra.agent.middleware import ToolSearchMiddleware

        user_middleware.append(
            ToolSearchMiddleware(
                deferred_manager=context.deferred_manager,
                search_limit=settings.DEFERRED_TOOL_SEARCH_LIMIT,
            )
        )

    # 代码解释器中间件：仅当用户开启 enable_code_interpreter 时才注入（否则返回空列表，extend 无副作用）。
    user_middleware.extend(create_code_interpreter_middleware(agent_options))
    # 目标评分（rubric）中间件：为带 active_goal 的会话动态评估完成度；无目标时返回 None，不注入。
    rubric_middleware = create_goal_rubric_middleware(
        model=llm,
        goal=active_goal,
        fallback_model=fallback_model_value,
        thinking=thinking_config,
    )
    if rubric_middleware is not None:
        user_middleware.append(rubric_middleware)

    # 主 agent 上下文 & 子代理结果交接：前者让主 agent 感知 backend 中的上下文，
    # 后者把子代理产出整合回主流程。二者靠近末尾，在缓存断点之前完成注入。
    user_middleware.append(MainAgentContextMiddleware(backend=backend))
    user_middleware.append(SubagentResultHandoffMiddleware(backend=backend))

    # KV cache: tag final system block + last tool AFTER all dynamic injection
    user_middleware.append(PromptCachingMiddleware())

    # 组装内层 ReAct graph：deepagents.create_deep_agent 把模型、系统提示、backend、工具、
    # checkpointer、store、子代理与上面拼好的中间件栈编织成一张可循环“思考-行动-观察”的图，
    # 这才是 Fast Agent 真正的执行核心（外层 graph 仅是薄壳）。
    # 注意 skills=None：技能不走 deepagents 的内建技能机制，而是经 SectionPromptMiddleware 以提示段形式注入。
    inner_graph = create_deep_agent(
        model=llm,
        system_prompt=system_prompt,
        backend=backend,
        tools=filtered_tools,
        checkpointer=inner_checkpointer,
        store=store,
        skills=None,
        subagents=custom_subagents,
        middleware=user_middleware,
    )
    graph_compile_time = time.time() - graph_compile_start
    logger.debug(f"[FastAgent] Graph compile: {graph_compile_time * 1000:.3f}ms")

    # 在外层节点内“手动”调用内层图，需显式构造它的 configurable：
    # build_nested_graph_configurable 会写入 thread_id 与 checkpointer 等键，
    # 并把 presenter/backend/context/附件等透传给内层图中的工具与中间件。
    inner_config: RunnableConfig = {
        "configurable": build_nested_graph_configurable(
            thread_id=state.get("session_id", str(uuid.uuid4())),
            checkpointer=inner_checkpointer,
            backend=backend,
            context=context,
            disabled_skills=configurable.get("disabled_skills"),
            enabled_skills=configurable.get("enabled_skills"),
            base_url=configurable.get("base_url", ""),
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
    # 附件内联（仅在模型支持视觉时）：把图片附件转为模型可直接读取的形态。
    # inline_image_attachments_as_data_urls 内部含 SSRF 防护——对远程 URL 会拒绝私网/环回/
    # 链路本地地址以及非 http(s) 协议，防止借“内联附件”探测内网资源；
    # force_data_url 由模型能力（image_url_to_base64）决定是否强制把外链下载转成 base64 data URL。
    if supports_vision:
        attachments = await inline_image_attachments_as_data_urls(
            attachments,
            base_url=configurable.get("base_url", ""),
            force_data_url=image_url_to_base64,
        )
    # 组装本轮 HumanMessage：支持视觉时把图片作为多模态块拼入，其余附件降级为文本摘要附在末尾。
    new_message = build_human_message(user_input, attachments, supports_vision=supports_vision)

    # 创建事件处理器（使用 AgentEventProcessor 处理 astream_events）
    logger.info("[FastAgent] Creating AgentEventProcessor")
    # 事件处理器：消费内层图 astream_events 产生的事件，转换为 presenter 的前端流式输出，并累积最终回复文本。
    event_processor = AgentEventProcessor(presenter, base_url=configurable.get("base_url", ""))

    # 推荐问题：在后台异步基于本轮输入生成“猜你想问”，不阻塞主回答流程。
    if recommendation_input and settings.ENABLE_RECOMMEND_QUESTIONS:
        from src.agents.core.recommendations import schedule_recommend_questions_from_state

        schedule_recommend_questions_from_state(
            presenter,
            recommendation_input,
            inner_graph,
            inner_config,
        )

    logger.info("[FastAgent] Starting astream_events")
    # 流式处理事件（不重试，直接调用）
    try:
        # 事件驱动流式：astream_events(version="v2") 订阅内层图的细粒度事件（token、工具调用/结果等），
        # 逐个交给 event_processor 转发为前端流。isolated_nested_graph_run() 隔离父图的运行配置，
        # 避免手动嵌套调用继承父任务的 callbacks/config，从而污染事件流或引发重复回调。
        async with isolated_nested_graph_run():
            async for event in inner_graph.astream_events(  # type: ignore[call-overload]
                # 带 goal 时，build_goal_input 会把目标信息包进输入，配合 rubric 中间件做目标跟踪。
                build_goal_input(new_message, active_goal, rubric_middleware=rubric_middleware),
                inner_config,
                version="v2",
            ):
                await event_processor.process_event(event)
    finally:
        # 收尾（无论正常结束还是异常）：flush 冲刷缓冲中的残余事件，随后统计并发出本轮 token 用量与耗时。
        await event_processor.flush()
        await emit_token_usage(
            event_processor,
            presenter,
            start_time,
            model_id=model_id,
            model=selected_model,
        )
    logger.info("[FastAgent] astream_events completed")

    # 自动记忆抓取：后台异步从本轮用户输入中沉淀长期记忆（不阻塞节点返回）。
    if settings.ENABLE_MEMORY and context.user_id:
        from src.infra.memory.tools import schedule_auto_memory_capture

        schedule_auto_memory_capture(context.user_id, user_input)

    # 持久化本会话“已发现”的延迟工具名：使下一轮可通过 restore_discovered_tools 继续暴露它们，
    # 让工具解锁状态跨轮次保持连贯（失败则静默忽略，不影响主流程）。
    session_id = state.get("session_id")
    if (
        context.deferred_manager is not None
        and session_id
        and context.deferred_manager.discovered_count > 0
    ):
        try:
            from src.infra.tool.deferred_manager import persist_discovered_tools

            await persist_discovered_tools(
                session_id,
                context.deferred_manager.discovered_names,
            )
        except Exception:
            pass

    # 取出累积的最终回复文本并清理处理器状态，作为外层 graph 节点的返回值写回 state["output"]。
    output_text = event_processor.output_text
    event_processor.clear()

    return {
        "output": output_text,
    }

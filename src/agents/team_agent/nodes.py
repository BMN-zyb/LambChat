"""
Team Agent 节点 - 团队路由，角色分派

基于 fast_agent/nodes.py 扩展，增加团队解析和多角色子代理。
"""

import time
import uuid
from typing import Any, Dict

# deepagents 提供内层 ReAct graph：create_deep_agent(...) 是本文件装配 Agent 运行时的核心。
from deepagents import create_deep_agent
# SubAgent / CompiledSubAgent 是 deepagents 的子代理（团队成员）声明类型，供 create_deep_agent(subagents=...) 使用。
from deepagents.middleware.subagents import CompiledSubAgent, SubAgent
from langchain_core.runnables import RunnableConfig

from src.agents.core.base import get_presenter
# 从 core 抽取的共享节点工具：消息构建、嵌套 graph 的配置/隔离、模型能力解析、附件内联（含 SSRF 防护）等。
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
    build_role_subagent_section,
    get_memory_guide,
)
from src.agents.core.thinking import build_thinking_config
from src.agents.fast_agent.prompt import FAST_SYSTEM_PROMPT
from src.agents.search_agent.prompt import (
    DEFAULT_SYSTEM_PROMPT as SEARCH_DEFAULT_SYSTEM_PROMPT,
)
from src.agents.search_agent.prompt import (
    SANDBOX_RUNTIME_SECTION as SEARCH_SANDBOX_RUNTIME_SECTION,
)
from src.agents.search_agent.prompt import (
    SANDBOX_SYSTEM_PROMPT as SEARCH_SANDBOX_SYSTEM_PROMPT,
)
from src.agents.team_agent.context import TeamAgentContext
from src.agents.team_agent.prompt import (
    build_team_member_subagent_type,
    build_team_router_system_prompt,
    build_team_subagent_avatars,
    build_team_subagent_display_names,
    summarize_role_system_prompt,
)
from src.infra.agent import AgentEventProcessor
# deep agent 的中间件工具箱：重试/降级、提示注入、图片转码、子代理活动记录、结果交接、缓存断点等；
# team_router_node 会把它们按特定顺序拼装成主代理与各子代理的中间件栈。
from src.infra.agent.middleware import (
    ArtifactDeliveryMiddleware,
    EnvVarPromptMiddleware,
    ImageUrlToBase64Middleware,
    MainAgentContextMiddleware,
    PromptCachingMiddleware,
    SandboxMCPMiddleware,
    SectionPromptMiddleware,
    SubagentActivityMiddleware,
    SubagentResultHandoffMiddleware,
    ToolResultBinaryMiddleware,
    create_code_interpreter_middleware,
    create_retry_middleware,
)
from src.infra.backend import (
    create_persistent_backend_factory,
    create_sandbox_backend_factory,
)
from src.infra.goal import (
    build_goal_input,
    build_goal_prompt_section,
    create_goal_rubric_middleware,
)
from src.infra.llm.client import LLMClient
from src.infra.logging import get_logger
from src.infra.sandbox.session_manager import get_session_sandbox_manager
from src.infra.skill.loader import build_skills_prompt
from src.infra.storage.checkpoint import get_async_checkpointer
from src.infra.storage.mongodb_store import acreate_store
from src.kernel.config import settings
from src.kernel.schemas.model import ModelConfig

logger = get_logger(__name__)


# ============================================================================
# 节点函数
# ============================================================================


# 未显式选择团队时，为"单代理回退模式"挑选系统提示词：
# 沙箱开启就用带存储架构说明的 search 提示，否则用精简的 fast 提示。
def build_no_team_fallback_system_prompt(*, sandbox_active: bool) -> str:
    """Choose the single-agent fallback prompt when no explicit team is selected."""
    if sandbox_active:
        return SEARCH_SANDBOX_SYSTEM_PROMPT
    return FAST_SYSTEM_PROMPT


# 解析本次运行要用的团队：只有显式传入 team_id 才尝试解析；没有 team_id 一律返回 None 走单代理回退。
# 关键副作用：team_id 给了却解析不到（团队不存在 / 无激活成员 / 无权限）时抛 ValueError，
# 让上层明确报错，而不是悄悄退化成单代理。
async def resolve_runtime_team(
    *,
    team_id: str | None,
    context: TeamAgentContext,
    user_input: str,
):
    """Resolve an explicit team; no team means single-agent fallback."""
    # 目前不依据输入内容自动选团队，仅按显式 team_id；保留形参以兼容调用约定，这里显式丢弃。
    del user_input
    # 匿名用户没有团队概念，直接走单代理回退。
    if not context.user_id:
        return None

    # 只有显式传入 team_id 才查团队管理器。
    if team_id:
        try:
            from src.infra.team.manager import get_team_manager

            tm = get_team_manager()
            team = await tm.resolve_team_for_runtime(team_id, owner_user_id=context.user_id)
            if team:
                logger.info(
                    f"[TeamAgent] Resolved team '{team.name}' "
                    f"with {len(team.active_members)} active members"
                )
                return team
            logger.info("[TeamAgent] Team resolved to None (no active members or not found)")
            raise ValueError("team_not_found_or_unavailable")
        except Exception as e:
            if isinstance(e, ValueError) and str(e) == "team_not_found_or_unavailable":
                raise
            logger.warning(f"[TeamAgent] Failed to resolve team: {e}")
            raise ValueError("team_not_found_or_unavailable") from e

    return None


# 解析并校验团队成员的"模型覆盖"：允许某个角色使用与主模型不同的模型。
# 两道关卡：模型必须存在且已启用；若带 user_id，还要确认该用户被允许使用此模型（防越权用未授权模型）。
async def resolve_team_member_model_config(
    member_model_id: str | None,
    *,
    user_id: str | None = None,
) -> ModelConfig | None:
    """Resolve and validate a team member model override for runtime use."""
    # 没配置覆盖模型则返回 None，沿用主模型。
    if not member_model_id:
        return None

    from src.infra.agent.model_storage import get_model_storage

    try:
        model = await get_model_storage().get(member_model_id)
    except Exception as e:
        logger.warning("[TeamAgent] Failed to resolve member model %s: %s", member_model_id, e)
        raise ValueError("team_member_model_unavailable") from e

    # 模型不存在或被禁用，视为不可用。
    if not model or not model.enabled:
        raise ValueError("team_member_model_unavailable")

    # 有登录用户时，进一步做模型访问权限校验。
    if user_id:
        try:
            from src.infra.agent.model_access import resolve_user_allowed_model_ids
            from src.infra.user.storage import UserStorage
            from src.kernel.schemas.user import TokenPayload

            user = await UserStorage().get_by_id(user_id)
            if not user:
                raise ValueError("team_member_model_not_allowed")
            allowed_model_ids = await resolve_user_allowed_model_ids(
                TokenPayload(
                    sub=user.id,
                    username=user.username,
                    roles=user.roles,
                    permissions=user.permissions,
                )
            )
            # allowed_model_ids 为 None 表示不限制；非 None 才按白名单校验模型 id / value。
            if allowed_model_ids is not None:
                allowed = set(allowed_model_ids)
                if model.id not in allowed and model.value not in allowed:
                    raise ValueError("team_member_model_not_allowed")
        except ValueError:
            raise
        except Exception as e:
            logger.warning(
                "[TeamAgent] Failed to validate member model access %s: %s",
                member_model_id,
                e,
            )
            raise ValueError("team_member_model_unavailable") from e
    return model


# 序列化成员模型配置前先抹掉 api_key，避免把密钥透传进 deep agent 的 config / 日志（安全考虑）。
def _safe_member_model_config_dict(model: ModelConfig) -> dict[str, Any]:
    return model.model_copy(update={"api_key": None}).model_dump(mode="json")


# 解析并校验团队成员的"agent 模式覆盖"（例如让某角色以 fast / search 模式运行）。
# 特别地：成员不能再用 "team" 模式（禁止团队套团队的递归）；还要校验该 agent 已注册且当前用户有权使用。
async def resolve_team_member_agent_id(
    member_agent_id: str | None,
    *,
    user_id: str | None = None,
) -> str | None:
    """Resolve and validate a team member agent mode override for runtime use."""
    # 未指定则返回 None，使用默认子代理行为。
    if not member_agent_id:
        return None

    # 禁止成员再嵌套 "team" 模式（否则会团队套团队、无限递归）。
    if member_agent_id == "team":
        raise ValueError("team_member_agent_unavailable")

    from src.agents.core.base import AgentFactory

    # 覆盖的 agent 必须是已注册的 agent id。
    registered_agent_ids = {agent["id"] for agent in AgentFactory.list_agents()}
    if member_agent_id not in registered_agent_ids:
        raise ValueError("team_member_agent_unavailable")

    role_ids: list[str] = []
    role_agent_map: dict[str, list[str] | None] = {}
    try:
        # 有登录用户时，按其角色可见的 agent 白名单再校验一次（RBAC 过滤）。
        if user_id:
            from src.infra.agent.config_storage import get_agent_config_storage
            from src.infra.role.manager import get_role_manager
            from src.infra.user.storage import UserStorage

            user = await UserStorage().get_by_id(user_id)
            if not user:
                raise ValueError("team_member_agent_not_allowed")

            storage = get_agent_config_storage()
            role_manager = get_role_manager()
            for role_name in user.roles or []:
                role = await role_manager.get_role_by_name(role_name)
                if not role:
                    continue
                role_ids.append(role.id)
                role_agent_map[role.id] = await storage.get_role_agents(role.id)

        allowed_agents = await AgentFactory.get_filtered_agents(
            user_roles=role_ids,
            role_agent_map=role_agent_map,
        )
        allowed_agent_ids = {agent["id"] for agent in allowed_agents}
        if member_agent_id not in allowed_agent_ids:
            raise ValueError("team_member_agent_not_allowed")
    except ValueError:
        raise
    except Exception as e:
        logger.warning(
            "[TeamAgent] Failed to validate member agent access %s: %s",
            member_agent_id,
            e,
        )
        raise ValueError("team_member_agent_unavailable") from e

    return member_agent_id


# 依据成员的 agent 模式，返回要额外注入该子代理的提示词小节，让子代理带上对应模式（fast / search）的行为约定。
def _build_member_agent_mode_sections(
    agent_id: str | None,
    *,
    sandbox_active: bool,
) -> list[str]:
    """Return mode-specific prompt sections for a team member subagent."""
    # 无覆盖则不加任何模式小节。
    if not agent_id:
        return []
    # fast 模式：注入 FAST 系统提示。
    if agent_id == "fast":
        return [FAST_SYSTEM_PROMPT]
    # search 模式：按是否启用沙箱选择对应的 search 提示。
    if agent_id == "search":
        return [SEARCH_SANDBOX_SYSTEM_PROMPT if sandbox_active else SEARCH_DEFAULT_SYSTEM_PROMPT]
    return []


async def team_router_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
    """
    Team Router 主节点 - 团队路由，角色分派

    特点：
    - 解析团队配置，按角色构建子代理
    - 使用 SectionPromptMiddleware 为每个角色注入角色、技能、记忆和运行时提示
    - 无团队时回退到单代理模式
    """
    start_time = time.time()

    # 从 config 取回外层 graph（graph.py）放入的 presenter / configurable / context。
    presenter = get_presenter(config)
    configurable = config.get("configurable", {})
    context: TeamAgentContext = configurable.get("context", TeamAgentContext())

    # 获取 agent_options
    agent_options = configurable.get("agent_options") or {}
    selected_model = agent_options.get("model")
    model_id = agent_options.get("model_id")
    resolved_model_config = agent_options.get("_resolved_model_config")
    # 把 enable_thinking 选项解析为提供商可识别的思考配置（模型不支持时为 None）。
    thinking_config = build_thinking_config(agent_options)

    # 获取附件
    attachments = state.get("attachments", [])

    # 创建 LLM
    llm_start = time.time()
    # 按选中的模型 + 思考配置创建主 LLM 客户端。
    llm = await LLMClient.get_model(
        model=selected_model,
        model_id=model_id,
        model_config=resolved_model_config,
        thinking=thinking_config,
    )
    llm_init_time = time.time() - llm_start
    logger.debug(f"[TeamAgent] LLM init: {llm_init_time * 1000:.3f}ms")

    # 查询 fallback_model 配置
    # 优先复用调用方预解析好的 _resolved_* 值（避免每轮都查库）；缺失时才实时查 DB。
    fallback_model_value = agent_options.get("_resolved_fallback_model")
    if "_resolved_fallback_model" not in agent_options:
        fallback_model_value = await resolve_fallback_model(
            model_id, selected_model, log_prefix="[TeamAgent]"
        )
    # 模型是否支持视觉输入：决定图片附件是作为多模态块传入，还是仅转成文本摘要。
    supports_vision = agent_options.get("_resolved_supports_vision")
    if supports_vision is None:
        supports_vision = await resolve_model_supports_vision(
            model_id, selected_model, log_prefix="[TeamAgent]"
        )
    supports_vision = bool(supports_vision)
    # 某些模型/网关要求把 image_url 转成 base64 data URL 才能识别，这里解析该开关。
    image_url_to_base64 = agent_options.get("_resolved_image_url_to_base64")
    if image_url_to_base64 is None:
        image_url_to_base64 = await resolve_model_image_url_to_base64(
            model_id, selected_model, log_prefix="[TeamAgent]"
        )
    image_url_to_base64 = bool(image_url_to_base64)

    # 多租户隔离
    # 以用户 id 派生 assistant_id，作为 backend 的隔离键，保证不同用户数据互不串扰。
    tenant_id = context.user_id or "default"
    assistant_id = f"assistant-{tenant_id}"

    # ── 团队解析 ──
    user_input = state.get("input", "")
    # 解析本次运行的团队：显式 team_id 才解析，否则返回 None 走单代理回退（详见函数内说明）。
    team = await resolve_runtime_team(
        team_id=configurable.get("team_id"),
        context=context,
        user_input=user_input,
    )

    # ── 系统提示 ──
    # In explicit team mode the main agent is only the router/synthesizer.
    # Role persona and skills are injected into the matching member subagents.
    # 团队模式下主代理只当 router/汇总者，故它本身不加 persona（persona 注入到各成员子代理里）；
    # 只有单代理模式才给主代理注入 persona 小节。
    persona_sections = (
        [] if team else build_persona_prompt_sections(configurable.get("persona_system_prompt"))
    )

    skills_prompt = ""
    # 构建技能系统提示（可能较慢，故计时）。
    if settings.ENABLE_SKILLS and context.skills:
        try:
            skills_start = time.time()
            skills_prompt = await build_skills_prompt(context.skills)
            skills_init_time = time.time() - skills_start
            logger.debug(f"[TeamAgent] Skills prompt init: {skills_init_time * 1000:.3f}ms")
        except Exception as e:
            logger.warning(f"Failed to build skills prompt: {e}")
    # 团队模式下 router 不直接用技能（技能按角色注入到成员子代理）；单代理模式才给 router 用。
    router_skills_prompt = "" if team else skills_prompt

    memory_guide = get_memory_guide() if settings.ENABLE_MEMORY else ""
    role_system_prompts: dict[str, str] = {}
    role_skill_prompts: dict[str, str] = {}
    role_summaries: dict[str, str] = {}

    # 团队模式：为每个成员解析 persona 预设、该角色专属技能提示，以及给 router 展示用的能力摘要。
    if team and team.active_members:
        try:
            from src.infra.persona_preset.manager import get_persona_preset_manager

            preset_mgr = get_persona_preset_manager()
            # 逐成员取 persona 预设快照（含 system_prompt 与技能名列表）。
            for member in team.active_members:
                preset_snapshot = await preset_mgr.use_preset(
                    member.persona_preset_id,
                    user_id=context.user_id or "default",
                    is_admin=False,
                )
                role_system_prompts[member.member_id] = preset_snapshot.system_prompt
                role_skill_names = set(getattr(preset_snapshot, "skill_names", []) or [])
                # 角色若指定了技能子集，只给它这部分技能；否则沿用全量技能提示。
                if role_skill_names:
                    role_skills = [
                        skill for skill in context.skills if skill.get("name") in role_skill_names
                    ]
                    role_skill_prompts[member.member_id] = await build_skills_prompt(role_skills)
                else:
                    role_skill_prompts[member.member_id] = skills_prompt
                summary = summarize_role_system_prompt(preset_snapshot.system_prompt)
                if summary:
                    role_summaries[member.member_id] = summary
        except Exception as e:
            logger.warning(f"[TeamAgent] Failed to resolve team member preset prompts: {e}")
            raise ValueError("team_member_preset_unavailable") from e

    # 计算 router 的兜底角色 default_role：优先团队指定的 default_member，
    # 其次团队第一个成员，都没有才用 deepagents 内置的 general-purpose。
    if team:
        default_role = "general-purpose"
        if team.default_member_id:
            default_member = next(
                (m for m in team.active_members if m.member_id == team.default_member_id),
                team.active_members[0] if team.active_members else None,
            )
            default_role = (
                build_team_member_subagent_type(default_member)
                if default_member
                else "general-purpose"
            )
        else:
            default_role = (
                build_team_member_subagent_type(team.active_members[0])
                if team.active_members
                else "general-purpose"
            )
        system_prompt = build_team_router_system_prompt(
            team,
            default_role=default_role,
            role_summaries=role_summaries,
        )
    # 无团队：主代理直接用 fast 系统提示（单代理模式）。
    else:
        system_prompt = FAST_SYSTEM_PROMPT
    # 同理，团队模式下运行时 enabled_skills 置空（技能已按角色注入）。
    runtime_enabled_skills = None if team else configurable.get("enabled_skills")

    # 创建 backend
    backend_start = time.time()
    sandbox_backend = None
    sandbox_work_dir = None

    # 沙箱关闭：用持久化 backend（无隔离沙箱进程），按 assistant_id / user / session 做数据隔离。
    if not settings.ENABLE_SANDBOX:
        session_id = state.get("session_id", str(uuid.uuid4()))
        backend_factory = create_persistent_backend_factory(
            assistant_id=assistant_id,
            user_id=context.user_id,
            session_id=session_id,
        )
        logger.info(
            f"[TeamAgent] Sandbox disabled, using PersistentBackend for assistant: {assistant_id}"
        )
    # 沙箱开启：必须有登录用户；建/取本会话专属沙箱，期间向前端发 starting/ready/error 事件。
    else:
        if not context.user_id:
            raise ValueError("Sandbox requires authenticated user (user_id is required)")

        sandbox_manager = get_session_sandbox_manager()
        try:
            await presenter.emit_sandbox_starting()
        except Exception as e:
            logger.warning(f"Failed to emit sandbox:starting event: {e}")

        try:
            # 取或创建本会话沙箱，拿到沙箱后端与工作目录（work_dir）。
            sandbox_backend, sandbox_work_dir = await sandbox_manager.get_or_create(
                session_id=state.get("session_id", str(uuid.uuid4())),
                user_id=context.user_id,
            )
            try:
                sandbox_id = getattr(sandbox_backend.default, "id", "unknown")
                await presenter.emit_sandbox_ready(
                    sandbox_id=sandbox_id,
                    work_dir=sandbox_work_dir,
                )
            except Exception as e:
                logger.warning(f"Failed to emit sandbox:ready event: {e}")

            backend_factory = create_sandbox_backend_factory(
                sandbox_backend.default,
                assistant_id,
                user_id=context.user_id,
            )
            # 沙箱模式下，把"存储架构"说明拼到系统提示前：团队模式拼在已建好的 router 提示前，
            # 单代理模式则整体改用沙箱回退提示。
            if team:
                system_prompt = f"{SEARCH_SANDBOX_SYSTEM_PROMPT}\n\n{system_prompt}"
            else:
                system_prompt = build_no_team_fallback_system_prompt(sandbox_active=True)
            logger.info(
                f"[TeamAgent] Sandbox enabled, using sandbox backend for assistant: {assistant_id}"
            )
        except Exception as e:
            try:
                await presenter.emit_sandbox_error(f"沙箱初始化失败: {str(e)}")
            except Exception as emit_err:
                logger.warning(f"Failed to emit sandbox:error event: {emit_err}")
            raise

    # backend_factory 可能是工厂函数（需调用生成实例）或直接就是实例，这里做兼容。
    backend = backend_factory(None) if callable(backend_factory) else backend_factory
    backend_init_time = time.time() - backend_start
    logger.debug(f"[TeamAgent] Backend init: {backend_init_time * 1000:.3f}ms")

    # 创建 store
    # 长期记忆 / 键值存储，供内层 deep agent 的 store=... 使用。
    store = await acreate_store()

    # 过滤工具（懒加载 MCP 工具）
    filtered_tools = None
    # 仅在启用 MCP 时才加载并过滤工具（懒加载，降低启动开销）。
    if settings.ENABLE_MCP:
        await context.get_tools()
        filtered_tools = context.filter_tools() or None

        # 存在延迟工具管理器时，额外挂一个 ToolSearchTool：让模型可按需检索海量工具，而非一次性全塞进上下文。
        if context.deferred_manager is not None and filtered_tools is not None:
            from src.infra.tool.tool_search_tool import ToolSearchTool

            search_tool = ToolSearchTool(
                manager=context.deferred_manager,
                search_limit=settings.DEFERRED_TOOL_SEARCH_LIMIT,
            )
            filtered_tools.append(search_tool)

    # 创建内层 graph (deep agent)
    checkpointer_start = time.time()
    # 内层 deep agent 的 checkpointer：用 session_id 作 thread_id 持久化对话历史。
    # 这就是为什么外层 graph 可以无 checkpointer（见 graph.py）——历史由内层负责。
    inner_checkpointer = await get_async_checkpointer(thread_id=state.get("session_id"))
    checkpointer_init_time = time.time() - checkpointer_start
    logger.debug(f"[TeamAgent] Checkpointer init: {checkpointer_init_time * 1000:.3f}ms")

    graph_compile_start = time.time()

    # ── 子代理配置 ──
    subagent_base_url = configurable.get("base_url", "")

    # 为单个子代理装配中间件栈。列表顺序即"洋葱式"包裹顺序（靠前的在外层、更先执行）：
    # 重试/降级（最外，兜底整段调用）-> 工具二进制结果处理 -> 沙箱工件交付 -> 子代理活动记录
    # ->（可选）图片转 base64 ->（可选）角色/技能等提示注入 ->（沙箱时）环境变量名提示
    # ->（有延迟工具时）工具检索 -> 最后 PromptCaching 重打缓存断点。
    def _build_subagent_middleware(
        subagent_type: str = "general-purpose",
        prompt_sections: list[str] | None = None,
        fallback_model: str | None = fallback_model_value,
        should_convert_image_url_to_base64: bool = image_url_to_base64,
    ) -> list:
        """Build the middleware stack for a single subagent."""
        # 基础层：无论何种子代理都需要的中间件。
        mw = [
            *create_retry_middleware(fallback_model=fallback_model, thinking=thinking_config),
            ToolResultBinaryMiddleware(base_url=subagent_base_url),
            ArtifactDeliveryMiddleware(workspace_path=sandbox_work_dir),
            SubagentActivityMiddleware(backend=backend),
        ]
        # 仅当模型要求时才追加图片转 base64。
        if should_convert_image_url_to_base64:
            mw.append(ImageUrlToBase64Middleware())
        # 有角色/技能等提示小节时，用 SectionPromptMiddleware 逐段注入（每段独立成块，利于缓存断点）。
        if prompt_sections:
            mw.append(SectionPromptMiddleware(sections=prompt_sections))
        # 沙箱下注入允许使用的环境变量名（只暴露名字、不含值）。
        if sandbox_backend:
            mw.append(EnvVarPromptMiddleware(user_id=context.user_id or "default"))
        # 为该子代理 fork 一个独立作用域的延迟工具检索器，避免不同子代理的工具发现互相污染。
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
        # 缓存断点重打必须放最后：要等所有动态提示注入完成后，才把 cache_control 贴到
        # 系统提示稳定前缀的末尾，从而最大化 KV 缓存命中率。
        mw.append(PromptCachingMiddleware())
        return mw

    # 待传给 create_deep_agent(subagents=...) 的子代理列表（团队成员，或无团队时的内置专家）。
    custom_subagents: list[SubAgent | CompiledSubAgent] = []
    subagent_display_names: dict[str, str] = {}
    subagent_avatars: dict[str, str] = {}
    # 沙箱运行时提示（含 work_dir），供子代理提示注入；非沙箱时为 None。
    subagent_runtime_section = (
        SEARCH_SANDBOX_RUNTIME_SECTION.format(work_dir=sandbox_work_dir)
        if sandbox_backend and sandbox_work_dir
        else None
    )

    # 团队模式：把每个激活团队成员各装配成一个 deep agent 子代理，router 通过 task 工具分派给它们。
    if team and team.active_members:
        # ── 多角色子代理 ──
        try:
            subagent_display_names = build_team_subagent_display_names(team)
            subagent_avatars = build_team_subagent_avatars(team)

            # 逐成员装配子代理。
            for member in team.active_members:
                subagent_type = build_team_member_subagent_type(member)
                role_name = member.role_name or subagent_type
                # 解析并校验该成员的 agent 模式覆盖（如 fast/search），不合法会抛错中断。
                member_agent_id = await resolve_team_member_agent_id(
                    member.agent_id,
                    user_id=context.user_id,
                )
                # 解析并校验该成员的模型覆盖（允许角色用不同模型）。
                member_model_config = await resolve_team_member_model_config(
                    member.model_id,
                    user_id=context.user_id,
                )
                member_model = None
                member_fallback_model = fallback_model_value
                member_image_url_to_base64 = image_url_to_base64
                # 成员配置了独立模型时，单独建其 LLM，并同步解析该模型对应的 fallback 与图片转码开关。
                if member_model_config is not None:
                    member_model = await LLMClient.get_model(
                        model=member_model_config.value,
                        model_id=member_model_config.id,
                        model_config=_safe_member_model_config_dict(member_model_config),
                        thinking=thinking_config,
                    )
                    member_fallback_model = await resolve_fallback_model(
                        member_model_config.id,
                        member_model_config.value,
                        log_prefix=f"[TeamAgent:{subagent_type}]",
                    )
                    member_image_url_to_base64 = bool(
                        getattr(member_model_config.profile, "image_url_to_base64", False)
                        if member_model_config.profile
                        else False
                    )
                    logger.info(
                        "[TeamAgent] Role subagent model override: type=%s role=%s model_id=%s model=%s",
                        subagent_type,
                        role_name,
                        member_model_config.id,
                        member_model_config.value,
                    )
                # 拼出该角色的人格/团队/角色指令提示小节（把 persona 预设与角色约束整合成一段）。
                role_section = build_role_subagent_section(
                    role_name=role_name,
                    role_system_prompt=role_system_prompts[member.member_id],
                    team_name=team.name,
                    team_instructions=team.team_instructions or None,
                    role_instructions=member.role_instructions or None,
                )
                # 组装该子代理完整的提示小节：模式提示 + 角色小节 + 技能 + 记忆 + 运行时，并过滤掉空段。
                role_prompt_sections = [
                    s
                    for s in (
                        *_build_member_agent_mode_sections(
                            member_agent_id,
                            sandbox_active=bool(sandbox_backend),
                        ),
                        role_section,
                        role_skill_prompts.get(member.member_id, skills_prompt),
                        memory_guide,
                        subagent_runtime_section,
                    )
                    if s
                ]
                logger.info(
                    "[TeamAgent] Role subagent prompt built: type=%s role=%s "
                    "section_chars=%d has_role_prompt=%s has_role_instructions=%s "
                    "has_skills=%s",
                    subagent_type,
                    role_name,
                    sum(len(s) for s in role_prompt_sections),
                    bool(role_system_prompts[member.member_id].strip())
                    and role_system_prompts[member.member_id].strip() in role_section,
                    bool((member.role_instructions or "").strip())
                    and (member.role_instructions or "").strip() in role_section,
                    any("## Skills System" in s for s in role_prompt_sections),
                )
                if member_agent_id:
                    logger.info(
                        "[TeamAgent] Role subagent agent mode override: type=%s role=%s agent_id=%s",
                        subagent_type,
                        role_name,
                        member_agent_id,
                    )

                # 子代理声明：name 用稳定的 subagent_type（与 router 提示里的分派目标一致），
                # description 告诉 router 何时该把任务分派给它，system_prompt 用通用 SUBAGENT_PROMPT，
                # middleware 用上面按成员定制的中间件栈（含角色提示、专属模型的 fallback/转码等）。
                subagent_config: SubAgent = {
                    "name": subagent_type,
                    "description": (
                        f"Team member '{role_name}' "
                        f"(member_id: {member.member_id}). "
                        f"Dispatch tasks matching this role's expertise."
                        + (f" {member.role_instructions}" if member.role_instructions else "")
                    ),
                    "system_prompt": SUBAGENT_PROMPT,
                    "middleware": _build_subagent_middleware(
                        subagent_type,
                        prompt_sections=role_prompt_sections,
                        fallback_model=member_fallback_model,
                        should_convert_image_url_to_base64=member_image_url_to_base64,
                    ),
                }
                # 仅当该成员有模型覆盖时才写入 model 字段；否则继承主代理模型。
                if member_model is not None:
                    subagent_config["model"] = member_model
                custom_subagents.append(subagent_config)

            logger.info(
                f"[TeamAgent] Built {len(custom_subagents)} role subagents for team '{team.name}'"
            )
        # 已知的成员级"不可用/越权"错误原样上抛（供上层区分并提示用户）；
        # 其它异常统一归一为 team_subagents_unavailable。
        except ValueError as e:
            if str(e) in {
                "team_member_agent_unavailable",
                "team_member_agent_not_allowed",
                "team_member_model_unavailable",
                "team_member_model_not_allowed",
            }:
                raise
            logger.error(f"[TeamAgent] Failed to build team subagents: {e}")
            raise ValueError("team_subagents_unavailable") from e
        except Exception as e:
            logger.error(f"[TeamAgent] Failed to build team subagents: {e}")
            raise ValueError("team_subagents_unavailable") from e

    # Fallback: built-in specialist subagents when no explicit team is selected
    # 没有显式团队时，装配一组内置专家子代理（通用/代码调查/实现/验证/研究），
    # 让单代理也能像团队一样把子任务分派给专门角色。它们共用同一套提示小节。
    if not custom_subagents:
        subagent_prompt_sections = [
            s
            for s in (*persona_sections, skills_prompt, memory_guide, subagent_runtime_section)
            if s
        ]
        custom_subagents = [
            {
                "name": "general-purpose",
                "description": "General-purpose agent for researching complex questions, searching for files and content, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. This agent has access to all tools as the main agent.",
                "system_prompt": SUBAGENT_PROMPT,
                "middleware": _build_subagent_middleware(
                    "general-purpose",
                    prompt_sections=subagent_prompt_sections,
                ),
            },
            {
                "name": "codebase-investigator",
                "description": SPECIALIZED_SUBAGENT_DESCRIPTIONS["codebase-investigator"],
                "system_prompt": CODEBASE_INVESTIGATOR_PROMPT,
                "middleware": _build_subagent_middleware(
                    "codebase-investigator",
                    prompt_sections=subagent_prompt_sections,
                ),
            },
            {
                "name": "implementation-worker",
                "description": SPECIALIZED_SUBAGENT_DESCRIPTIONS["implementation-worker"],
                "system_prompt": IMPLEMENTATION_WORKER_PROMPT,
                "middleware": _build_subagent_middleware(
                    "implementation-worker",
                    prompt_sections=subagent_prompt_sections,
                ),
            },
            {
                "name": "verification-runner",
                "description": SPECIALIZED_SUBAGENT_DESCRIPTIONS["verification-runner"],
                "system_prompt": VERIFICATION_RUNNER_PROMPT,
                "middleware": _build_subagent_middleware(
                    "verification-runner",
                    prompt_sections=subagent_prompt_sections,
                ),
            },
            {
                "name": "researcher",
                "description": SPECIALIZED_SUBAGENT_DESCRIPTIONS["researcher"],
                "system_prompt": RESEARCH_SUBAGENT_PROMPT,
                "middleware": _build_subagent_middleware(
                    "researcher",
                    prompt_sections=subagent_prompt_sections,
                ),
            },
        ]

    # ── 主代理中间件栈 ──
    # 主代理（router / 单代理）的中间件栈，与子代理同构但更完整。最外层放重试/降级作兜底。
    user_middleware = create_retry_middleware(
        fallback_model=fallback_model_value, thinking=thinking_config
    )
    # 处理工具返回的二进制 / 大体积结果。
    user_middleware.append(ToolResultBinaryMiddleware(base_url=subagent_base_url))
    # 侦测沙箱产物并发出工件事件。
    user_middleware.append(ArtifactDeliveryMiddleware(workspace_path=sandbox_work_dir))
    # 模型要求时把 image_url 转成 base64。
    if image_url_to_base64:
        user_middleware.append(ImageUrlToBase64Middleware())
    # 主代理系统提示的动态小节：主代理工作流 + persona + router 技能 + 记忆，过滤掉空段。
    _prompt_sections = [
        s
        for s in (
            *MAIN_AGENT_PROMPT_SECTIONS,
            *persona_sections,
            router_skills_prompt,
            memory_guide,
        )
        if s
    ]
    # 沙箱时追加运行时 work_dir 小节。
    if sandbox_backend and sandbox_work_dir:
        _prompt_sections.append(SEARCH_SANDBOX_RUNTIME_SECTION.format(work_dir=sandbox_work_dir))
    # 本轮若带目标（goal），追加目标提示小节。
    active_goal = configurable.get("active_goal")
    goal_section = build_goal_prompt_section(active_goal)
    if goal_section:
        _prompt_sections.append(goal_section)
    # auto 模式追加自动模式提示小节。
    if configurable.get("auto_mode"):
        _prompt_sections.append(AUTO_MODE_PROMPT_SECTION)
    # 有动态小节才挂 SectionPromptMiddleware（每段独立成块，利于缓存断点划分）。
    if _prompt_sections:
        user_middleware.append(SectionPromptMiddleware(sections=_prompt_sections))
    # 沙箱时挂 SandboxMCP（把沙箱工具说明注入到系统提示尾部）与环境变量名提示。
    if sandbox_backend:
        user_middleware.append(
            SandboxMCPMiddleware(backend=sandbox_backend, user_id=context.user_id or "default")
        )
        user_middleware.append(EnvVarPromptMiddleware(user_id=context.user_id or "default"))
    # 原生记忆索引开启且有用户时，挂 MemoryIndex 注入记忆索引。
    if settings.ENABLE_MEMORY and settings.NATIVE_MEMORY_INDEX_ENABLED and context.user_id:
        from src.infra.agent.middleware import MemoryIndexMiddleware

        user_middleware.append(MemoryIndexMiddleware(user_id=context.user_id))

    # 有延迟工具时，挂主代理级工具检索中间件。
    if context.deferred_manager is not None:
        from src.infra.agent.middleware import ToolSearchMiddleware

        user_middleware.append(
            ToolSearchMiddleware(
                deferred_manager=context.deferred_manager,
                search_limit=settings.DEFERRED_TOOL_SEARCH_LIMIT,
            )
        )

    # 目标评分中间件（有 goal 时才返回非 None）：用于按评分标准检查目标是否达成。
    rubric_middleware = create_goal_rubric_middleware(
        model=llm,
        goal=active_goal,
        fallback_model=fallback_model_value,
        thinking=thinking_config,
    )
    # 按 agent_options 挂轻量代码解释器中间件（可能为空列表）。
    user_middleware.extend(create_code_interpreter_middleware(agent_options))
    if rubric_middleware is not None:
        user_middleware.append(rubric_middleware)

    # 启动子代理任务前，把父对话上下文写入 backend 供子代理读取（团队协作的上下文传递）。
    user_middleware.append(MainAgentContextMiddleware(backend=backend))
    # 子代理完成后，把其最终报告搬进交接文件，供主代理汇总时消费。
    user_middleware.append(SubagentResultHandoffMiddleware(backend=backend))

    # 同子代理：缓存断点重打必须放在整个用户中间件链的最后。
    user_middleware.append(PromptCachingMiddleware())

    # 用前面装配好的 模型 / 系统提示 / backend / 工具 / checkpointer / store / 子代理 / 中间件，
    # 编译出内层 deep agent 的 ReAct graph——这才是真正的 Agent 循环（外层 graph 只是薄壳）。
    # 注意 skills=None：技能不走 deepagents 的 skills 机制，而是通过中间件以提示小节形式注入。
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
    logger.debug(f"[TeamAgent] Graph compile: {graph_compile_time * 1000:.3f}ms")

    # 手动在外层节点里调用内层 graph，需要自建一份 configurable：
    # 用 build_nested_graph_configurable 正确设置 thread_id / checkpointer / checkpoint_ns，
    # 并携带 backend、context、presenter、附件等，避免与外层 graph 的状态冲突。
    inner_config: RunnableConfig = {
        "configurable": build_nested_graph_configurable(
            thread_id=state.get("session_id", str(uuid.uuid4())),
            checkpointer=inner_checkpointer,
            backend=backend,
            context=context,
            disabled_skills=configurable.get("disabled_skills"),
            enabled_skills=runtime_enabled_skills,
            base_url=configurable.get("base_url", ""),
            session_id=state.get("session_id"),
            trace_id=getattr(presenter, "trace_id", None),
            presenter=presenter,
            attachments=attachments,
        ),
        "recursion_limit": config.get("recursion_limit", settings.SESSION_MAX_RUNS_PER_SESSION),
    }

    # 构建传入的新消息（包含附件）
    # 推荐追问的输入源（缺省用本轮用户输入）。
    recommendation_input = configurable.get("recommendation_input") or user_input
    # 仅当模型支持视觉时才内联图片附件；inline_image_attachments_as_data_urls 内部会对
    # 私有/内网地址做 SSRF 防护，并按大小上限决定是否真正内联成 data URL。
    if supports_vision:
        attachments = await inline_image_attachments_as_data_urls(
            attachments,
            base_url=configurable.get("base_url", ""),
            force_data_url=image_url_to_base64,
        )
    # 把文本 + 附件组装成 HumanMessage：图片作为多模态块，其余附件转成文本摘要附加。
    new_message = build_human_message(user_input, attachments, supports_vision=supports_vision)

    # 创建事件处理器
    logger.info("[TeamAgent] Creating AgentEventProcessor")
    # 事件处理器：把内层 graph 的事件翻译成前端流式协议，并借 display_names/avatars 把子代理显示成对应角色。
    event_processor = AgentEventProcessor(
        presenter,
        base_url=configurable.get("base_url", ""),
        subagent_display_names=subagent_display_names,
        subagent_avatars=subagent_avatars,
    )

    # 满足条件时后台调度"推荐追问"生成（复用同一内层 graph/config，不阻塞主回答流）。
    if recommendation_input and settings.ENABLE_RECOMMEND_QUESTIONS:
        from src.agents.core.recommendations import schedule_recommend_questions_from_state

        schedule_recommend_questions_from_state(
            presenter,
            recommendation_input,
            inner_graph,
            inner_config,
        )

    logger.info("[TeamAgent] Starting astream_events")
    # 事件驱动流式消费：逐个取出内层 graph 的事件交给 processor（模型 token、工具调用、子代理活动等）。
    try:
        # 用隔离上下文运行手动嵌套的 graph，避免继承外层 graph 任务的 runnable config 造成事件/回调串扰。
        async with isolated_nested_graph_run():
            # v2 事件流：内层 ReAct 循环的所有中间产物都以事件形式实时流出。
            async for event in inner_graph.astream_events(  # type: ignore[call-overload]
                # 把新消息与目标（及评分中间件）打包成内层 graph 的输入。
                build_goal_input(new_message, active_goal, rubric_middleware=rubric_middleware),
                inner_config,
                version="v2",
            ):
                await event_processor.process_event(event)
    # 无论成功或异常，都要 flush 残留事件并上报本轮 token 用量与耗时。
    finally:
        await event_processor.flush()
        await emit_token_usage(
            event_processor,
            presenter,
            start_time,
            model_id=model_id,
            model=selected_model,
        )
    logger.info("[TeamAgent] astream_events completed")

    # 开启记忆时，后台异步抽取本轮对话中的可记忆信息（不阻塞本次返回）。
    if settings.ENABLE_MEMORY and context.user_id:
        from src.infra.memory.tools import schedule_auto_memory_capture

        schedule_auto_memory_capture(context.user_id, user_input)

    session_id = state.get("session_id")
    # 本轮若通过工具检索发现了新的延迟工具，持久化到该会话，供后续轮次直接复用。
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

    # 取本轮聚合出的最终回答文本，作为节点输出；随后清理事件处理器状态。
    output_text = event_processor.output_text
    event_processor.clear()

    # 写回外层 state 的 output 通道（外层 graph 到此 END，见 graph.py）。
    return {"output": output_text}

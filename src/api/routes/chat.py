"""
聊天路由

支持后台执行的聊天接口。
每次对话生成独立的 run_id，实现多轮对话隔离。
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.agents.core import resolve_agent_name
from src.agents.core.base import AgentFactory
from src.api.deps import get_current_user_required, require_permissions
from src.api.routes.auth.utils import _get_language
from src.api.routes.chat_validation import validate_team_agent_request
from src.api.routes.session import verify_session_ownership
from src.infra.async_utils import run_blocking_io
from src.infra.chat.user_message_timestamp import format_user_message_with_timestamp
from src.infra.goal import GoalSpec, coerce_goal_spec
from src.infra.logging import get_logger
from src.infra.persona_preset.manager import PersonaPresetManager
from src.infra.session.manager import SessionManager
from src.infra.task.cancellation import _close_agent_safely
from src.infra.task.concurrency import register_executor
from src.infra.task.manager import get_task_manager
from src.infra.task.status import TaskStatus
from src.kernel.config import settings
from src.kernel.exceptions import AuthorizationError, NotFoundError
from src.kernel.schemas.agent import AgentRequest
from src.kernel.schemas.model import ModelConfig
from src.kernel.schemas.persona_preset import PersonaPresetSnapshot
from src.kernel.schemas.user import TokenPayload

# 本模块挂载于 /api/chat，是全站最核心的聊天链路，采用"两段式"设计：
# 1) POST /stream 提交消息后立即返回 session_id/run_id，真正的 agent 推理放到后台任务执行；
# 2) GET /sessions/{id}/stream 通过 SSE 从 Redis Stream 流式读取该 run 的事件并推送给前端。
# 两段以 run_id（对话轮次 ID）关联；前端断线后可凭 run_id 重新连接并回放已产生的事件。
router = APIRouter()
logger = get_logger(__name__)

# 单条 SSE data 字段允许的最大字节数（256KB）；超限时改发 error 事件，避免超大 payload 拖垮前端或反向代理。
CHAT_SSE_DATA_MAX_BYTES = 256 * 1024


# 把本轮显式选中的技能拼接到用户消息末尾，生成 <required_skills> 提示块，
# 要求模型在回答前先读取并遵循各技能对应的 SKILL.md；enabled_skills 为空时原样返回。
def append_required_skills_prompt(message: str, enabled_skills: list[str] | None) -> str:
    """Append a run-scoped instruction for explicitly selected skills."""
    # 未显式选中任何技能，直接返回原始消息
    if not enabled_skills:
        return message

    # 拼出"- 技能名: /skills/技能名/SKILL.md"清单，供模型定位每个技能的说明文件
    skill_paths = "\n".join(f"- {name}: /skills/{name}/SKILL.md" for name in enabled_skills if name)
    # 过滤空技能名后若无有效项，同样返回原始消息
    if not skill_paths:
        return message

    return (
        f"{message}\n\n"
        "<required_skills>\n"
        "Required skills for this message:\n"
        f"{skill_paths}\n\n"
        "You must read and follow the SKILL.md instructions for each required skill "
        "before answering. Use these skills for this message unless the request is "
        "impossible or unsafe, and clearly say so if you cannot use them.\n"
        "</required_skills>"
    )


# 把模型的 profile（能力档案，如是否支持视觉）序列化为 dict；无 profile 时返回 None。
def _model_profile_dict(model: ModelConfig) -> dict | None:
    if not model.profile:
        return None
    return (
        model.profile.model_dump() if hasattr(model.profile, "model_dump") else dict(model.profile)
    )


# 把模型配置序列化为 JSON dict，同时把 api_key 抹成 None，避免密钥泄露到会话元数据/前端。
def _safe_model_config_dict(model: ModelConfig) -> dict:
    return model.model_copy(update={"api_key": None}).model_dump(mode="json")


# 递归估算对象序列化成 JSON 后的字节数，用于在真正编码前快速判断 SSE 数据是否超限（避免无谓的完整编码）。
def _estimated_json_data_bytes(data: object) -> int:
    if data is None or isinstance(data, (bool, int, float)):
        return len(json.dumps(data, default=str).encode("utf-8"))
    if isinstance(data, str):
        return len(data.encode("utf-8")) + 2
    if isinstance(data, dict):
        total = 2
        for index, (key, value) in enumerate(data.items()):
            if index:
                total += 1
            total += len(str(key).encode("utf-8")) + 3
            total += _estimated_json_data_bytes(value)
        return total
    if isinstance(data, (list, tuple)):
        total = 2
        for index, item in enumerate(data):
            if index:
                total += 1
            total += _estimated_json_data_bytes(item)
        return total
    return len(str(data).encode("utf-8")) + 2


# 构造一条"数据过大"的 SSE error 事件文本；保留原事件 id，便于前端定位与断点续读。
def _chat_sse_payload_too_large_event(event_id: object | None) -> str:
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f'event: error\ndata: {{"error":"event_payload_too_large"}}\n{id_line}\n'


# 带大小上限地把数据编码为 JSON 字符串：先估算、再边编码边累加字节数，任一步超过上限即返回 None。
def _json_dumps_chat_sse_data_limited(data: object) -> str | None:
    if _estimated_json_data_bytes(data) > CHAT_SSE_DATA_MAX_BYTES:
        return None

    encoder = json.JSONEncoder(ensure_ascii=False, default=str)
    chunks: list[str] = []
    total = 0
    for chunk in encoder.iterencode(data):
        total += len(chunk.encode("utf-8"))
        if total > CHAT_SSE_DATA_MAX_BYTES:
            return None
        chunks.append(chunk)
    return "".join(chunks)


# 把内部事件 dict 格式化为标准 SSE 帧："event: 类型\ndata: JSON\nid: 事件ID\n\n"。
# 若数据体带 timestamp，则注入 data 的 _timestamp 字段；数据超限时返回 error 帧。
def _format_sse_event(event: dict) -> str:
    event_data = event["data"]
    if isinstance(event_data, dict) and event.get("timestamp"):
        event_data = {**event_data, "_timestamp": event["timestamp"]}
    data_str = _json_dumps_chat_sse_data_limited(event_data)
    if data_str is None:
        return _chat_sse_payload_too_large_event(event.get("id"))
    return f"event: {event['event_type']}\ndata: {data_str}\nid: {event['id']}\n\n"


# 把已解析的模型信息（id、value、能力档案、是否支持视觉、兜底模型等）写入 agent_options，
# 使后台执行器无需再查库即可直接使用；同时缓存 api_key。这些 _resolved_* 字段供下游按需读取。
async def _attach_resolved_model_options(agent_options: dict, model: ModelConfig) -> None:
    """Persist resolved model details in request options to avoid repeated DB lookups."""
    agent_options["model_id"] = model.id
    agent_options["model"] = model.value
    agent_options["_resolved_model_config"] = _safe_model_config_dict(model)
    agent_options["_resolved_supports_vision"] = bool(
        getattr(model.profile, "supports_vision", False)
    )
    agent_options["_resolved_image_url_to_base64"] = bool(
        getattr(model.profile, "image_url_to_base64", False)
    )
    if model.api_key:
        from src.infra.llm.models_service import set_cached_api_key

        set_cached_api_key(model.value, model.api_key)

    fallback_value = None
    if model.fallback_model:
        from src.infra.agent.model_storage import get_model_storage

        try:
            fallback = await get_model_storage().get(model.fallback_model)
            if fallback and fallback.enabled:
                fallback_value = fallback.value
        except Exception as e:
            logger.warning("Failed to resolve fallback model %s: %s", model.fallback_model, e)
    agent_options["_resolved_fallback_model"] = fallback_value
    agent_options["_resolved_model_profile"] = _model_profile_dict(model)


# 校验本次请求所选模型的可用性与权限：
# - 未指定模型时，从用户角色允许的模型里挑第一个启用的作为默认；一个都没有则报 model_disabled；
# - 指定了模型时，要求该模型存在且启用，且在用户角色允许的白名单内，否则报 model_disabled / model_not_allowed。
# 校验通过后把解析结果回填到 agent_options。
async def validate_agent_model_access(
    agent_options: dict | None,
    user: TokenPayload,
) -> None:
    """Validate per-request model selection against enabled models and role access."""
    if agent_options is None:
        agent_options = {}

    model_id = agent_options.get("model_id")
    selected_model = agent_options.get("model")

    from src.infra.agent.model_storage import get_model_storage

    storage = get_model_storage()
    from src.infra.agent.model_access import resolve_user_allowed_model_ids

    allowed_model_ids = await resolve_user_allowed_model_ids(user)

    # 请求未指定任何模型：allowed_model_ids 为 None 表示不限制，直接放行（用系统默认）
    if not model_id and not selected_model:
        if allowed_model_ids is None:
            return
        # 从角色允许列表中选出第一个启用的模型作为本次默认模型
        for allowed_model_id in allowed_model_ids:
            model = await storage.get(allowed_model_id)
            if not model:
                model = await storage.get_by_value(allowed_model_id)
            if model and model.enabled:
                await _attach_resolved_model_options(agent_options, model)
                return
        raise AuthorizationError("model_disabled")

    model = None
    if isinstance(model_id, str) and model_id:
        model = await storage.get(model_id)
    elif isinstance(selected_model, str) and selected_model:
        model = await storage.get_by_value(selected_model)

    # 指定的模型不存在或已被禁用
    if not model or not model.enabled:
        raise AuthorizationError("model_disabled")

    # allowed_model_ids 非 None 时执行白名单校验：模型的 id 或 value 命中其一即视为允许
    allowed_model_set = set(allowed_model_ids or [])
    if allowed_model_ids is not None and (
        model.id not in allowed_model_set and model.value not in allowed_model_set
    ):
        raise AuthorizationError("model_not_allowed")

    await _attach_resolved_model_options(agent_options, model)


# 把本轮对话的完整配置（agent、模型选项、禁用工具/技能、人设、语言等）写入 session 的 metadata，
# 便于刷新页面或断线重连时恢复对话上下文，也供后台执行器读取。
async def _update_session_config(
    session_id: str,
    run_id: str,
    agent_id: str,
    request: AgentRequest,
    language: str,
    trace_id: str | None = None,
) -> None:
    """Update session metadata with conversation configuration."""
    session_manager = SessionManager()
    conversation_config = build_conversation_config(
        session_id=session_id,
        run_id=run_id,
        agent_id=agent_id,
        request=request,
        language=language,
        trace_id=trace_id,
    )
    await session_manager.update_session_metadata(session_id, conversation_config)


# 解析本轮请求的 goal（目标/任务规格）。注意：goal 仅作用于当前 run，不从 session 继承
# （existing_metadata 被显式忽略），从而实现多轮对话之间的目标隔离。
def resolve_goal_for_request(
    request: AgentRequest,
    existing_metadata: dict | None,
) -> tuple[GoalSpec | None, str]:
    """Resolve the run-scoped goal for this request without session inheritance."""
    _ = existing_metadata
    active_goal = coerce_goal_spec(request.goal)
    request.goal = active_goal
    return active_goal, request.message


# 从人设快照中提取技能白名单：仅当人设配置了 skill_names 时返回，否则返回 None（表示不限制）。
def _persona_enabled_skills_from_snapshot(
    snapshot: PersonaPresetSnapshot,
) -> list[str] | None:
    """Return a whitelist only when the persona has usable skills."""
    if snapshot.skill_names:
        return snapshot.skill_names
    return None


# 构建写入 session metadata 的对话配置 dict；下面逐字段说明其含义。
def build_conversation_config(
    run_id: str,
    agent_id: str,
    request: AgentRequest,
    language: str,
    session_id: str | None = None,
    trace_id: str | None = None,
) -> dict:
    """Build session metadata for conversation configuration."""
    conversation_config = {
        # 当前对话轮次 ID
        "current_run_id": run_id,
        # 使用的 Agent ID
        "agent_id": agent_id,
        # 后台执行器 key，固定为 agent_stream（对应 _execute_agent_stream）
        "executor_key": "agent_stream",
        # 传给 agent 的自定义选项（含解析后的模型信息）
        "agent_options": request.agent_options or {},
        # 本轮禁用的内置工具列表
        "disabled_tools": request.disabled_tools or [],
        # 本轮禁用的技能列表
        "disabled_skills": request.disabled_skills or [],
        # 本轮显式启用（白名单）的技能列表
        "enabled_skills": request.enabled_skills,
        # 本轮禁用的 MCP 工具列表
        "disabled_mcp_tools": request.disabled_mcp_tools or [],
        # 回复使用的语言
        "language": language,
        # 是否自动模式（无需人工确认即继续执行）
        "auto_mode": request.auto_mode,
    }
    # 以下均为可选字段，仅在对应值存在时才写入 metadata（trace 追踪、人设、项目、时区、团队等）
    if trace_id:
        conversation_config["trace_id"] = trace_id
    if request.persona_preset_id:
        conversation_config["persona_preset_id"] = request.persona_preset_id
    if request.persona_preset_id and request.persona_snapshot:
        conversation_config["persona_preset_name"] = request.persona_snapshot.name
        conversation_config["persona_snapshot"] = request.persona_snapshot.model_dump()
        if request.persona_snapshot.avatar:
            conversation_config["persona_avatar"] = request.persona_snapshot.avatar
    if request.project_id:
        conversation_config["project_id"] = request.project_id
    if request.user_timezone:
        conversation_config["user_timezone"] = request.user_timezone
    if agent_id == "team" and request.team_id:
        conversation_config["team_id"] = request.team_id
    return conversation_config


# 解析人设预设：先清空客户端可能自带的 persona_snapshot / persona_system_prompt（防止提示注入），
# 再按 persona_preset_id 拉取服务端权威快照，并据此覆盖启用技能与系统提示词。
async def resolve_persona_request(
    request: AgentRequest,
    user: TokenPayload,
    manager: PersonaPresetManager | None = None,
) -> None:
    """Resolve persona preset data and drop any client-supplied prompt injection."""
    request.persona_snapshot = None
    request.persona_system_prompt = None

    # 未选择人设预设，直接返回（保持已清空状态）
    if not request.persona_preset_id:
        return

    # 以服务端权威数据为准拉取人设快照（内部会校验归属与管理员权限）
    persona_manager = manager or PersonaPresetManager()
    snapshot = await persona_manager.use_preset(
        request.persona_preset_id,
        user_id=user.sub,
        is_admin="persona_preset:admin" in (user.permissions or []),
    )
    request.persona_snapshot = snapshot
    request.enabled_skills = _persona_enabled_skills_from_snapshot(snapshot)
    request.persona_system_prompt = snapshot.system_prompt


# 后台任务的实际执行体（已注册为 "agent_stream" 执行器）：以异步生成器逐条 yield 事件，
# 由 TaskManager/Presenter 写入 Redis Stream，再经 SSE 推送到前端。
# 若带 active_goal，则在开始/结束处补发 goal:start / goal:end 事件用于前端展示目标进度。
async def _execute_agent_stream(
    session_id: str,
    agent_id: str,
    message: str,
    user_id: str,
    presenter=None,
    disabled_tools: list[str] | None = None,
    agent_options: dict | None = None,
    attachments: list[dict] | None = None,
    disabled_skills: list[str] | None = None,
    enabled_skills: list[str] | None = None,
    persona_system_prompt: str | None = None,
    disabled_mcp_tools: list[str] | None = None,
    team_id: str | None = None,
    active_goal: dict | None = None,
    recommendation_input: str | None = None,
    auto_mode: bool = False,
):
    """执行 Agent 并流式输出事件（供 TaskManager 调用）"""
    from src.infra.task.manager import TaskInterruptedError

    run_id = presenter.run_id if presenter else None

    # goal_end_emitted 用于去重：agent 内部可能已发过 goal:end，避免这里重复补发
    started_at: str | None = None
    goal_end_emitted = False
    # 有目标时，先补发 goal:start 事件并记录开始时间
    if active_goal is not None:
        started_at = datetime.now(timezone.utc).isoformat()
        yield {"event": "goal:start", "data": {"goal": active_goal, "started_at": started_at}}

    try:
        # 按 agent_id 获取 agent 实例，并把它产生的每一个事件透传出去
        agent = await AgentFactory.get(agent_id)
        async for event in agent.stream(
            message,
            session_id,
            user_id=user_id,
            presenter=presenter,
            disabled_tools=disabled_tools,
            agent_options=agent_options,
            attachments=attachments,
            disabled_skills=disabled_skills,
            enabled_skills=enabled_skills,
            persona_system_prompt=persona_system_prompt,
            disabled_mcp_tools=disabled_mcp_tools,
            team_id=team_id,
            active_goal=active_goal,
            auto_mode=auto_mode,
            goal_started_at=started_at,
            recommendation_input=recommendation_input,
        ):
            # 记录 agent 是否已自行发出 goal:end，避免下面重复补发
            if event.get("event") == "goal:end":
                goal_end_emitted = True
            yield event

        # 正常结束但 agent 未发过 goal:end 时，这里补一条
        if active_goal is not None and not goal_end_emitted:
            ended_at = datetime.now(timezone.utc).isoformat()
            yield {
                "event": "goal:end",
                "data": {"goal": active_goal, "started_at": started_at, "ended_at": ended_at},
            }
    except (asyncio.CancelledError, TaskInterruptedError):
        # 取消/中断时，调用 agent.close 清理资源
        if run_id:
            await _close_agent_safely(agent, run_id)
        # agent 的 finally 块可能已发 goal:end，此处再 yield 确保不遗漏（Presenter 有去重）
        if active_goal is not None and not goal_end_emitted:
            ended_at = datetime.now(timezone.utc).isoformat()
            yield {
                "event": "goal:end",
                "data": {"goal": active_goal, "started_at": started_at, "ended_at": ended_at},
            }
        raise


# 把默认执行器注册到全局，使任意 worker（含 arq 后台进程）都能据 executor_key 派发排队任务
# Register the default agent-stream executor so any worker can dispatch queued tasks
register_executor("agent_stream", _execute_agent_stream)


# POST /api/chat/stream —— 提交一条聊天消息（两段式的第一段）。
# 权限：需要 chat:write。请求体为 AgentRequest（message、session_id、attachments、人设、目标等）。
# 立即返回 session_id/run_id/status，真正的推理在后台任务中执行；前端随后用 SSE 接口取事件。
# 受基于角色的并发限制：超并发则排队（返回 queued），队列也满则返回 429。
@router.post("/stream")
async def chat_stream(
    request: AgentRequest,
    http_request: Request,
    agent_id: str = "search",
    user: TokenPayload = Depends(require_permissions("chat:write")),
):
    """
    提交聊天任务，立即返回 session_id 和 run_id

    任务在后台执行，前端可通过 SSE 或轮询获取结果。
    支持基于角色的并发限制：达到上限时排队等待，队列满时返回 429。

    Args:
        request: 包含 message 和 session_id
        agent_id: 要使用的 Agent ID（默认: search）

    Returns:
        session_id: 会话 ID
        run_id: 当前对话轮次的运行 ID
        trace_id: 追踪 ID
        status: 任务状态 (pending / queued)
        queue_position: 排队位置（仅排队时返回）
    """
    from src.infra.task.concurrency import ConcurrencyResult, get_concurrency_limiter
    from src.infra.task.manager import _generate_run_id

    # 复用前端传入的 session_id；未传则视为新会话，生成一个新的 UUID
    session_id = request.session_id or str(uuid.uuid4())
    # team 类型 agent 的额外参数校验（如必须携带 team_id）
    validate_team_agent_request(agent_id, request)

    # 如果用户传入了 session_id，验证所有权
    existing_metadata: dict = {}
    if request.session_id:
        session_manager = SessionManager()
        existing_session = await session_manager.get_session(session_id)
        if existing_session:
            verify_session_ownership(existing_session, user)
            existing_metadata = existing_session.metadata or {}

    # 解析本轮目标，并取出实际要发给 agent 的消息文本
    active_goal, agent_message = resolve_goal_for_request(request, existing_metadata)
    active_goal_data = active_goal.model_dump() if active_goal else None
    task_manager = get_task_manager()
    # 从请求头解析用户偏好语言
    preferred_language = _get_language(http_request)

    try:
        # 解析人设预设并校验所选模型的可用性与权限（失败抛业务异常，下面转换为对应 HTTP 错误）
        await resolve_persona_request(request, user)
        if request.agent_options is None:
            request.agent_options = {}
        await validate_agent_model_access(request.agent_options, user)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="角色预设不存在")
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))

    # 给用户消息注入本地时间戳，再追加"必须使用的技能"提示块，得到最终发给 agent 的文本
    formatted_message = format_user_message_with_timestamp(
        agent_message,
        request.user_timezone,
    )
    formatted_message = append_required_skills_prompt(
        formatted_message,
        request.enabled_skills,
    )

    # 生成 run_id（不管是否排队都需要唯一 ID）
    run_id = _generate_run_id()

    # Prepare attachments (needed for both queued and direct paths)
    attachments_data = (
        [a.model_dump() for a in request.attachments] if request.attachments else None
    )

    # 提前构造一个 Presenter 只为拿到 trace_id（追踪 ID），使排队/执行两条路径复用同一 trace
    # Build task context for queued dispatch (stored in Redis, multi-worker safe)
    # trace_id is generated early so it can be passed to the executor for trace reuse
    from src.infra.writer.present import Presenter, PresenterConfig

    _pre_presenter = Presenter(
        PresenterConfig(
            session_id=session_id,
            agent_id=agent_id,
            agent_name=resolve_agent_name(agent_id),
            user_id=user.sub,
            run_id=run_id,
            enable_storage=False,
        )
    )
    trace_id = _pre_presenter.trace_id

    # 任务上下文：排队时会整体存入 Redis 队列条目，出队后由任意 worker 据此重建执行参数
    task_context = {
        "executor_key": "agent_stream",
        "agent_id": agent_id,
        "message": formatted_message,
        "display_message": request.message,
        "disabled_tools": request.disabled_tools,
        "agent_options": request.agent_options,
        "attachments": attachments_data,
        "trace_id": trace_id,
        "user_message_written": True,
        "disabled_skills": request.disabled_skills,
        "enabled_skills": request.enabled_skills,
        "persona_system_prompt": request.persona_system_prompt,
        "disabled_mcp_tools": request.disabled_mcp_tools,
        "team_id": request.team_id,
        "active_goal": active_goal_data,
        "recommendation_input": request.message,
        "auto_mode": request.auto_mode,
    }

    # 检查并发限制
    limiter = get_concurrency_limiter()
    concurrency_result = await limiter.acquire(
        user_id=user.sub,
        roles=user.roles,
        run_id=run_id,
        session_id=session_id,
        task_context=task_context,
    )

    # 活跃数已满且排队队列也满：拒绝并返回 429（附带当前活跃数/上限/队列长度供前端提示）
    if concurrency_result.result == ConcurrencyResult.REJECTED_QUEUE:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "too_many_requests",
                "message": f"排队已满，当前活跃 {concurrency_result.active_count}/{concurrency_result.max_concurrent}，排队 {concurrency_result.queue_length}",
                "active": concurrency_result.active_count,
                "max_concurrent": concurrency_result.max_concurrent,
                "queue_length": concurrency_result.queue_length,
            },
        )

    # 命中并发上限但队列未满：任务进入排队。task_context 已在上面的 acquire() 里存入 Redis
    if concurrency_result.result == ConcurrencyResult.QUEUED:
        # Task context already stored in Redis queue entry by acquire().
        # Ensure executor is initialized and create session immediately.
        if task_manager._executor is None:
            from src.infra.task.executor import TaskExecutor

            task_manager._executor = TaskExecutor(
                task_manager.storage, task_manager._run_info, task_manager._heartbeat
            )
        # 立即创建 session 记录并置为 QUEUED（不等出队），这样前端刷新也能看到该会话
        # Create session record immediately (don't wait for dequeue)
        await task_manager._executor.ensure_session(
            session_id, agent_id, user.sub, project_id=request.project_id
        )
        await task_manager._executor._update_session_status(
            session_id, TaskStatus.QUEUED, run_id=run_id
        )

        # 立即把 user:message 事件落库，保证刷新页面时能加载到用户刚发出的消息
        # Write user:message event to MongoDB immediately so page refresh can load it
        presenter = Presenter(
            PresenterConfig(
                session_id=session_id,
                agent_id=agent_id,
                agent_name=resolve_agent_name(agent_id),
                user_id=user.sub,
                run_id=run_id,
                trace_id=trace_id,
                enable_storage=True,
            )
        )
        await presenter._ensure_trace()
        await presenter.emit_user_message(
            request.message,
            attachments=[a.model_dump() for a in request.attachments]
            if request.attachments
            else None,
            enabled_skills=request.enabled_skills,
        )

        # 标记 user:message 已写入，出队执行时执行器不再重复发送
        # Mark user message as already written so executor skips re-emitting
        task_manager._run_info[run_id] = {
            "session_id": session_id,
            "agent_id": agent_id,
            "user_id": user.sub,
            "trace_id": trace_id,
            "user_message_written": True,
        }

        # 更新 session metadata，存储完整的对话配置（排队状态）
        await _update_session_config(
            session_id,
            run_id,
            agent_id,
            request,
            preferred_language,
            trace_id=trace_id,
        )

        # 返回排队态：queue_position 为当前排队位置，前端可据此提示"排队中"
        return {
            "session_id": session_id,
            "run_id": run_id,
            "status": "queued",
            "queue_position": concurrency_result.queue_position,
            "max_concurrent": concurrency_result.max_concurrent,
        }

    # 未排队，直接提交后台任务。两种后端二选一：arq（分布式队列，多进程）或内置 submit（本进程）
    if settings.TASK_BACKEND == "arq":
        _, _ = await task_manager.submit_arq(
            session_id=session_id,
            agent_id=agent_id,
            message=formatted_message,
            user_id=user.sub,
            executor_key="agent_stream",
            disabled_tools=request.disabled_tools,
            agent_options=request.agent_options,
            attachments=attachments_data,
            run_id=run_id,
            project_id=request.project_id,
            disabled_skills=request.disabled_skills,
            enabled_skills=request.enabled_skills,
            persona_system_prompt=request.persona_system_prompt,
            disabled_mcp_tools=request.disabled_mcp_tools,
            display_message=request.message,
            recommendation_input=request.message,
            trace_id=trace_id,
            team_id=request.team_id,
            active_goal=active_goal_data,
            auto_mode=request.auto_mode,
            write_user_message_immediately=True,
        )
    else:
        # STARTED — 正常提交后台任务
        _, _ = await task_manager.submit(
            session_id=session_id,
            agent_id=agent_id,
            message=formatted_message,
            user_id=user.sub,
            executor=_execute_agent_stream,
            disabled_tools=request.disabled_tools,
            agent_options=request.agent_options,
            attachments=attachments_data,
            run_id=run_id,
            project_id=request.project_id,
            disabled_skills=request.disabled_skills,
            enabled_skills=request.enabled_skills,
            persona_system_prompt=request.persona_system_prompt,
            disabled_mcp_tools=request.disabled_mcp_tools,
            display_message=request.message,
            recommendation_input=request.message,
            team_id=request.team_id,
            trace_id=trace_id,
            active_goal=active_goal_data,
            auto_mode=request.auto_mode,
            write_user_message_immediately=True,
        )

    # 更新 session metadata，存储完整的对话配置
    await _update_session_config(
        session_id,
        run_id,
        agent_id,
        request,
        preferred_language,
        trace_id=trace_id,
    )

    # 返回受理态 pending：任务已在后台开始，前端接着用 SSE 接口按 run_id 拉取事件流
    return {
        "session_id": session_id,
        "run_id": run_id,
        "status": "pending",
    }


# GET /api/chat/sessions/{session_id}/stream —— 两段式的第二段：SSE 事件流。
# 权限：需登录并拥有该 session。查询参数 run_id 用于隔离并选定要读取的对话轮次。
# 从 Redis Stream 按 run_id 读取事件；断线重连会从流头回放，保证不丢已产生的事件。
# 收到 complete 或 error 事件后流自动结束。返回 text/event-stream。
@router.get("/sessions/{session_id}/stream")
async def session_stream(
    session_id: str,
    run_id: str = Query(..., description="Run ID for isolating conversation turns"),
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    SSE 流式读取特定 run 的事件

    从 Redis Stream 读取。
    run_id: 对话轮次 ID，用于隔离多轮对话。
    流会在收到 complete 或 error 事件后自动结束。
    """
    from src.infra.logging import get_logger
    from src.infra.session.dual_writer import get_dual_writer

    logger = get_logger(__name__)

    # 验证用户对该 session 的所有权
    session_manager = SessionManager()
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    verify_session_ownership(session, user)

    logger.info(f"[SSE] New connection: session={session_id}, run_id={run_id}")

    # 双写器：负责从 Redis Stream 读取事件（写入侧由后台任务的 Presenter 完成）
    dual_writer = get_dual_writer()

    # SSE 事件生成器：被 StreamingResponse 迭代，每 yield 一次就向客户端推送一帧
    async def event_generator():
        logger.info(f"[SSE] Generator started for session={session_id}, run_id={run_id}")
        try:
            # 使用 run_id 读取特定轮次的事件
            # read_from_redis 从该 run 的 Redis Stream 起点开始读，因此断线重连能回放历史事件
            event_count = 0
            async for event in dual_writer.read_from_redis(
                session_id,
                run_id=run_id,
            ):
                # 心跳事件：发送 SSE 注释（: 开头的行被 EventSource 忽略）
                # 这样能检测到客户端断开，同时不干扰前端逻辑
                if event["event_type"] == "heartbeat":
                    yield ": heartbeat\n\n"
                    continue

                event_count += 1
                # 普通事件：在线程池中格式化为 SSE 帧后再 yield（格式化可能较重，避免阻塞事件循环）
                yield await run_blocking_io(_format_sse_event, event)

            logger.info(f"[SSE] Stream ended after {event_count} events")

        # 生成过程中出错：向前端补发一条 error 事件，前端据此结束流并提示
        except Exception as e:
            logger.error(f"[SSE] Generator error: {e}")
            yield 'event: error\ndata: {"error": "An internal error occurred"}\n\n'

    # 以 SSE 规范返回：text/event-stream + 关闭缓存 + 保持长连接；X-Accel-Buffering 关掉 nginx 缓冲以实现实时推送
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


# GET /api/chat/sessions/{session_id}/status —— 查询任务状态（前端轮询用，作为 SSE 的补充）。
# 权限：需登录并拥有该 session。带 run_id 查指定轮次，否则查会话当前轮次。
# 返回该轮的 status（pending/running/completed/error 等）与错误信息。
@router.get("/sessions/{session_id}/status")
async def get_session_status(
    session_id: str,
    run_id: str = Query(None, description="Run ID (optional, defaults to current run)"),
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    获取任务状态

    Args:
        session_id: 会话 ID
        run_id: 运行 ID（可选，默认为当前 run）
    """
    # 验证用户对该 session 的所有权
    session_manager = SessionManager()
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    verify_session_ownership(session, user)

    task_manager = get_task_manager()

    # 指定了 run_id 则查该轮次状态，否则回退到会话级（当前轮次）状态
    if run_id:
        status = await task_manager.get_run_status(session_id, run_id)
        error = await task_manager.get_run_error(run_id)
    else:
        status = await task_manager.get_status(session_id)
        error = await task_manager.get_error(session_id)

    return {
        "session_id": session_id,
        "run_id": run_id,
        "status": status.value,
        "error": error,
    }


# POST /api/chat/sessions/{session_id}/cancel —— 取消正在运行或排队中的任务。
# 权限：需登录并拥有该 session。先尝试在本地取消正在跑的任务；若本地未命中，
# 再从 Redis 排队队列中移除该会话待执行的任务。返回取消结果。
@router.post("/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: str,
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    取消正在运行的任务（包括排队中的任务）

    Args:
        session_id: 会话 ID

    Returns:
        success: 是否成功设置取消信号
        cancelled_locally: 是否在本地实例取消
        run_id: 被取消的运行 ID
        message: 状态信息
    """
    # 验证用户对该 session 的所有权
    session_manager = SessionManager()
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    verify_session_ownership(session, user)

    task_manager = get_task_manager()
    result = await task_manager.cancel(session_id, user_id=user.sub)

    # 如果本地没有取消到，尝试从排队队列中移除
    if not result.get("cancelled_locally"):
        try:
            from src.infra.task.concurrency import get_concurrency_limiter

            limiter = get_concurrency_limiter()
            removed = await limiter.remove_from_queue(user.sub, session_id)
            if removed:
                result["message"] = f"已从排队中移除 ({removed} 个任务)"
        except Exception as e:
            logger.warning(f"Failed to remove from queue: {e}")

    return result


# POST /api/chat/sessions/{session_id}/resume —— 从最近一次 checkpoint 恢复被中断的任务。
# 权限：需登录并拥有该 session。用于任务因中断/超时停止后从断点继续执行。
@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: str,
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    Resume an interrupted task from the latest checkpoint for this session.
    """
    session_manager = SessionManager()
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    verify_session_ownership(session, user)

    task_manager = get_task_manager()
    return await task_manager.resume_session(session_id)

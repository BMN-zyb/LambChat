"""Human-in-the-loop approval flow for scheduled-task creation."""

from typing import Any

from src.infra.logging import get_logger
from src.infra.persona_preset.manager import PersonaPresetManager
from src.infra.team.manager import TeamManager

logger = get_logger(__name__)


async def create_approval(*args, **kwargs):
    # 延迟导入 src.api.routes.human，避免 infra 层反向依赖 api 层造成的模块级循环引用；
    # 仅在真正调用创建审批时才导入
    from src.api.routes.human import create_approval as _create_approval

    return await _create_approval(*args, **kwargs)


async def wait_for_response(*args, **kwargs):
    # 同上：延迟导入以避免循环依赖。此调用会阻塞等待人工在前端做出审批决定（或超时）
    from src.api.routes.human import wait_for_response as _wait_for_response

    return await _wait_for_response(*args, **kwargs)


def _format_approval_message(preview: dict[str, Any]) -> str:
    # 把任务预览信息渲染成一段 Markdown 表格文本，作为审批卡片展示给用户，
    # 让用户在批准前就能看清任务名称/agent/调度规则/是否立即执行/超时时间以及每次运行发送的提示词
    immediate = "✅ Yes" if preview["run_on_start"] else "❌ No"
    return (
        "Please confirm creation of this scheduled task.\n\n"
        "No task has been created yet. Approve to create it.\n\n"
        f"| | |\n|---|---|\n"
        f"| **Name** | {preview['name']} |\n"
        f"| **Agent** | `{preview['agent_id']}` |\n"
        f"| **Schedule** | {preview['schedule']} |\n"
        f"| **Run immediately** | {immediate} |\n"
        f"| **Timeout** | {preview['timeout_seconds']}s |\n"
        "\n"
        f"{preview['effect']}\n\n"
        "**Prompt sent on each run:**\n\n"
        f"```text\n{preview['message']}\n```"
    )


def _is_persona_admin(user) -> bool:
    # 是否具备人设预设管理员权限，决定搜索人设时能否看到非公开/他人创建的预设
    return bool(user and "persona_preset:admin" in set(user.permissions or []))


def _choose_named_match(items: list[Any], query: str) -> Any | None:
    """Prefer exact name match, otherwise use the search result ranking."""
    # 大小写/首尾空白不敏感地精确匹配名称；找不到精确匹配时，
    # 退而选用搜索结果里排名最靠前的一项（假定搜索服务已按相关度排序）
    clean_query = query.strip().casefold()
    if not clean_query or not items:
        return None
    for item in items:
        name = getattr(item, "name", "")
        if isinstance(name, str) and name.strip().casefold() == clean_query:
            return item
    return items[0]


async def _resolve_persona_preset_id_from_query(
    *,
    user_id: str,
    user,
    query: str | None,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    # 供 scheduled_task_create 在只拿到自然语言 role_query（而非明确 persona_preset_id）时，
    # 按名称/关键字搜索出最匹配的人设预设。返回值三元组：(命中的 preset_id, 供预览展示的匹配信息, 错误信息)
    if not query or not query.strip():
        return None, None, None
    try:
        presets = await PersonaPresetManager().list_presets(
            user_id=user_id,
            is_admin=_is_persona_admin(user),
            q=query.strip(),
            limit=10,
        )
    except Exception as e:
        return None, None, f"Failed to search persona presets: {e}"

    match = _choose_named_match(presets, query)
    if match is None:
        return None, None, f"No persona preset matched '{query}'."
    return (
        match.id,
        {
            "id": match.id,
            "name": match.name,
            "query": query,
        },
        None,
    )


async def _resolve_team_id_from_query(
    *,
    user_id: str,
    query: str | None,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    # 与 _resolve_persona_preset_id_from_query 同理，但用于按自然语言搜索团队
    if not query or not query.strip():
        return None, None, None
    try:
        response = await TeamManager().list_teams(
            owner_user_id=user_id,
            q=query.strip(),
            limit=10,
        )
    except Exception as e:
        return None, None, f"Failed to search teams: {e}"

    match = _choose_named_match(response.teams, query)
    if match is None:
        return None, None, f"No team matched '{query}'."
    return (
        match.id,
        {
            "id": match.id,
            "name": match.name,
            "query": query,
        },
        None,
    )


async def _send_scheduled_task_approval_event(
    *,
    approval_id: str,
    message: str,
    session_id: str | None,
    run_id: str | None,
    timeout: int,
) -> None:
    # 通过会话的双写事件流把"需要人工审批"这件事实时推送到前端 UI，
    # 前端据此渲染出确认/拒绝按钮；没有 session_id 就没有实时通道，只能记录警告并放弃推送
    if not session_id:
        logger.warning("[ScheduledTask] Cannot send approval event: no session_id")
        return

    try:
        from src.infra.session.dual_writer import get_dual_writer

        await get_dual_writer().write_event(
            session_id=session_id,
            event_type="approval_required",
            data={
                "id": approval_id,
                "message": message,
                "type": "confirm",
                "fields": [],
                "timeout": timeout,
            },
            run_id=run_id,
        )
    except Exception as e:
        logger.error("[ScheduledTask] Failed to send approval event: %s", e, exc_info=True)


async def _confirm_scheduled_task_creation(
    *,
    preview: dict[str, Any],
    user_id: str,
    timeout: int = 300,
) -> dict[str, Any]:
    """Create a human-in-the-loop confirmation and wait for the user's decision."""
    # 整个确认流程：1) 生成审批记录 2) 推送实时事件通知前端展示审批卡片
    # 3) 阻塞等待用户在超时时间内做出响应（批准/拒绝/超时三种终态）
    from src.infra.logging.context import TraceContext

    ctx = TraceContext.get_request_context()
    approval_message = _format_approval_message(preview)
    approval = await create_approval(
        message=approval_message,
        approval_type="confirm",
        fields=[],
        session_id=ctx.session_id or None,
        user_id=user_id,
        metadata={
            "approval_type": "scheduled_task_create",
            "preview": preview,
        },
    )
    await _send_scheduled_task_approval_event(
        approval_id=approval.id,
        message=approval_message,
        session_id=ctx.session_id or None,
        run_id=ctx.run_id or None,
        timeout=timeout,
    )

    # wait_for_response 会阻塞挂起当前工具调用协程，直到用户响应或超时才返回，
    # 这段时间内 agent 的这一次工具调用处于"等待人工"状态
    response = await wait_for_response(approval.id, timeout=timeout)
    if response is None:
        return {
            "approved": False,
            "status": "timeout",
            "approval_id": approval.id,
            "message": f"Scheduled task creation timed out waiting for user confirmation ({timeout}s).",
        }
    if not response.approved:
        return {
            "approved": False,
            "status": "rejected",
            "approval_id": approval.id,
            "message": "User rejected scheduled task creation.",
        }
    return {
        "approved": True,
        "status": "approved",
        "approval_id": approval.id,
    }

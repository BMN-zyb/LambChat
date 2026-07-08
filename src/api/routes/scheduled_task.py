"""Scheduled task API routes — CRUD, pause/resume, manual trigger, run history."""

# 定时任务路由模块（挂载于 /api/scheduled-tasks）
# 职责：定时任务的增删改查、暂停/恢复、手动立即执行、查看运行历史及其创建的会话
# 具体调度语义（cron / 固定间隔触发等）由 ScheduledTaskService 实现，本模块只负责 HTTP 接口与归属校验
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import require_permissions
from src.infra.scheduler.service import ScheduledTaskService
from src.kernel.schemas.scheduled_task import (
    ScheduledTask,
    ScheduledTaskCreate,
    ScheduledTaskListResponse,
    ScheduledTaskResponse,
    ScheduledTaskStatus,
    ScheduledTaskUpdate,
    TaskRunListResponse,
    TaskSessionListResponse,
    TaskSessionResponse,
)
from src.kernel.schemas.user import TokenPayload
from src.kernel.types import Permission

router = APIRouter()


# 依赖注入工厂：每次请求创建一个定时任务服务实例（ScheduledTaskService），供各接口通过 Depends 复用
def _get_service() -> ScheduledTaskService:
    return ScheduledTaskService()


# 公共鉴权辅助：按 task_id 加载任务并校验归属，供多个接口复用
async def _require_owned_task(
    task_id: str,
    user: TokenPayload,
    service: ScheduledTaskService,
) -> ScheduledTask:
    """Load task and verify ownership. Raises 404 if not found or forbidden."""
    task = await service.get_task(task_id)
    # 任务不存在、或拥有者不是当前用户，都统一返回 404（不暴露任务是否存在，避免被枚举探测）
    if task is None or task.owner_id != user.sub:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# ── CRUD ────────────────────────────────────────────


# POST /api/scheduled-tasks/ —— 新建定时任务，需要 SCHEDULED_TASK_WRITE 权限
# 请求体 ScheduledTaskCreate：包含调度规则（cron / 间隔）、要执行的 agent 及执行参数等
# owner_id 绑定当前登录用户；调度配置非法时抛 400；成功返回 201 + 任务详情
@router.post("/", response_model=ScheduledTaskResponse, status_code=201)
async def create_scheduled_task(
    body: ScheduledTaskCreate,
    user: TokenPayload = Depends(require_permissions(Permission.SCHEDULED_TASK_WRITE.value)),
    service: ScheduledTaskService = Depends(_get_service),
):
    """Create a new scheduled task."""
    try:
        task = await service.create_task(body, owner_id=user.sub)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await service.get_task_response(task)


# GET /api/scheduled-tasks/ —— 分页列出当前用户拥有的定时任务，需要 SCHEDULED_TASK_READ 权限
# 查询参数：status 按状态过滤、source_session_id 按来源会话过滤、created_by 按创建来源过滤（user/agent/api）
# skip/limit 控制分页；返回 items 列表与 total 总数
@router.get("/", response_model=ScheduledTaskListResponse)
async def list_scheduled_tasks(
    status: ScheduledTaskStatus | None = None,
    source_session_id: str | None = None,
    created_by: str | None = Query(None, pattern="^(user|agent|api)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    user: TokenPayload = Depends(require_permissions(Permission.SCHEDULED_TASK_READ.value)),
    service: ScheduledTaskService = Depends(_get_service),
):
    """List scheduled tasks owned by the current user, with pagination."""
    items, total = await service.list_tasks_paginated(
        owner_id=user.sub,
        status=status,
        source_session_id=source_session_id,
        created_by=created_by,
        skip=skip,
        limit=limit,
    )
    return ScheduledTaskListResponse(items=items, total=total)


# GET /api/scheduled-tasks/{task_id} —— 获取单个定时任务详情，需要 SCHEDULED_TASK_READ 权限
# 先经 _require_owned_task 校验归属，非本人任务返回 404
@router.get("/{task_id}", response_model=ScheduledTaskResponse)
async def get_scheduled_task(
    task_id: str,
    user: TokenPayload = Depends(require_permissions(Permission.SCHEDULED_TASK_READ.value)),
    service: ScheduledTaskService = Depends(_get_service),
):
    """Get details of a single scheduled task."""
    task = await _require_owned_task(task_id, user, service)
    return await service.get_task_response(task)


# PUT /api/scheduled-tasks/{task_id} —— 更新定时任务配置，需要 SCHEDULED_TASK_WRITE 权限
# 请求体 ScheduledTaskUpdate（部分字段）；先校验归属，调度配置非法抛 400，任务不存在抛 404
@router.put("/{task_id}", response_model=ScheduledTaskResponse)
async def update_scheduled_task(
    task_id: str,
    body: ScheduledTaskUpdate,
    user: TokenPayload = Depends(require_permissions(Permission.SCHEDULED_TASK_WRITE.value)),
    service: ScheduledTaskService = Depends(_get_service),
):
    """Update a scheduled task's configuration."""
    await _require_owned_task(task_id, user, service)
    try:
        updated = await service.update_task(task_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return await service.get_task_response(updated)


# ── Pause / Resume / Delete ─────────────────────────


# POST /api/scheduled-tasks/{task_id}/pause —— 暂停任务，需要 SCHEDULED_TASK_WRITE 权限
# 从调度器中移除触发器但保留任务配置，状态置为暂停；可通过 resume 恢复
@router.post("/{task_id}/pause", response_model=ScheduledTaskResponse)
async def pause_scheduled_task(
    task_id: str,
    user: TokenPayload = Depends(require_permissions(Permission.SCHEDULED_TASK_WRITE.value)),
    service: ScheduledTaskService = Depends(_get_service),
):
    """Pause a scheduled task (removes from scheduler, keeps config)."""
    await _require_owned_task(task_id, user, service)
    updated = await service.pause_task(task_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return await service.get_task_response(updated)


# POST /api/scheduled-tasks/{task_id}/resume —— 恢复被暂停的任务，需要 SCHEDULED_TASK_WRITE 权限
# 按原调度规则重新注册到调度器
@router.post("/{task_id}/resume", response_model=ScheduledTaskResponse)
async def resume_scheduled_task(
    task_id: str,
    user: TokenPayload = Depends(require_permissions(Permission.SCHEDULED_TASK_WRITE.value)),
    service: ScheduledTaskService = Depends(_get_service),
):
    """Resume a paused scheduled task."""
    await _require_owned_task(task_id, user, service)
    updated = await service.resume_task(task_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return await service.get_task_response(updated)


# DELETE /api/scheduled-tasks/{task_id} —— 物理删除任务，需要 SCHEDULED_TASK_DELETE 权限（比读写更高）
# 成功返回 204 无内容；先校验归属
@router.delete("/{task_id}", status_code=204)
async def delete_scheduled_task(
    task_id: str,
    user: TokenPayload = Depends(require_permissions(Permission.SCHEDULED_TASK_DELETE.value)),
    service: ScheduledTaskService = Depends(_get_service),
):
    """Physically delete a scheduled task."""
    await _require_owned_task(task_id, user, service)
    await service.delete_task(task_id)


# ── Manual trigger ──────────────────────────────────


# POST /api/scheduled-tasks/{task_id}/run —— 不等下次调度、立即手动触发一次执行，需要 SCHEDULED_TASK_WRITE 权限
# 异步提交后立即返回 {"run_id": ..., "status": "submitted"}，需轮询 /{task_id}/runs 查看进度
@router.post("/{task_id}/run", response_model=dict)
async def run_scheduled_task_now(
    task_id: str,
    user: TokenPayload = Depends(require_permissions(Permission.SCHEDULED_TASK_WRITE.value)),
    service: ScheduledTaskService = Depends(_get_service),
):
    """Manually trigger a scheduled task execution.

    Returns immediately with ``{"run_id": ..., "status": "submitted"}``.
    Poll ``GET /{task_id}/runs`` to monitor progress.
    """
    await _require_owned_task(task_id, user, service)
    return await service.run_task_now(task_id)


# ── Run history ─────────────────────────────────────


# GET /api/scheduled-tasks/{task_id}/runs —— 查看任务的历次执行记录，需要 SCHEDULED_TASK_READ 权限
# limit/offset 分页；返回每次运行的状态、时间等
@router.get("/{task_id}/runs", response_model=TaskRunListResponse)
async def list_task_runs(
    task_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: TokenPayload = Depends(require_permissions(Permission.SCHEDULED_TASK_READ.value)),
    service: ScheduledTaskService = Depends(_get_service),
):
    """View execution history for a scheduled task."""
    await _require_owned_task(task_id, user, service)
    runs, total = await service.get_task_runs(task_id, limit, offset)
    return TaskRunListResponse(items=runs, total=total)


# ── Task sessions ────────────────────────────────────


# GET /api/scheduled-tasks/{task_id}/sessions —— 列出该任务历次执行所创建的会话，需要 SCHEDULED_TASK_READ 权限
# 每次任务执行通常会生成一个会话（对话），此处从会话存储层按任务 id 反查
@router.get("/{task_id}/sessions", response_model=TaskSessionListResponse)
async def list_task_sessions(
    task_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    user: TokenPayload = Depends(require_permissions(Permission.SCHEDULED_TASK_READ.value)),
    service: ScheduledTaskService = Depends(_get_service),
):
    """List sessions (conversations) created by a scheduled task's executions."""
    await _require_owned_task(task_id, user, service)
    # 会话数据不归定时任务服务管理，这里按需从会话存储层查询由该任务触发生成的会话
    from src.infra.session.storage import SessionStorage

    storage = SessionStorage()
    sessions, total = await storage.list_sessions_for_task(
        scheduled_task_id=task_id,
        user_id=user.sub,
        skip=skip,
        limit=limit,
    )
    # 将底层会话对象逐个映射为对外的 TaskSessionResponse（含未读数等展示字段）
    items = [
        TaskSessionResponse(
            id=s.id,
            name=s.name,
            agent_id=s.agent_id,
            created_at=s.created_at,
            updated_at=s.updated_at,
            is_active=s.is_active,
            metadata=s.metadata,
            unread_count=s.unread_count,
        )
        for s in sessions
    ]
    return TaskSessionListResponse(items=items, total=total)

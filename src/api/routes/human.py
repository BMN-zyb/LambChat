"""
Human Input 路由

用于 Agent 请求人工审批/输入的 API。

支持分布式部署：
- 审批数据存储在 MongoDB
- 使用 Redis Pub/Sub 实现跨进程响应唤醒
- 自动降级为 MongoDB 轮询（Redis 不可用时）
"""

import asyncio
import json
import time
import uuid
from collections import OrderedDict
from typing import Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import require_permissions
from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.storage.mongodb import (
    ApprovalResponse,
    PendingApproval,
    get_approval_storage,
    notify_approval_response,
    wait_for_response_distributed,
)
from src.infra.utils.datetime import utc_now
from src.kernel.schemas.user import TokenPayload

logger = get_logger(__name__)

# Human-in-the-loop 路由：挂载在 /human
# Agent 执行到需要人工确认/输入处会创建"待审批"(PendingApproval) 并阻塞等待；
# 前端轮询 /pending 拉取、调用 /{id}/respond 提交结果，从而唤醒并驱动 Agent 继续执行
router = APIRouter()

# ============================================================================
# 回调机制 - 用于通知前端有新的审批请求
# ============================================================================

# 当创建新审批时的回调函数列表
_approval_created_callbacks: List[Callable[[str], None]] = []


def register_approval_callback(callback: Callable[[str], None]) -> None:
    """注册审批创建回调"""
    _approval_created_callbacks.append(callback)


def unregister_approval_callback(callback: Callable[[str], None]) -> None:
    """注销审批创建回调"""
    if callback in _approval_created_callbacks:
        _approval_created_callbacks.remove(callback)


async def _notify_approval_created(session_id: str) -> None:
    """通知所有回调有新的审批创建"""
    # 逐个执行回调，兼容同步与异步回调；单个回调异常不影响其它回调
    for callback in _approval_created_callbacks:
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(session_id)
            else:
                callback(session_id)
        except Exception as e:
            logger.warning(f"Approval callback error: {e}")


# ============================================================================
# 内存状态 (单进程优化)
# ============================================================================

# 单进程内使用 asyncio.Event 加速（可选优化）
# 分布式环境下会同时使用 Redis Pub/Sub + MongoDB 轮询作为备用
# 存储 (event, created_at) 以支持 TTL 清理
HUMAN_LOCAL_EVENT_CACHE_MAX_ENTRIES = 512
_local_events: OrderedDict[str, tuple[asyncio.Event, float]] = OrderedDict()

# MongoDB 存储实例
_approval_storage = get_approval_storage()


# ============================================================================
# 核心函数
# ============================================================================


# 取出并"续期"本地事件（LRU：命中后移到末尾表示最近使用）；不存在返回 None
def _touch_local_event(approval_id: str) -> tuple[asyncio.Event, float] | None:
    entry = _local_events.get(approval_id)
    if entry is None:
        return None
    _local_events.move_to_end(approval_id)
    return entry


# 存入本地事件并做 LRU 淘汰：超出容量上限时丢弃最旧条目，防止遗弃审批泄漏内存
def _store_local_event(approval_id: str, entry: tuple[asyncio.Event, float]) -> None:
    _local_events[approval_id] = entry
    _local_events.move_to_end(approval_id)
    while len(_local_events) > HUMAN_LOCAL_EVENT_CACHE_MAX_ENTRIES:
        _local_events.popitem(last=False)


async def create_approval(
    message: str,
    approval_type: str = "form",
    fields: Optional[List[dict]] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> PendingApproval:
    """
    创建审批请求 (供 Agent 调用)

    Args:
        message: 提示消息
        approval_type: 类型 (form, confirm)
        fields: 表单字段列表
        session_id: 关联的会话 ID
        user_id: 关联的用户 ID

    Returns:
        PendingApproval 对象
    """
    approval_id = str(uuid.uuid4())
    approval = PendingApproval(
        id=approval_id,
        message=message,
        type=approval_type,
        fields=fields or [],
        status="pending",
        session_id=session_id,
        user_id=user_id,
        created_at=utc_now(),
        metadata=metadata,
    )

    # 存储到 MongoDB
    # 持久化审批记录（跨进程/重启可见，是审批状态的权威存储）
    await _approval_storage.create(approval)
    logger.info(
        "[HITL] approval_id=%s Approval created (type=%s)",
        approval_id,
        approval_type,
    )

    # 创建本地 Event（单进程优化）
    # 在本进程内建立 asyncio.Event，供同进程内的等待者被快速唤醒
    _store_local_event(approval_id, (asyncio.Event(), time.time()))

    # 通知前端有新的审批请求
    # 触发已注册回调（如 WebSocket 推送），让前端及时感知有新的待审批
    await _notify_approval_created(session_id or "")

    return approval


async def wait_for_response(approval_id: str, timeout: float = 300) -> Optional[ApprovalResponse]:
    """
    等待审批响应 (供 Agent 调用)

    使用本地 asyncio.Event + MongoDB 轮询：
    1. 优先使用本地 Event（单进程内快速响应）
    2. 使用 MongoDB 轮询作为后备

    Args:
        approval_id: 审批 ID
        timeout: 超时时间 (秒)

    Returns:
        ApprovalResponse 或 None (超时)
    """
    logger.info(
        "[HITL] approval_id=%s Waiting for response (timeout=%ss)",
        approval_id,
        timeout,
    )
    # 查找本进程是否持有该审批的本地事件：决定走"本地 Event + 轮询"还是"纯分布式轮询"
    local_event = _touch_local_event(approval_id)
    event = local_event[0] if local_event else None

    if event:
        # 单进程内：同时等待本地 Event 和 MongoDB 轮询
        try:
            # 先检查是否已有响应
            # 若响应已存在则直接返回，避免进入不必要的等待
            response = await _approval_storage.get_response(approval_id)
            if response:
                logger.info(
                    "[HITL] approval_id=%s Response received (already present)",
                    approval_id,
                )
                return response

            # 创建两个任务：本地 Event 和 MongoDB 轮询
            # 同时等待两路信号：本地 Event（同进程秒级唤醒）与分布式轮询（跨进程兜底），先到先返回
            local_wait = asyncio.wait_for(event.wait(), timeout=timeout)
            mongo_wait = wait_for_response_distributed(approval_id, timeout)

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(local_wait),
                    asyncio.create_task(mongo_wait),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # 取消未完成的任务
            # 任一路已完成，取消另一路，避免留下悬挂的等待任务
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            # 获取结果（local_event.wait() 返回 True，需要从 MongoDB 获取实际响应）
            for task in done:
                try:
                    # local_event.wait() 返回 True，表示事件被触发，但实际响应在 MongoDB 中
                    task.result()
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    logger.warning(f"Wait task error: {e}")

            # 从 MongoDB 获取最终结果
            # 本地 Event 只表示"已被响应"，真正的响应内容统一以 MongoDB 为准
            final_response = await _approval_storage.get_response(approval_id)
            if final_response:
                logger.info("[HITL] approval_id=%s Response received", approval_id)
            else:
                logger.info("[HITL] approval_id=%s Wait timed out", approval_id)
            return final_response

        finally:
            # 无论成功、超时还是异常，都清理本地 Event，防止内存泄漏
            _local_events.pop(approval_id, None)
    else:
        # 跨进程：直接使用 MongoDB 轮询
        # 本进程没有本地 Event（审批可能在其它进程创建），只能靠 Redis Pub/Sub + MongoDB 轮询等待
        distributed_response = await wait_for_response_distributed(approval_id, timeout)
        if distributed_response:
            logger.info("[HITL] approval_id=%s Response received", approval_id)
        else:
            logger.info("[HITL] approval_id=%s Wait timed out", approval_id)
        return distributed_response


def _cleanup_approval(approval_id: str) -> None:
    """清理审批相关数据"""
    _local_events.pop(approval_id, None)


def _cleanup_stale_events(max_age: float = 3600) -> int:
    """清理超时的本地 Event（防止遗弃的审批泄漏内存）"""
    now = time.time()
    stale = [aid for aid, (_, created) in _local_events.items() if now - created > max_age]
    for aid in stale:
        _local_events.pop(aid, None)
    return len(stale)


# ============================================================================
# API 路由
# ============================================================================


# GET /human/pending —— 前端轮询获取当前用户待处理的审批列表
# 需要 chat:write 权限；仅返回当前用户 (user.sub) 的审批；顺带清理过期的本地事件
@router.get("/pending")
async def get_pending_approvals(
    limit: int = Query(100, ge=1, le=100, description="最大返回审批数量"),
    user: TokenPayload = Depends(require_permissions("chat:write")),
):
    """
    获取待处理的审批列表

    前端轮询此接口获取待审批的请求。只返回当前用户的审批。
    """
    _cleanup_stale_events()
    pending = await _approval_storage.list_pending(user_id=user.sub, limit=limit)
    return {"approvals": [a.model_dump() for a in pending], "count": len(pending)}


# POST /human/{approval_id}/respond —— 前端提交审批结果（HITL 恢复的关键入口）
# 需要 chat:write 权限；查询参数 approved(bool)、response(JSON 字符串)
# 更新审批状态后，通过 Redis Pub/Sub + 本地 Event 唤醒正在阻塞等待的 Agent，使其继续执行
@router.post("/{approval_id}/respond", dependencies=[Depends(require_permissions("chat:write"))])
async def respond_to_approval(
    approval_id: str,
    approved: bool = Query(..., description="是否批准"),
    response: str = Query("{}", description="响应数据（JSON 字符串）"),
):
    """
    响应审批请求

    前端调用此接口提交审批结果。
    """
    logger.info(
        "[HITL] approval_id=%s Responding approved=%s",
        approval_id,
        approved,
    )
    # 校验审批存在且仍处于 pending（防止对已处理/不存在的审批重复响应）
    approval = await _approval_storage.get(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="审批请求不存在")

    if approval.status != "pending":
        logger.info(
            "[HITL] approval_id=%s Approval not pending (status=%s)",
            approval_id,
            approval.status,
        )
        raise HTTPException(status_code=400, detail="审批请求已处理")

    # 解析 JSON 响应数据
    # 前端提交的表单/确认结果为 JSON 字符串；解析失败时降级为空字典
    try:
        response_data = await run_blocking_io(json.loads, response) if response else {}
    except json.JSONDecodeError:
        response_data = {}

    # 记录响应并更新状态
    approval_response = ApprovalResponse(approved=approved, response=response_data)
    status = "approved" if approved else "rejected"
    # 优先使用原子的"仅当仍为 pending 才写入"接口，避免并发下的重复响应竞态
    respond_if_pending = getattr(_approval_storage, "respond_if_pending", None)
    if callable(respond_if_pending):
        updated_approval = await respond_if_pending(approval_id, status, approval_response)
        if updated_approval is None:
            raise HTTPException(status_code=400, detail="审批请求已处理")
    else:
        await _approval_storage.update_status(approval_id, status, approval_response)

    logger.info(
        "[HITL] approval_id=%s Approval response recorded, notifying waiters",
        approval_id,
    )

    # 通知等待的 Agent（分布式支持）
    # 1. 通过 Redis Pub/Sub 通知跨进程的 Agent
    await notify_approval_response(approval_id, approval_response)

    # 2. 触发本地 Event（单进程内快速响应）
    entry = _touch_local_event(approval_id)
    if entry:
        entry[0].set()

    return {"status": "success", "approval_id": approval_id, "approved": approved}


# POST /human/{approval_id}/extend —— 延长审批超时时间（用户正在交互时调用，避免等待超时）
# 需要 chat:write 权限；查询参数 extra_seconds；达到最大延长次数时返回 max_extensions_reached
@router.post("/{approval_id}/extend", dependencies=[Depends(require_permissions("chat:write"))])
async def extend_approval_timeout(
    approval_id: str,
    extra_seconds: int = Query(60, ge=10, le=300, description="延长的秒数"),
):
    """
    延长审批超时时间（用户交互时触发，支持分布式）
    """
    new_expires = await _approval_storage.extend_expires_at(
        approval_id,
        extra_seconds=extra_seconds,
    )
    if new_expires is None:
        return {"status": "max_extensions_reached", "expires_at": None}

    return {
        "status": "success",
        "expires_at": new_expires.isoformat(),
    }


# GET /human/{approval_id} —— 获取单个审批详情
# 需要 chat:write 权限；不存在时返回 200 且 status=not_found（便于前端处理，无需捕获 404）
@router.get("/{approval_id}", dependencies=[Depends(require_permissions("chat:write"))])
async def get_approval(approval_id: str):
    """获取单个审批详情"""
    approval = await _approval_storage.get(approval_id)
    if not approval:
        # 返回 200 状态码，但用 status 字段表示不存在
        # 这样前端处理更简洁，不需要 catch 404 错误
        return {"id": approval_id, "status": "not_found"}

    return approval.model_dump()


# DELETE /human/{approval_id} —— 取消并删除一个审批请求
# 需要 chat:write 权限；同时删除 MongoDB 记录与内存中的本地 Event
@router.delete("/{approval_id}", dependencies=[Depends(require_permissions("chat:write"))])
async def cancel_approval(approval_id: str):
    """取消审批请求"""
    approval = await _approval_storage.get(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="审批请求不存在")

    # 删除 MongoDB 记录
    await _approval_storage.delete(approval_id)
    # 清理内存中的 Event
    _cleanup_approval(approval_id)
    return {"status": "cancelled"}

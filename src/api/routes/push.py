"""Push 订阅路由"""

# Web Push 订阅路由模块（挂载于 /api/push）
# 职责：下发 VAPID 公钥、保存/删除浏览器推送订阅，配合服务端向浏览器推送通知
# VAPID 是 Web Push 的身份标识机制：前端用公钥订阅，服务端用私钥签名推送
# 除获取公钥外均需登录；订阅端点必须为 https，且需带 p256dh/auth 两个密钥
from fastapi import APIRouter, Depends, HTTPException, status

from src.api.deps import get_current_user_required
from src.infra.logging import get_logger
from src.infra.push.manager import get_push_manager
from src.kernel.config import settings
from src.kernel.schemas.push_subscription import (
    PushSubscription,
    PushSubscriptionCreate,
    UnsubscribeRequest,
    VapidPublicKeyResponse,
)
from src.kernel.schemas.user import TokenPayload

logger = get_logger(__name__)

router = APIRouter()


# GET /api/push/vapid-public-key —— 返回 VAPID 公钥供前端发起推送订阅（无需鉴权）
# 若服务端未成功生成 VAPID 密钥，说明推送不可用，返回 503
@router.get("/vapid-public-key", response_model=VapidPublicKeyResponse)
async def get_vapid_public_key() -> VapidPublicKeyResponse:
    """Get VAPID public key for push subscription (no auth required)."""
    if not settings.VAPID_PUBLIC_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Push notifications are not available (VAPID key generation failed)",
        )
    return VapidPublicKeyResponse(public_key=settings.VAPID_PUBLIC_KEY)


# POST /api/push/subscribe —— 保存浏览器上报的推送订阅，需登录
# 请求体 PushSubscriptionCreate（endpoint + keys.p256dh/keys.auth + user_agent）
# 校验：endpoint 必须以 https:// 开头，且 p256dh/auth 必填，否则 400；订阅绑定到当前用户
@router.post("/subscribe", response_model=PushSubscription)
async def subscribe_push(
    data: PushSubscriptionCreate,
    user: TokenPayload = Depends(get_current_user_required),
    manager=Depends(get_push_manager),
) -> PushSubscription:
    """Save a push subscription from the browser."""
    if not data.endpoint.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Push subscription endpoint must use HTTPS",
        )
    if not data.keys.p256dh or not data.keys.auth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Push subscription keys are required",
        )
    subscription = await manager.save_subscription(
        user_id=user.sub,
        data=data.model_dump(),
        user_agent=data.user_agent,
    )
    logger.info("Push subscription saved: user_id=%s, endpoint=%s", user.sub, data.endpoint[:80])
    return PushSubscription.model_validate(subscription)


# POST /api/push/unsubscribe —— 按 endpoint 移除一个推送订阅，需登录
# 返回 {"status": "unsubscribed", "deleted": <是否删除到记录>}
@router.post("/unsubscribe")
async def unsubscribe_push(
    data: UnsubscribeRequest,
    user: TokenPayload = Depends(get_current_user_required),
    manager=Depends(get_push_manager),
) -> dict:
    """Remove a push subscription."""
    deleted = await manager.remove_subscription(data.endpoint)
    return {"status": "unsubscribed", "deleted": deleted}


# DELETE /api/push/subscriptions —— 删除当前用户的全部推送订阅（如登出所有设备），需登录
# 返回 {"status": "deleted", "count": <删除数量>}
@router.delete("/subscriptions")
async def delete_all_subscriptions(
    user: TokenPayload = Depends(get_current_user_required),
    manager=Depends(get_push_manager),
) -> dict:
    """Remove all push subscriptions for the current user."""
    deleted = await manager.remove_all_for_user(user.sub)
    return {"status": "deleted", "count": deleted}

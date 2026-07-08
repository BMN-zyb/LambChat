"""Push 通知管理器"""
# 中文说明：PushManager 封装了浏览器 Web Push 订阅的增删查，以及基于 VAPID
# 密钥对通过 pywebpush 库实际发送推送通知的逻辑。核心难点：
#   1）pywebpush 是同步库，发送单条推送是阻塞 IO，必须用 asyncio.to_thread
#      丢到线程池执行，避免阻塞事件循环；
#   2）订阅可能已经失效（用户卸载浏览器/取消授权等），此时推送服务会返回
#      404/410，需要据此自动清理数据库中的失效订阅记录，否则会一直重复推送失败。

from __future__ import annotations

from functools import lru_cache

from src.infra.logging import get_logger
from src.infra.push.storage import PushSubscriptionStorage
from src.kernel.config import settings

logger = get_logger(__name__)


class PushManager:
    def __init__(self):
        self.storage = PushSubscriptionStorage()

    async def save_subscription(self, user_id: str, data: dict, user_agent: str = "") -> dict:
        # 保存浏览器上报的订阅信息（endpoint + p256dh/auth 密钥），返回可序列化的 dict
        subscription = await self.storage.create(user_id, data, user_agent)
        return subscription.model_dump()

    async def remove_subscription(self, endpoint: str) -> bool:
        # 用户主动取消某个设备/浏览器的订阅
        return await self.storage.delete_by_endpoint(endpoint)

    async def remove_all_for_user(self, user_id: str) -> int:
        # 清空某个用户名下所有订阅（如账号注销、登出所有设备等场景）
        return await self.storage.delete_by_user(user_id)

    async def send_push_to_user(self, user_id: str, payload: dict) -> int:
        """Send Web Push notification to all of user's subscriptions. Returns delivered count."""
        # 未配置 VAPID 密钥对说明 Web Push 功能未启用，直接跳过（不算错误）
        if not settings.VAPID_PUBLIC_KEY or not settings.VAPID_PRIVATE_KEY:
            return 0

        subscriptions = await self.storage.get_by_user(user_id)
        if not subscriptions:
            return 0

        import asyncio
        import json

        from pywebpush import webpush

        delivered = 0
        # 中文：一个用户可能在多个设备/浏览器上订阅过推送，逐条发送，
        # 单条失败不影响其它订阅的发送（不因一个失效订阅而中断整批推送）
        for sub in subscriptions:
            try:
                subscription_info = {
                    "endpoint": sub.endpoint,
                    "keys": {
                        "p256dh": sub.keys.p256dh,
                        "auth": sub.keys.auth,
                    },
                }
                data = json.dumps(payload)
                # pywebpush is synchronous; run in thread to avoid blocking event loop
                await asyncio.to_thread(
                    webpush,
                    subscription_info=subscription_info,
                    data=data,
                    vapid_private_key=settings.VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": settings.VAPID_SUBJECT},
                )
                # 发送成功则更新该订阅的最近使用时间，用于后续清理长期不活跃订阅
                await self.storage.touch_last_used(sub.endpoint)
                delivered += 1
            except Exception as e:
                # 中文：webpush 抛出的异常可能是普通异常，也可能带有 HTTP response，
                # 这里尽量从异常对象或其 response 属性中取出状态码
                response = getattr(e, "response", None)
                status_code = getattr(e, "status_code", None) or getattr(
                    response, "status_code", None
                )
                if status_code in (404, 410):
                    # Subscription expired or revoked
                    # 中文：404/410 表示推送服务端已确认该订阅不再有效
                    # （用户注销授权、卸载浏览器等），主动清理，避免以后继续无效重试
                    logger.info(
                        "Push subscription gone (HTTP %s), removing: endpoint=%s",
                        status_code,
                        sub.endpoint[:80],
                    )
                    await self.storage.delete_by_endpoint(sub.endpoint)
                else:
                    # 其它错误（网络问题、服务端临时故障等）只记录警告，保留订阅下次再试
                    logger.warning(
                        "Failed to send push notification: endpoint=%s, error=%s",
                        sub.endpoint[:80],
                        e,
                    )
        return delivered

    async def close(self) -> None:
        await self.storage.close()


# 中文：用 lru_cache 实现单例——同一进程内多次调用 get_push_manager()
# 复用同一个 PushManager（及其内部的存储连接），避免重复创建
@lru_cache
def get_push_manager() -> PushManager:
    return PushManager()


async def close_push_manager() -> None:
    # 中文：只有当单例已经被创建过（缓存非空）才需要关闭并清空缓存，
    # 避免仅仅为了"关闭"这个动作而意外触发一次不必要的 PushManager 创建
    if get_push_manager.cache_info().currsize == 0:
        return
    try:
        await get_push_manager().close()
    finally:
        get_push_manager.cache_clear()

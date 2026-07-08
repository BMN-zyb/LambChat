"""Push 订阅 Schema"""

# 模块说明：定义浏览器 Web Push（网页推送通知）订阅相关的数据模型。
# 基于标准 Web Push 协议 + VAPID：浏览器通过 Push API 生成一个订阅端点（endpoint）
# 和一对加密密钥（keys），上报给后端保存；后端之后据此向浏览器的推送服务发送加密推送消息。
# 主要使用方：src/infra/push/storage.py（订阅记录的存取）、
# src/api/routes/push.py（订阅/取消订阅、获取 VAPID 公钥等接口）。
from __future__ import annotations

# 启用 PEP 563 延迟注解求值（本文件暂未用到前向引用，为项目内统一写法）
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


# Web Push 标准要求客户端提供的加密密钥对，服务端用它们加密推送消息内容。
class PushSubscriptionKeys(BaseModel):
    # 客户端 ECDH 公钥（P-256 曲线），用于消息加密
    p256dh: str
    # 客户端认证密钥（auth secret），用于消息加密的身份验证
    auth: str


# 注册/创建推送订阅的请求体：前端调用浏览器 Push API 拿到订阅信息后上报给后端。
class PushSubscriptionCreate(BaseModel):
    # 浏览器推送服务分配的回调地址，用作订阅的唯一标识
    endpoint: str
    # 加密密钥对
    keys: PushSubscriptionKeys
    # 客户端 User-Agent，便于区分/管理同一用户的多台设备订阅
    user_agent: str = ""


# 推送订阅记录的完整模型（数据库实体视图）。
class PushSubscription(BaseModel):
    # 订阅记录 ID
    id: str
    # 订阅所属用户 ID
    user_id: str
    endpoint: str
    keys: PushSubscriptionKeys
    user_agent: str = ""
    # 创建时间
    created_at: datetime
    # 最近一次成功推送使用的时间，可用于识别/清理长期失效的订阅
    last_used_at: Optional[datetime] = None

    # 允许从属性对象（如 Mongo 文档转换后的对象）直接构造本模型
    model_config = ConfigDict(from_attributes=True)


# 返回服务端 VAPID 公钥的响应：前端调用浏览器 Push 订阅 API 时
# 需要把该公钥作为 applicationServerKey 传入。
class VapidPublicKeyResponse(BaseModel):
    public_key: str


# 取消订阅的请求体，仅需 endpoint 即可定位并删除对应订阅记录。
class UnsubscribeRequest(BaseModel):
    endpoint: str

"""Push 订阅 Schema"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionCreate(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys
    user_agent: str = ""


class PushSubscription(BaseModel):
    id: str
    user_id: str
    endpoint: str
    keys: PushSubscriptionKeys
    user_agent: str = ""
    created_at: datetime
    last_used_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class VapidPublicKeyResponse(BaseModel):
    public_key: str


class UnsubscribeRequest(BaseModel):
    endpoint: str

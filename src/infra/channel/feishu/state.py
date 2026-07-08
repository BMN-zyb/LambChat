"""
Feishu connection state management.
"""

from enum import Enum


class ConnectionState(Enum):
    """WebSocket connection state."""

    # 飞书 WebSocket 长连接的五种生命周期状态：
    # 未连接 -> 连接中 -> 已连接；断线后进入重连中；多次失败后置为失败。
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"

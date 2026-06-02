from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from src.api.routes import websocket as websocket_route


class _FakeWebSocket:
    def __init__(self) -> None:
        self.client = SimpleNamespace(host="127.0.0.1")
        self.accept_calls = 0
        self.sent_texts: list[str] = []
        self.closed: list[tuple[int, str]] = []
        self._messages = ['{"type": "auth", "token": "token-1"}']

    async def accept(self) -> None:
        self.accept_calls += 1

    async def receive_text(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise websocket_route.WebSocketDisconnect()

    async def send_text(self, value: str) -> None:
        self.sent_texts.append(value)

    async def close(self, code: int, reason: str) -> None:
        self.closed.append((code, reason))


class _RateLimiter:
    async def check(self, _client_ip: str):
        return True, 0

    async def reset(self, _client_ip: str) -> None:
        return None

    async def record_failure(self, _client_ip: str):
        return False, 0


class _Manager:
    def __init__(self) -> None:
        self.connected: list[tuple[Any, str, bool]] = []
        self.disconnected: list[tuple[Any, str]] = []

    async def connect(self, websocket: Any, user_id: str, accept: bool = True) -> None:
        self.connected.append((websocket, user_id, accept))

    async def disconnect(self, websocket: Any, user_id: str) -> None:
        self.disconnected.append((websocket, user_id))


@pytest.mark.asyncio
async def test_websocket_auth_message_offloads_json_parse_and_auth_ok_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    manager = _Manager()

    async def fake_run_blocking_io(func, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    async def fake_current_user(token: str):
        assert token == "token-1"
        return SimpleNamespace(sub="user-1")

    monkeypatch.setattr(websocket_route, "run_blocking_io", fake_run_blocking_io, raising=False)
    monkeypatch.setattr(websocket_route, "get_ws_rate_limiter", lambda: _RateLimiter())
    monkeypatch.setattr(websocket_route, "get_current_user_from_websocket", fake_current_user)
    monkeypatch.setattr(websocket_route, "get_connection_manager", lambda: manager)

    websocket = _FakeWebSocket()
    await websocket_route.websocket_endpoint(websocket, token=None)

    assert calls == [json.loads, json.dumps]
    assert websocket.sent_texts == ['{"type": "auth:ok"}']
    assert manager.connected == [(websocket, "user-1", False)]

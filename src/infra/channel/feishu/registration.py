"""Feishu one-click app registration sessions.

The lark-oapi ``register_app`` helper is synchronous and blocks while the user
scans and approves a QR code. This module wraps it in a short-lived background
thread so the API can expose a pollable registration session.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.infra.logging import get_logger

logger = get_logger(__name__)

_SESSION_TTL_SECONDS = 15 * 60
_COMPLETED_SESSION_TTL_SECONDS = 2 * 60
_sessions_lock = threading.Lock()
_sessions: dict[str, "FeishuRegistrationSession"] = {}


@dataclass
class FeishuRegistrationSession:
    id: str
    status: str = "pending"
    qr_url: str | None = None
    expire_in: int | None = None
    app_id: str | None = None
    app_secret: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        payload = {
            "session_id": self.id,
            "status": self.status,
            "qr_url": self.qr_url,
            "expire_in": self.expire_in,
            "app_id": self.app_id,
            "error": self.error,
        }
        if include_secret:
            payload["app_secret"] = self.app_secret
        return payload

    def touch(self) -> None:
        self.updated_at = time.time()


def _cleanup_sessions() -> None:
    now = time.time()
    with _sessions_lock:
        expired = [
            sid
            for sid, session in _sessions.items()
            if now - session.created_at > _SESSION_TTL_SECONDS
            or session.status in {"success", "error", "expired", "cancelled"}
            and now - session.updated_at > _COMPLETED_SESSION_TTL_SECONDS
        ]
        for sid in expired:
            _sessions.pop(sid, None)


def start_registration(source: str = "lambchat") -> FeishuRegistrationSession:
    """Start a Feishu registration session in a background thread."""
    _cleanup_sessions()
    session = FeishuRegistrationSession(id=uuid.uuid4().hex)
    with _sessions_lock:
        _sessions[session.id] = session

    def _run() -> None:
        try:
            import lark_oapi as lark

            def _on_qr(info: dict[str, Any]) -> None:
                session.qr_url = info.get("url")
                session.expire_in = info.get("expire_in")
                session.status = "qr_ready"
                session.touch()

            def _on_status(info: dict[str, Any]) -> None:
                status = info.get("status")
                if status and status != "polling":
                    session.status = str(status)
                    session.touch()

            result = lark.register_app(
                on_qr_code=_on_qr,
                on_status_change=_on_status,
                source=source,
                cancel_event=session.cancel_event,
            )
            session.app_id = result.get("client_id") or result.get("app_id")
            session.app_secret = result.get("client_secret") or result.get("app_secret")
            if session.app_id and session.app_secret:
                session.status = "success"
            else:
                session.status = "error"
                session.error = "Feishu registration result did not include app credentials"
        except Exception as e:
            if session.cancel_event.is_set():
                session.status = "cancelled"
            elif "Expired" in e.__class__.__name__:
                session.status = "expired"
                session.error = "QR code expired"
            else:
                session.status = "error"
                session.error = str(e)
            logger.warning("[Feishu] one-click registration failed: %s", e)
        finally:
            session.touch()

    thread = threading.Thread(target=_run, daemon=True, name=f"feishu-register-{session.id[:8]}")
    thread.start()
    return session


def get_registration(session_id: str) -> FeishuRegistrationSession | None:
    _cleanup_sessions()
    with _sessions_lock:
        return _sessions.get(session_id)


def cancel_registration(session_id: str) -> bool:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if not session:
        return False
    session.cancel_event.set()
    session.status = "cancelled"
    session.touch()
    return True

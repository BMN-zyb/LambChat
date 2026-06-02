"""Feishu one-click app registration sessions.

The lark-oapi ``register_app`` helper is synchronous and blocks while the user
scans and approves a QR code. This module wraps it in a short-lived background
thread so the API can expose a pollable registration session.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import redis

from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)

_SESSION_TTL_SECONDS = 15 * 60
_COMPLETED_SESSION_TTL_SECONDS = 2 * 60
_SHARED_KEY_PREFIX = "feishu:registration:"
_CANCEL_POLL_SECONDS = 1.0
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

    def to_snapshot(self) -> dict[str, Any]:
        payload = self.to_dict(include_secret=True)
        payload["created_at"] = self.created_at
        payload["updated_at"] = self.updated_at
        return payload

    def touch(self) -> None:
        self.updated_at = time.time()


class _SharedRegistrationStore:
    def __init__(self) -> None:
        self._client = redis.Redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
            retry_on_timeout=True,
            password=settings.REDIS_PASSWORD or None,
        )

    def _key(self, session_id: str) -> str:
        return f"{_SHARED_KEY_PREFIX}{session_id}"

    def save(self, session: FeishuRegistrationSession) -> None:
        ttl = (
            _COMPLETED_SESSION_TTL_SECONDS
            if session.status in {"success", "error", "expired", "cancelled"}
            else _SESSION_TTL_SECONDS
        )
        self._client.set(
            self._key(session.id),
            json.dumps(session.to_snapshot()),
            ex=ttl,
        )

    def get(self, session_id: str) -> dict[str, Any] | None:
        raw = self._client.get(self._key(session_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[Feishu] invalid registration snapshot in Redis: %s", session_id)
            return None

    def mark_cancelled(self, session_id: str) -> bool:
        snapshot = self.get(session_id)
        if snapshot is None:
            return False
        snapshot["status"] = "cancelled"
        snapshot["updated_at"] = time.time()
        self._client.set(
            self._key(session_id),
            json.dumps(snapshot),
            ex=_COMPLETED_SESSION_TTL_SECONDS,
        )
        return True


@lru_cache
def _get_shared_store() -> _SharedRegistrationStore:
    return _SharedRegistrationStore()


def _save_shared_session(session: FeishuRegistrationSession) -> None:
    try:
        _get_shared_store().save(session)
    except Exception as e:
        logger.debug("[Feishu] failed to save registration snapshot: %s", e)


def _session_from_snapshot(snapshot: dict[str, Any]) -> FeishuRegistrationSession:
    return FeishuRegistrationSession(
        id=str(snapshot.get("session_id") or snapshot.get("id") or ""),
        status=str(snapshot.get("status") or "pending"),
        qr_url=snapshot.get("qr_url"),
        expire_in=snapshot.get("expire_in"),
        app_id=snapshot.get("app_id"),
        app_secret=snapshot.get("app_secret"),
        error=snapshot.get("error"),
        created_at=float(snapshot.get("created_at") or time.time()),
        updated_at=float(snapshot.get("updated_at") or time.time()),
        cancel_event=threading.Event(),
    )


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


def _watch_distributed_cancel(session: FeishuRegistrationSession) -> None:
    """Mirror cross-instance cancel requests into this process' cancel_event."""
    while not session.cancel_event.wait(_CANCEL_POLL_SECONDS):
        if session.status in {"success", "error", "expired", "cancelled"}:
            return
        try:
            snapshot = _get_shared_store().get(session.id)
        except Exception as e:
            logger.debug("[Feishu] failed to poll registration cancel state: %s", e)
            continue
        if snapshot and snapshot.get("status") == "cancelled":
            session.cancel_event.set()
            session.status = "cancelled"
            session.touch()
            return


def start_registration(source: str = "lambchat") -> FeishuRegistrationSession:
    """Start a Feishu registration session in a background thread."""
    _cleanup_sessions()
    session = FeishuRegistrationSession(id=uuid.uuid4().hex)
    with _sessions_lock:
        _sessions[session.id] = session
    _save_shared_session(session)

    def _run() -> None:
        try:
            import lark_oapi as lark

            def _on_qr(info: dict[str, Any]) -> None:
                session.qr_url = info.get("url")
                session.expire_in = info.get("expire_in")
                session.status = "qr_ready"
                session.touch()
                _save_shared_session(session)

            def _on_status(info: dict[str, Any]) -> None:
                status = info.get("status")
                if status and status != "polling":
                    session.status = str(status)
                    session.touch()
                    _save_shared_session(session)

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
            _save_shared_session(session)

    watcher = threading.Thread(
        target=_watch_distributed_cancel,
        args=(session,),
        daemon=True,
        name=f"feishu-register-cancel-{session.id[:8]}",
    )
    watcher.start()
    thread = threading.Thread(target=_run, daemon=True, name=f"feishu-register-{session.id[:8]}")
    thread.start()
    return session


def get_registration(session_id: str) -> FeishuRegistrationSession | None:
    _cleanup_sessions()
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session:
        return session

    try:
        snapshot = _get_shared_store().get(session_id)
    except Exception as e:
        logger.debug("[Feishu] failed to read registration snapshot: %s", e)
        return None
    if not snapshot:
        return None
    return _session_from_snapshot(snapshot)


def cancel_registration(session_id: str) -> bool:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session:
        session.cancel_event.set()
        session.status = "cancelled"
        session.touch()
        _save_shared_session(session)
        return True
    try:
        return _get_shared_store().mark_cancelled(session_id)
    except Exception as e:
        logger.debug("[Feishu] failed to cancel registration snapshot: %s", e)
        return False

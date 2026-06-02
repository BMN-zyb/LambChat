from __future__ import annotations

import threading

from src.infra.channel.feishu import registration


def test_cleanup_keeps_completed_registration_result_for_polling_window(
    monkeypatch,
) -> None:
    session = registration.FeishuRegistrationSession(
        id="session-1",
        status="success",
        app_id="cli_1",
        app_secret="secret",
        created_at=1000.0,
        updated_at=1000.0,
        cancel_event=threading.Event(),
    )
    with registration._sessions_lock:
        registration._sessions.clear()
        registration._sessions[session.id] = session

    monkeypatch.setattr(registration.time, "time", lambda: 1080.0)

    registration._cleanup_sessions()

    with registration._sessions_lock:
        assert "session-1" in registration._sessions


def test_get_registration_restores_session_from_shared_snapshot(monkeypatch) -> None:
    snapshot = {
        "session_id": "session-1",
        "status": "qr_ready",
        "qr_url": "https://example.test/qr",
        "expire_in": 300,
        "app_id": None,
        "app_secret": None,
        "error": None,
        "created_at": 1000.0,
        "updated_at": 1001.0,
    }

    class FakeSharedStore:
        def get(self, session_id: str):
            assert session_id == "session-1"
            return snapshot

    with registration._sessions_lock:
        registration._sessions.clear()
    monkeypatch.setattr(registration, "_get_shared_store", lambda: FakeSharedStore())

    session = registration.get_registration("session-1")

    assert session is not None
    assert session.id == "session-1"
    assert session.status == "qr_ready"
    assert session.qr_url == "https://example.test/qr"

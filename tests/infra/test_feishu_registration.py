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

from __future__ import annotations

import pytest

from src.infra.session.manager import SessionManager


class _FileRecordStorage:
    def __init__(self) -> None:
        self.deleted_keys: list[str] = []

    async def release_references(self, _keys: list[str]) -> int:
        return 0

    async def find_by_key(self, key: str) -> dict:
        return {"key": key, "reference_count": 0}

    async def delete_by_key(self, key: str) -> bool:
        self.deleted_keys.append(key)
        return True


class _TraceStorage:
    def __init__(self) -> None:
        self.get_session_events_calls: list[tuple[str, dict]] = []

    async def get_session_events(self, _session_id: str, **kwargs) -> list[dict]:
        self.get_session_events_calls.append((_session_id, kwargs))
        return []

    async def delete_session_traces(self, _session_id: str) -> int:
        return 0


@pytest.mark.asyncio
async def test_cleanup_unreferenced_files_skips_storage_when_s3_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    file_records = _FileRecordStorage()
    manager._file_record_storage = file_records

    monkeypatch.setattr("src.infra.session.manager.get_s3_enabled", lambda: False)

    async def fail_get_or_init_storage():
        raise AssertionError("S3 storage should not be used when S3 is disabled")

    monkeypatch.setattr("src.infra.session.manager.get_or_init_storage", fail_get_or_init_storage)

    cleaned = await manager._cleanup_unreferenced_files(["attachments/u1/missing.png"])

    assert cleaned == 1
    assert file_records.deleted_keys == ["attachments/u1/missing.png"]


@pytest.mark.asyncio
async def test_cleanup_unreferenced_files_uses_initialized_storage_when_s3_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    file_records = _FileRecordStorage()
    manager._file_record_storage = file_records
    deleted_storage_keys: list[str] = []

    class _Storage:
        async def delete_file(self, key: str) -> bool:
            deleted_storage_keys.append(key)
            return True

    async def get_or_init_storage():
        return _Storage()

    monkeypatch.setattr("src.infra.session.manager.get_s3_enabled", lambda: True)
    monkeypatch.setattr("src.infra.session.manager.get_or_init_storage", get_or_init_storage)

    cleaned = await manager._cleanup_unreferenced_files(["attachments/u1/file.png"])

    assert cleaned == 1
    assert deleted_storage_keys == ["attachments/u1/file.png"]
    assert file_records.deleted_keys == ["attachments/u1/file.png"]


@pytest.mark.asyncio
async def test_delete_session_continues_when_checkpoint_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager._trace_storage = _TraceStorage()
    manager._file_record_storage = _FileRecordStorage()

    deleted_sessions: list[str] = []

    class _Storage:
        async def delete(self, session_id: str) -> bool:
            deleted_sessions.append(session_id)
            return True

    async def _fail_delete_checkpoints(_session_id: str) -> None:
        raise RuntimeError("checkpoint cleanup failed")

    class _RevealedStorage:
        async def delete_by_session(self, _session_id: str) -> int:
            return 0

    manager.storage = _Storage()
    monkeypatch.setattr(
        "src.infra.session.manager.delete_checkpoints_for_thread",
        _fail_delete_checkpoints,
    )
    monkeypatch.setattr(
        "src.infra.revealed_file.storage.get_revealed_file_storage",
        lambda: _RevealedStorage(),
    )

    deleted = await manager.delete_session("session-1")

    assert deleted is True
    assert deleted_sessions == ["session-1"]


@pytest.mark.asyncio
async def test_delete_session_cleans_checkpoints_after_session_document_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager()
    manager._trace_storage = _TraceStorage()
    manager._file_record_storage = _FileRecordStorage()
    calls: list[str] = []

    class _Storage:
        async def delete(self, _session_id: str) -> bool:
            calls.append("session")
            return True

    async def _delete_checkpoints(_session_id: str) -> None:
        calls.append("checkpoints")

    class _RevealedStorage:
        async def delete_by_session(self, _session_id: str) -> int:
            return 0

    manager.storage = _Storage()
    monkeypatch.setattr(
        "src.infra.session.manager.delete_checkpoints_for_thread",
        _delete_checkpoints,
    )
    monkeypatch.setattr(
        "src.infra.revealed_file.storage.get_revealed_file_storage",
        lambda: _RevealedStorage(),
    )

    deleted = await manager.delete_session("session-1")

    assert deleted is True
    assert calls == ["session", "checkpoints"]


@pytest.mark.asyncio
async def test_collect_user_attachment_keys_uses_bounded_user_message_query() -> None:
    manager = SessionManager()

    class _AttachmentTraceStorage:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def get_session_events(self, session_id: str, **kwargs) -> list[dict]:
            self.calls.append((session_id, kwargs))
            return [
                {
                    "event_type": "user:message",
                    "data": {
                        "attachments": [
                            {"key": "attachments/u1/a.png"},
                            {"key": "attachments/u1/a.png"},
                            {"key": " attachments/u1/b.txt "},
                        ]
                    },
                },
                {
                    "event_type": "assistant:message",
                    "data": {"attachments": [{"key": "attachments/u1/ignored.png"}]},
                },
            ]

    trace_storage = _AttachmentTraceStorage()
    manager._trace_storage = trace_storage

    keys = await manager._collect_user_attachment_keys("session-1")

    assert keys == ["attachments/u1/a.png", "attachments/u1/b.txt"]
    assert trace_storage.calls == [
        (
            "session-1",
            {
                "event_types": ["user:message"],
                "completed_only": False,
                "max_events": 1000,
            },
        )
    ]

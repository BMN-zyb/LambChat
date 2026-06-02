from __future__ import annotations

import pytest

from src.infra.session.manager import SessionManager
from src.infra.session.storage import SessionStorage


class _UpdateResult:
    matched_count = 1


class _FakeCollection:
    def __init__(self) -> None:
        self.update_one_calls: list[tuple[dict, dict]] = []
        self.find_one_and_update_called = False

    async def update_one(self, query: dict, update: dict) -> _UpdateResult:
        self.update_one_calls.append((query, update))
        return _UpdateResult()

    async def find_one_and_update(self, *_args, **_kwargs):
        self.find_one_and_update_called = True
        raise AssertionError("metadata-only updates should not fetch the full session document")


@pytest.mark.asyncio
async def test_update_metadata_only_uses_update_one_without_fetching_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ensure_indexes(_self) -> None:
        return None

    monkeypatch.setattr(SessionStorage, "ensure_indexes_if_needed", fake_ensure_indexes)
    storage = SessionStorage()
    collection = _FakeCollection()
    storage._collection = collection

    updated = await storage.update_metadata_only(
        "session-1",
        {"current_run_id": "run-1", "agent_id": "search"},
    )

    assert updated is True
    assert collection.find_one_and_update_called is False
    assert collection.update_one_calls
    query, update = collection.update_one_calls[0]
    assert query == {"session_id": "session-1"}
    assert update["$set"]["metadata.current_run_id"] == "run-1"
    assert update["$set"]["metadata.agent_id"] == "search"


@pytest.mark.asyncio
async def test_session_manager_get_session_events_forwards_limit_as_max_events() -> None:
    calls: list[tuple] = []

    class _FakeTraceStorage:
        async def get_session_events(self, *args, **kwargs):
            calls.append((args, kwargs))
            return [{"event_type": "done"}]

    manager = SessionManager()
    manager._trace_storage = _FakeTraceStorage()

    events = await manager.get_session_events("session-1", limit=25)

    assert events == [{"event_type": "done"}]
    assert calls == [(("session-1",), {"max_events": 25})]

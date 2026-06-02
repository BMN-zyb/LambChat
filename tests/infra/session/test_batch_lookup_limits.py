from __future__ import annotations

import pytest

from src.infra.session import storage as session_storage


class _EmptyAsyncCursor:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _RecordingCollection:
    def __init__(self) -> None:
        self.queries = []

    def find(self, query):
        self.queries.append(query)
        return _EmptyAsyncCursor()


class _RecordingListCursor:
    def __init__(self) -> None:
        self.skip_value = None
        self.limit_value = None
        self.to_list_length = None

    def skip(self, value):
        self.skip_value = value
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def sort(self, *_args):
        return self

    async def to_list(self, length=None):
        self.to_list_length = length
        return []


class _RecordingListCollection:
    def __init__(self) -> None:
        self.cursor = _RecordingListCursor()

    async def count_documents(self, _query):
        return 0

    def find(self, *_args, **_kwargs):
        return self.cursor


@pytest.mark.asyncio
async def test_get_by_session_ids_caps_mongo_in_query(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = _RecordingCollection()
    storage = session_storage.SessionStorage()
    storage._collection = collection

    async def _skip_indexes(_self):
        return None

    monkeypatch.setattr(session_storage.SessionStorage, "ensure_indexes_if_needed", _skip_indexes)

    session_ids = [
        f"session-{index}" for index in range(session_storage.SESSION_BATCH_LOOKUP_LIMIT + 25)
    ]

    result = await storage.get_by_session_ids(session_ids)

    assert result == {}
    queried_ids = collection.queries[0]["session_id"]["$in"]
    assert len(queried_ids) == session_storage.SESSION_BATCH_LOOKUP_LIMIT


@pytest.mark.asyncio
async def test_list_sessions_caps_direct_storage_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = _RecordingListCollection()
    storage = session_storage.SessionStorage()
    storage._collection = collection

    async def _skip_indexes(_self):
        return None

    monkeypatch.setattr(session_storage.SessionStorage, "ensure_indexes_if_needed", _skip_indexes)

    sessions, total = await storage.list_sessions(user_id="user", skip=-10, limit=10_000)

    assert sessions == []
    assert total == 0
    assert collection.cursor.skip_value == 0
    assert collection.cursor.limit_value == session_storage.SESSION_LIST_LOOKUP_LIMIT
    assert collection.cursor.to_list_length == session_storage.SESSION_LIST_LOOKUP_LIMIT

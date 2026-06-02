from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.infra.share.storage import ShareStorage

SHARE_LIST_LIMIT_MAX = 100


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.skip_value: int | None = None
        self.limit_value: int | None = None
        self.to_list_length: int | None = None

    def skip(self, value: int):
        self.skip_value = value
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def sort(self, *_args):
        return self

    async def to_list(self, length: int | None = None):
        self.to_list_length = length
        cap = self.limit_value if self.limit_value is not None else length
        return self._docs[: cap or None]


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.cursor = _FakeCursor(docs)

    async def count_documents(self, _query: dict[str, Any]) -> int:
        return 1_000

    def find(self, *_args):
        return self.cursor


def _share_doc(index: int) -> dict[str, Any]:
    return {
        "_id": f"id-{index}",
        "share_id": f"share-{index}",
        "session_id": "session-1",
        "share_type": "full",
        "visibility": "public",
        "run_ids": None,
        "created_at": datetime(2026, 4, 25, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_list_by_owner_clamps_storage_limit() -> None:
    storage = ShareStorage()
    storage._collection = _FakeCollection([_share_doc(index) for index in range(150)])

    shares, total = await storage.list_by_owner("owner-1", limit=10_000)

    assert total == 1_000
    assert len(shares) == SHARE_LIST_LIMIT_MAX
    assert storage.collection.cursor.limit_value == SHARE_LIST_LIMIT_MAX
    assert storage.collection.cursor.to_list_length == SHARE_LIST_LIMIT_MAX


@pytest.mark.asyncio
async def test_list_by_session_applies_cursor_limit() -> None:
    storage = ShareStorage()
    storage._collection = _FakeCollection([_share_doc(index) for index in range(150)])

    shares = await storage.list_by_session("session-1")

    assert len(shares) == SHARE_LIST_LIMIT_MAX
    assert storage.collection.cursor.limit_value == SHARE_LIST_LIMIT_MAX
    assert storage.collection.cursor.to_list_length == SHARE_LIST_LIMIT_MAX

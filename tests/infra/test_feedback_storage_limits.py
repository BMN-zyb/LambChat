from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from bson import ObjectId

from src.infra.feedback.storage import FeedbackStorage

FEEDBACK_LIST_LIMIT_MAX = 100


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.skip_value: int | None = None
        self.limit_value: int | None = None

    def sort(self, *_args):
        return self

    def skip(self, value: int):
        self.skip_value = value
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def __aiter__(self):
        cap = self.limit_value
        self._iter = iter(self._docs[: cap or None])
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.cursor = _FakeCursor(docs)

    def find(self, *_args):
        return self.cursor


def _feedback_doc() -> dict[str, Any]:
    return {
        "_id": ObjectId(),
        "user_id": "user-1",
        "username": "user",
        "session_id": "session-1",
        "run_id": "run-1",
        "rating": "up",
        "comment": None,
        "created_at": datetime(2026, 4, 25, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_list_feedback_clamps_storage_limit() -> None:
    storage = FeedbackStorage()
    storage._collection = _FakeCollection([_feedback_doc() for _ in range(150)])

    feedbacks = await storage.list(limit=10_000)

    assert len(feedbacks) == FEEDBACK_LIST_LIMIT_MAX
    assert storage.collection.cursor.limit_value == FEEDBACK_LIST_LIMIT_MAX

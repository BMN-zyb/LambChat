from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from bson import ObjectId

from src.infra.push.storage import PushSubscriptionStorage


def _subscription_doc(
    sub_id: ObjectId | None = None,
    *,
    user_id: str = "user-1",
    endpoint: str = "https://push.example.com/sub/123",
) -> dict[str, Any]:
    return {
        "_id": sub_id or ObjectId(),
        "user_id": user_id,
        "endpoint": endpoint,
        "keys": {"p256dh": "fake-key", "auth": "fake-auth"},
        "user_agent": "Mozilla/5.0",
        "created_at": datetime(2026, 1, 15),
        "last_used_at": datetime(2026, 3, 1),
    }


class _UpdateOneResult:
    def __init__(self, matched_count: int = 1):
        self.matched_count = matched_count


class _DeleteOneResult:
    def __init__(self, deleted_count: int = 1):
        self.deleted_count = deleted_count


class _DeleteManyResult:
    def __init__(self, deleted_count: int = 1):
        self.deleted_count = deleted_count


class _FindCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.update_calls: list[dict[str, Any]] = []
        self.delete_one_calls: list[dict[str, Any]] = []
        self.delete_many_calls: list[dict[str, Any]] = []
        self.find_calls: list[dict[str, Any]] = []
        self.create_index_calls: list[tuple | list] = []
        self.find_one_result: dict[str, Any] | None = None

    async def update_one(self, query: dict, update: dict, *, upsert: bool = False):
        self.update_calls.append({"query": query, "update": update, "upsert": upsert})
        return _UpdateOneResult()

    async def delete_one(self, query: dict):
        self.delete_one_calls.append(query)
        return _DeleteOneResult(len(self._docs))

    async def delete_many(self, query: dict):
        self.delete_many_calls.append(query)
        return _DeleteManyResult(len(self._docs))

    def find(self, query: dict):
        self.find_calls.append(query)
        return _FindCursor([dict(doc) for doc in self._docs])

    async def find_one(self, query: dict):
        return self.find_one_result

    async def create_index(self, keys, **kwargs):
        self.create_index_calls.append(keys)


@pytest.mark.asyncio
async def test_get_by_user_queries_correct_user_id() -> None:
    storage = PushSubscriptionStorage()
    fake = _FakeCollection([_subscription_doc()])
    storage._collection = fake

    items = await storage.get_by_user("user-42")

    assert fake.find_calls == [{"user_id": "user-42"}]
    assert len(items) == 1
    assert items[0].endpoint == "https://push.example.com/sub/123"


@pytest.mark.asyncio
async def test_get_by_user_returns_empty_for_unknown_user() -> None:
    storage = PushSubscriptionStorage()
    fake = _FakeCollection([])
    storage._collection = fake

    items = await storage.get_by_user("unknown")

    assert items == []


@pytest.mark.asyncio
async def test_delete_by_endpoint_delegates_correctly() -> None:
    storage = PushSubscriptionStorage()
    fake = _FakeCollection([_subscription_doc()])
    storage._collection = fake

    result = await storage.delete_by_endpoint("https://push.example.com/sub/123")

    assert result is True
    assert fake.delete_one_calls == [{"endpoint": "https://push.example.com/sub/123"}]


@pytest.mark.asyncio
async def test_delete_by_endpoint_returns_false_when_no_match() -> None:
    storage = PushSubscriptionStorage()
    fake = _FakeCollection([])
    storage._collection = fake

    result = await storage.delete_by_endpoint("https://nonexistent.example.com")

    assert result is False


@pytest.mark.asyncio
async def test_delete_by_user_deletes_all_user_subscriptions() -> None:
    storage = PushSubscriptionStorage()
    fake = _FakeCollection([_subscription_doc()])
    storage._collection = fake

    count = await storage.delete_by_user("user-1")

    assert count == 1
    assert fake.delete_many_calls == [{"user_id": "user-1"}]


@pytest.mark.asyncio
async def test_touch_last_used_updates_timestamp() -> None:
    storage = PushSubscriptionStorage()
    fake = _FakeCollection([])
    storage._collection = fake

    await storage.touch_last_used("https://push.example.com/sub/123")

    assert len(fake.update_calls) == 1
    assert fake.update_calls[0]["query"] == {"endpoint": "https://push.example.com/sub/123"}
    assert "$set" in fake.update_calls[0]["update"]
    assert "last_used_at" in fake.update_calls[0]["update"]["$set"]


@pytest.mark.asyncio
async def test_delete_expired_queries_old_subscriptions() -> None:
    storage = PushSubscriptionStorage()
    fake = _FakeCollection([])
    storage._collection = fake

    count = await storage.delete_expired(days=90)

    assert fake.delete_many_calls
    query = fake.delete_many_calls[0]
    assert "$or" in query
    assert len(query["$or"]) == 2


@pytest.mark.asyncio
async def test_create_indexes_builds_correct_indexes() -> None:
    storage = PushSubscriptionStorage()
    fake = _FakeCollection([])
    storage._collection = fake

    await storage.create_indexes()

    assert len(fake.create_index_calls) == 3
    assert fake.create_index_calls[0] == [("user_id", 1)]
    assert fake.create_index_calls[1] == [("endpoint", 1)]
    assert fake.create_index_calls[2] == [("created_at", 1)]


@pytest.mark.asyncio
async def test_close_clears_collection_ref() -> None:
    storage = PushSubscriptionStorage()
    storage._collection = object()

    await storage.close()

    assert storage._collection is None

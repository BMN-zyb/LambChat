from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.infra.user.storage import UserStorage
from src.kernel.exceptions import ValidationError

USER_LIST_LIMIT_MAX = 100


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.skip_value: int | None = None
        self.limit_value: int | None = None

    def skip(self, value: int):
        self.skip_value = value
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def __aiter__(self):
        self._iter = iter(self._docs[: self.limit_value or None])
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.cursor = _FakeCursor(docs)

    def find(self, _query: dict[str, Any]):
        return self.cursor


class _FakeMetadataCollection:
    def __init__(self) -> None:
        self.find_one_called = False
        self.find_one_and_update_calls: list[tuple[dict[str, Any], dict[str, Any], Any]] = []

    async def find_one(self, *_args, **_kwargs):
        self.find_one_called = True
        raise AssertionError("metadata update should not prefetch the full user document")

    async def find_one_and_update(self, query, update, return_document=True):
        self.find_one_and_update_calls.append((query, update, return_document))
        doc = _user_doc(1)
        doc["_id"] = query["_id"]
        doc["metadata"] = {
            "existing": "kept",
            "theme": update["$set"]["metadata.theme"],
            "language": update["$set"]["metadata.language"],
        }
        return doc


def _user_doc(index: int) -> dict[str, Any]:
    return {
        "_id": f"user-{index}",
        "username": f"user-{index}",
        "email": f"user-{index}@example.com",
        "roles": ["user"],
        "permissions": [],
        "is_active": True,
        "email_verified": True,
        "created_at": datetime(2026, 4, 25, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 4, 25, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_list_users_clamps_storage_limit() -> None:
    storage = UserStorage()
    storage._collection = _FakeCollection([_user_doc(index) for index in range(150)])

    users = await storage.list_users(limit=10_000)

    assert len(users) == USER_LIST_LIMIT_MAX
    assert storage.collection.cursor.limit_value == USER_LIST_LIMIT_MAX


@pytest.mark.asyncio
async def test_update_metadata_uses_atomic_field_set_without_full_prefetch() -> None:
    storage = UserStorage()
    storage._collection = _FakeMetadataCollection()
    user_id = "507f1f77bcf86cd799439011"

    user = await storage.update_metadata(
        user_id,
        {
            "theme": "dark",
            "language": "zh-CN",
        },
    )

    assert user is not None
    assert user.metadata == {
        "existing": "kept",
        "theme": "dark",
        "language": "zh-CN",
    }
    assert storage.collection.find_one_called is False
    query, update, return_document = storage.collection.find_one_and_update_calls[0]
    assert str(query["_id"]) == user_id
    assert update == {
        "$set": {
            "metadata.theme": "dark",
            "metadata.language": "zh-CN",
            "updated_at": update["$set"]["updated_at"],
        }
    }
    assert isinstance(update["$set"]["updated_at"], datetime)
    assert return_document is True


@pytest.mark.asyncio
async def test_update_metadata_rejects_mongo_path_keys_before_storage_call() -> None:
    storage = UserStorage()
    storage._collection = _FakeMetadataCollection()

    with pytest.raises(ValidationError, match="Invalid metadata key"):
        await storage.update_metadata(
            "507f1f77bcf86cd799439011",
            {"theme.color": "dark"},
        )

    assert storage.collection.find_one_and_update_calls == []

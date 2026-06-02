from __future__ import annotations

import json
from typing import Any

import pytest

from src.infra import skill


class _AsyncCursor:
    def __init__(self, docs: list[dict[str, Any]], on_yield=None) -> None:
        self._docs = docs
        self._index = 0
        self.batch_size_value: int | None = None
        self._on_yield = on_yield

    def batch_size(self, value: int):
        self.batch_size_value = value
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._docs):
            raise StopAsyncIteration
        doc = self._docs[self._index]
        self._index += 1
        if self._on_yield is not None:
            self._on_yield(self._index)
        return doc


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]] | None = None, on_yield=None) -> None:
        self.docs = docs or []
        self.on_yield = on_yield
        self.cursors: list[_AsyncCursor] = []
        self.find_calls: list[tuple[dict[str, Any], dict[str, int] | None]] = []
        self.update_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.insert_calls: list[dict[str, Any]] = []

    async def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for doc in self.docs if _matches(doc, query))

    def find(
        self,
        query: dict[str, Any],
        projection: dict[str, int] | None = None,
    ) -> _AsyncCursor:
        self.find_calls.append((query, projection))
        docs = [doc for doc in self.docs if _matches(doc, query)]
        cursor = _AsyncCursor(docs, on_yield=self.on_yield)
        self.cursors.append(cursor)
        return cursor

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return next((doc for doc in self.docs if _matches(doc, query)), None)

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self.insert_calls.append(doc)
        self.docs.append(doc)

    async def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> None:
        del upsert
        self.update_calls.append((query, update))


class _FakeDb:
    def __init__(self) -> None:
        self.collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return self.collections.setdefault(name, _FakeCollection())


class _FakeClient:
    def __init__(self, db: _FakeDb) -> None:
        self.db = db

    def __getitem__(self, name: str) -> _FakeDb:
        del name
        return self.db


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, value in query.items():
        current = doc.get(key)
        if isinstance(value, dict) and "$ne" in value:
            if current == value["$ne"]:
                return False
            continue
        if current != value:
            return False
    return True


@pytest.mark.asyncio
async def test_legacy_skill_migration_uses_batched_cursors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skill, "LEGACY_SKILL_MIGRATION_BATCH_SIZE", 2, raising=False)
    db = _FakeDb()
    db.collections["system_skills"] = _FakeCollection(
        [{"name": f"system-{index}"} for index in range(3)]
    )
    db.collections["skill_files"] = _FakeCollection([])

    monkeypatch.setattr(
        "src.infra.storage.mongodb.get_mongo_client",
        lambda: _FakeClient(db),
    )

    await skill._migrate_legacy_skills(_Logger())

    system_cursors = db["system_skills"].cursors
    assert [cursor.batch_size_value for cursor in system_cursors] == [2]


@pytest.mark.asyncio
async def test_legacy_skill_migration_does_not_preload_all_system_skill_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDb()
    old_system = _FakeCollection([{"name": f"system-{index}"} for index in range(3)])
    db.collections["system_skills"] = old_system
    db.collections["user_skill_preferences"] = _FakeCollection(
        [{"user_id": "user-1", "skill_name": "system-1", "enabled": True}]
    )
    db.collections["skill_files"] = _FakeCollection([])

    monkeypatch.setattr(
        "src.infra.storage.mongodb.get_mongo_client",
        lambda: _FakeClient(db),
    )

    await skill._migrate_legacy_skills(_Logger())

    assert ({}, {"name": 1}) not in old_system.find_calls


@pytest.mark.asyncio
async def test_legacy_skill_migration_flushes_disabled_users_incrementally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skill, "LEGACY_SKILL_MIGRATION_USER_FLUSH_SIZE", 2, raising=False)
    db = _FakeDb()
    users = _FakeCollection([])

    def _assert_flushed_after_second_toggle(index: int) -> None:
        if index == 3:
            assert len(users.update_calls) == 2

    db.collections["skill_toggles"] = _FakeCollection(
        [
            {"user_id": f"user-{index}", "skill_name": "planner", "enabled": False}
            for index in range(3)
        ],
        on_yield=_assert_flushed_after_second_toggle,
    )
    db.collections["skill_files"] = _FakeCollection([])
    db.collections["users"] = users

    monkeypatch.setattr(
        "src.infra.storage.mongodb.get_mongo_client",
        lambda: _FakeClient(db),
    )

    await skill._migrate_legacy_skills(_Logger())

    assert len(users.update_calls) == 3
    assert users.update_calls[0][0] == {"id": "user-0"}
    assert users.update_calls[1][0] == {"id": "user-1"}
    assert users.update_calls[2][0] == {"id": "user-2"}


@pytest.mark.asyncio
async def test_legacy_skill_migration_offloads_meta_json_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    db = _FakeDb()
    db.collections["skill_toggles"] = _FakeCollection(
        [
            {
                "user_id": "user-1",
                "skill_name": "planner",
                "enabled": True,
                "installed_from": "marketplace",
                "published_marketplace_name": "Planner",
            }
        ]
    )
    skill_files = _FakeCollection([])
    db.collections["skill_files"] = skill_files

    async def _fake_run_blocking_io(func, /, *args: Any, **kwargs: Any):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(skill, "run_blocking_io", _fake_run_blocking_io, raising=False)
    monkeypatch.setattr(
        "src.infra.storage.mongodb.get_mongo_client",
        lambda: _FakeClient(db),
    )

    await skill._migrate_legacy_skills(_Logger())

    assert calls == [json.dumps]
    inserted = skill_files.insert_calls[0]
    assert inserted["file_path"] == "__meta__"
    assert json.loads(inserted["content"])["installed_from"] == "marketplace"


class _Logger:
    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass

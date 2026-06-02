import json
import re
from datetime import datetime
from typing import Any

import pytest
from bson import ObjectId

from src.infra.role import storage as role_storage
from src.infra.role.storage import RoleStorage


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.values[key] = value
        return True

    async def incr(self, key: str) -> int:
        next_value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(next_value)
        return next_value


class _FakeCollection:
    async def find_one(self, query: dict) -> dict | None:
        if query != {"name": "admin"}:
            return None
        return {
            "_id": "role-1",
            "name": "admin",
            "description": "Administrator",
            "permissions": [],
            "allowed_agents": [],
            "limits": None,
            "is_system": True,
            "created_at": datetime(2026, 1, 1),
            "updated_at": datetime(2026, 1, 1),
        }


class _LargeAllowedAgentsCollection:
    async def find_one(self, query: dict) -> dict | None:
        if query != {"name": "large"}:
            return None
        return {
            "_id": "role-large",
            "name": "large",
            "description": "Large Role",
            "permissions": [],
            "allowed_agents": [
                f"agent-{index}" for index in range(role_storage.ROLE_ALLOWED_AGENTS_LIMIT + 25)
            ],
            "limits": None,
            "is_system": False,
            "created_at": datetime(2026, 1, 1),
            "updated_at": datetime(2026, 1, 1),
        }


class _FakeRoleCursor:
    def __init__(self) -> None:
        self.operations: list[tuple[str, int | str]] = []

    def sort(self, field: str, direction: int):
        self.operations.append(("sort", field))
        self.operations.append(("direction", direction))
        return self

    def skip(self, skip: int):
        self.operations.append(("skip", skip))
        return self

    def limit(self, limit: int):
        self.operations.append(("limit", limit))
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _ListRoleCursor(_FakeRoleCursor):
    def __init__(self, docs: list[dict]) -> None:
        super().__init__()
        self._docs = docs
        self.limit_value: int | None = None

    def limit(self, limit: int):
        self.limit_value = limit
        return super().limit(limit)

    def __aiter__(self):
        self._iter = iter(self._docs[: self.limit_value or None])
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _ListRolesCollection:
    def __init__(self, docs: list[dict]) -> None:
        self.cursor = _ListRoleCursor(docs)

    def find(self, _query: dict):
        return self.cursor


class _SearchCollection:
    def __init__(self) -> None:
        self.find_query: dict | None = None
        self.count_query: dict | None = None
        self.cursor = _FakeRoleCursor()

    def find(self, query: dict):
        self.find_query = query
        return self.cursor

    async def count_documents(self, query: dict) -> int:
        self.count_query = query
        return 0


class _EmptyAsyncCursor:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _RecordingIdsCollection:
    def __init__(self) -> None:
        self.find_queries: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any]):
        self.find_queries.append(query)
        return _EmptyAsyncCursor()


class _CountingRoleStorage(RoleStorage):
    def __init__(self) -> None:
        super().__init__()
        self.looked_up_names: list[str] = []

    async def get_by_name(self, name: str):
        self.looked_up_names.append(name)
        return None


@pytest.mark.asyncio
async def test_get_by_name_uses_dedicated_redis_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    isolated_pool_flags: list[bool] = []

    monkeypatch.setattr(
        "src.infra.role.storage.create_redis_client",
        lambda isolated_pool=False: isolated_pool_flags.append(isolated_pool) or fake_redis,
    )

    storage = RoleStorage()
    storage._collection = _FakeCollection()

    role = await storage.get_by_name("admin")

    assert role is not None
    assert role.name == "admin"
    assert isolated_pool_flags == [True]


@pytest.mark.asyncio
async def test_get_by_name_caps_allowed_agents_from_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.infra.role.storage.create_redis_client",
        lambda isolated_pool=False: _FakeRedis(),
    )
    storage = RoleStorage()
    storage._collection = _LargeAllowedAgentsCollection()

    role = await storage.get_by_name("large")

    assert role is not None
    assert role.allowed_agents == [
        f"agent-{index}" for index in range(role_storage.ROLE_ALLOWED_AGENTS_LIMIT)
    ]


@pytest.mark.asyncio
async def test_get_by_name_caps_allowed_agents_from_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    fake_redis.values["role:obj_ver:large"] = "1"
    fake_redis.values["role:obj:large:v1"] = json.dumps(
        {
            "id": "role-large",
            "name": "large",
            "description": "Large Role",
            "permissions": [],
            "allowed_agents": [
                f"agent-{index}" for index in range(role_storage.ROLE_ALLOWED_AGENTS_LIMIT + 25)
            ],
            "limits": None,
            "is_system": False,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
    )
    monkeypatch.setattr(
        "src.infra.role.storage.create_redis_client",
        lambda isolated_pool=False: fake_redis,
    )
    storage = RoleStorage()
    storage._collection = _LargeAllowedAgentsCollection()

    role = await storage.get_by_name("large")

    assert role is not None
    assert role.allowed_agents == [
        f"agent-{index}" for index in range(role_storage.ROLE_ALLOWED_AGENTS_LIMIT)
    ]


@pytest.mark.asyncio
async def test_get_by_name_offloads_role_cache_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    fake_redis = _FakeRedis()
    fake_redis.values["role:obj_ver:admin"] = "1"
    fake_redis.values["role:obj:admin:v1"] = json.dumps(
        {
            "id": "role-1",
            "name": "admin",
            "description": "Administrator",
            "permissions": [],
            "allowed_agents": [],
            "limits": None,
            "is_system": True,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
    )

    async def _fake_run_blocking_io(func, /, *args: Any, **kwargs: Any):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(role_storage, "_role_cache_redis", None)
    monkeypatch.setattr(
        "src.infra.role.storage.create_redis_client",
        lambda isolated_pool=False: fake_redis,
    )
    monkeypatch.setattr(role_storage, "run_blocking_io", _fake_run_blocking_io, raising=False)
    storage = RoleStorage()
    storage._collection = _FakeCollection()

    role = await storage.get_by_name("admin")

    assert calls == [json.loads]
    assert role is not None
    assert role.name == "admin"


@pytest.mark.asyncio
async def test_get_by_name_offloads_role_cache_json_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    fake_redis = _FakeRedis()

    async def _fake_run_blocking_io(func, /, *args: Any, **kwargs: Any):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(role_storage, "_role_cache_redis", None)
    monkeypatch.setattr(
        "src.infra.role.storage.create_redis_client",
        lambda isolated_pool=False: fake_redis,
    )
    monkeypatch.setattr(role_storage, "run_blocking_io", _fake_run_blocking_io, raising=False)
    storage = RoleStorage()
    storage._collection = _FakeCollection()

    role = await storage.get_by_name("admin")

    assert calls == [json.dumps]
    assert role is not None
    assert fake_redis.values["role:obj:admin:v0"].startswith('{"id": "role-1"')


@pytest.mark.asyncio
async def test_role_search_escapes_regex_metacharacters() -> None:
    storage = RoleStorage()
    collection = _SearchCollection()
    storage._collection = collection

    await storage.list_roles(q="A.B")
    await storage.count_roles(q="A.B")

    expected = re.escape("A.B")
    assert collection.find_query == {
        "$or": [
            {"name": {"$regex": expected, "$options": "i"}},
            {"description": {"$regex": expected, "$options": "i"}},
        ]
    }
    assert collection.count_query == collection.find_query


@pytest.mark.asyncio
async def test_list_roles_clamps_storage_limit() -> None:
    storage = RoleStorage()
    storage._collection = _ListRolesCollection(
        [
            {
                "_id": f"role-{index}",
                "name": f"role-{index}",
                "description": "Role",
                "permissions": [],
                "allowed_agents": [],
                "limits": None,
                "is_system": False,
                "created_at": datetime(2026, 1, 1),
                "updated_at": datetime(2026, 1, 1),
            }
            for index in range(250)
        ]
    )

    roles = await storage.list_roles(limit=10_000)

    assert len(roles) == 200
    assert storage.collection.cursor.limit_value == 200


@pytest.mark.asyncio
async def test_get_by_ids_caps_mongo_in_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(role_storage, "ROLE_BATCH_LOOKUP_LIMIT", 3, raising=False)
    storage = RoleStorage()
    collection = _RecordingIdsCollection()
    storage._collection = collection
    ids = [str(ObjectId()) for _ in range(5)]

    roles = await storage.get_by_ids(ids)

    assert roles == []
    assert len(collection.find_queries[0]["_id"]["$in"]) == 3


@pytest.mark.asyncio
async def test_get_by_names_caps_sequential_cache_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(role_storage, "ROLE_BATCH_LOOKUP_LIMIT", 3, raising=False)
    storage = _CountingRoleStorage()

    roles = await storage.get_by_names([f"role-{index}" for index in range(5)] + ["role-1"])

    assert roles == []
    assert storage.looked_up_names == ["role-0", "role-1", "role-2"]

from __future__ import annotations

import pytest

from src.infra.upload.file_record import FileRecordStorage


class _FakeUpdateResult:
    modified_count = 100


class _FakeCollection:
    def __init__(self) -> None:
        self.update_calls: list[tuple[dict, dict]] = []

    async def update_many(self, query: dict, update: dict):
        self.update_calls.append((query, update))
        return _FakeUpdateResult()


async def _noop_async() -> None:
    return None


@pytest.mark.asyncio
async def test_add_references_bounds_unique_keys_before_mongo_update() -> None:
    storage = FileRecordStorage()
    storage.ensure_indexes_if_needed = _noop_async
    storage._collection = _FakeCollection()

    modified = await storage.add_references([f"key-{index}" for index in range(150)])

    assert modified == 100
    query, _update = storage.collection.update_calls[0]
    assert len(query["key"]["$in"]) == storage.REFERENCE_KEYS_MAX

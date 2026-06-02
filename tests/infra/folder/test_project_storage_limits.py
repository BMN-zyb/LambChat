from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.infra.folder.storage import ProjectStorage

PROJECT_LIST_LIMIT = 100


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.limit_value: int | None = None
        self.to_list_length: int | None = None

    def sort(self, *_args):
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    async def to_list(self, length: int | None = None):
        self.to_list_length = length
        cap = self.limit_value if self.limit_value is not None else length
        return self._docs[: cap or None]


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.cursor = _FakeCursor(docs)

    def find(self, *_args):
        return self.cursor


def _project_doc(index: int) -> dict[str, Any]:
    return {
        "_id": f"project-{index}",
        "name": f"Project {index}",
        "type": "custom",
        "icon": "Folder",
        "sort_order": index,
        "user_id": "user-1",
        "created_at": datetime(2026, 4, 25, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 4, 25, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_list_projects_applies_cursor_limit() -> None:
    storage = ProjectStorage()
    storage._collection = _FakeCollection([_project_doc(index) for index in range(150)])

    projects = await storage.list_projects("user-1")

    assert len(projects) == PROJECT_LIST_LIMIT
    assert storage.collection.cursor.limit_value == PROJECT_LIST_LIMIT
    assert storage.collection.cursor.to_list_length == PROJECT_LIST_LIMIT

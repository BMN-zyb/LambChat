from __future__ import annotations

from typing import Any

import pytest

from src.kernel.config import SETTING_DEFINITIONS


class _FakeCursor:
    def __init__(self) -> None:
        self.length = None

    async def to_list(self, length=None):
        self.length = length
        return []


class _FakeCollection:
    def __init__(self) -> None:
        self.cursor = _FakeCursor()
        self.find_calls = []

    def find(self, query, projection=None):
        self.find_calls.append((query, projection))
        return self.cursor


@pytest.mark.asyncio
async def test_get_all_bounds_settings_query() -> None:
    from src.infra.settings.storage import SettingsStorage

    collection = _FakeCollection()
    storage = SettingsStorage()
    storage._collection = collection

    await storage.get_all(admin_mode=True)

    assert collection.cursor.length == len(SETTING_DEFINITIONS)
    query, projection = collection.find_calls[0]
    assert query == {"_id": {"$in": list(SETTING_DEFINITIONS.keys())}}
    assert projection == {
        "_id": 1,
        "value": 1,
        "updated_at": 1,
        "updated_by": 1,
    }


@pytest.mark.asyncio
async def test_settings_service_get_offloads_env_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.infra.settings import service as service_module
    from src.infra.settings.service import SettingsService

    inside_blocking_io = False

    class _NoDbSettingStorage:
        async def get(self, _key: str) -> None:
            return None

    async def fake_run_blocking_io(func, /, *args: Any, **kwargs: Any) -> Any:
        nonlocal inside_blocking_io
        assert inside_blocking_io is False
        inside_blocking_io = True
        try:
            return func(*args, **kwargs)
        finally:
            inside_blocking_io = False

    def fake_json_loads(value: str) -> dict[str, Any]:
        assert inside_blocking_io, "JSON environment setting parsing must be offloaded"
        assert value == '{"en":[]}'
        return {"en": []}

    monkeypatch.setenv("WELCOME_SUGGESTIONS", '{"en":[]}')
    monkeypatch.setattr(service_module, "run_blocking_io", fake_run_blocking_io)
    monkeypatch.setattr(service_module.json, "loads", fake_json_loads)

    settings_service = SettingsService()
    settings_service._storage = _NoDbSettingStorage()  # type: ignore[assignment]

    value = await settings_service.get("WELCOME_SUGGESTIONS")

    assert value == {"en": []}

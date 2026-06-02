from __future__ import annotations

import json
from typing import Any

import pytest

from src.infra.settings import service as settings_service


class _EmptySettingsStorage:
    async def get(self, key: str):
        assert key == "WELCOME_SUGGESTIONS"
        return None


@pytest.mark.asyncio
async def test_get_offloads_json_env_value_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    service = settings_service.SettingsService()
    service._storage = _EmptySettingsStorage()  # type: ignore[assignment]

    async def fake_run_blocking_io(func, /, *args: Any, **kwargs: Any):
        calls.append(getattr(func, "__name__", ""))
        return func(*args, **kwargs)

    monkeypatch.setenv("WELCOME_SUGGESTIONS", json.dumps({"en": [{"text": "hello"}]}))
    monkeypatch.setattr(settings_service, "run_blocking_io", fake_run_blocking_io)

    value = await service.get("WELCOME_SUGGESTIONS")

    assert calls == ["loads"]
    assert value == {"en": [{"text": "hello"}]}

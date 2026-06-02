from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.kernel.config import service as config_service


class _FakeSettingsStorage:
    def __init__(self, value: object) -> None:
        self._value = value

    async def get_raw(self, key: str):
        return SimpleNamespace(key=key, value=self._value)


class _FakeSettingsService:
    def __init__(self, value: object) -> None:
        self._storage = _FakeSettingsStorage(value)


@pytest.mark.asyncio
async def test_refresh_checkpoint_setting_resets_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_calls: list[str] = []

    async def _fake_reset_runtime_state() -> None:
        reset_calls.append("reset")

    monkeypatch.setattr(config_service, "_settings_service", _FakeSettingsService("postgres"))
    monkeypatch.setattr(config_service.settings, "CHECKPOINT_BACKEND", "mongodb")
    monkeypatch.setattr(
        "src.infra.storage.checkpoint.reset_checkpointer_runtime_state",
        _fake_reset_runtime_state,
    )

    await config_service.refresh_settings("CHECKPOINT_BACKEND")

    assert config_service.settings.CHECKPOINT_BACKEND == "postgres"
    assert reset_calls == ["reset"]


def test_checkpoint_settings_require_restart() -> None:
    from src.infra.settings.service import SettingsService

    checkpoint_settings = [
        "CHECKPOINT_BACKEND",
        "CHECKPOINT_PG_HOST",
        "CHECKPOINT_PG_PORT",
        "CHECKPOINT_PG_USER",
        "CHECKPOINT_PG_PASSWORD",
        "CHECKPOINT_PG_DB",
        "CHECKPOINT_PG_POOL_MIN_SIZE",
        "CHECKPOINT_PG_POOL_MAX_SIZE",
    ]

    assert all(SettingsService.requires_restart(key) for key in checkpoint_settings)

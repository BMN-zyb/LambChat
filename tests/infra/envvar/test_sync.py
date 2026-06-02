from __future__ import annotations

import pytest

from src.infra.envvar.sync import sync_envvar_change


@pytest.mark.asyncio
async def test_sync_envvar_change_invalidates_prompt_cache_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def _publish(cache: str, user_id: str | None = None) -> None:
        calls.append((cache, user_id or ""))

    monkeypatch.setattr(
        "src.infra.envvar.sync.publish_tool_cache_invalidation",
        _publish,
    )

    await sync_envvar_change("user-1")

    assert calls == [("env_var_prompt", "user-1")]


@pytest.mark.asyncio
async def test_sync_envvar_change_rebuilds_current_sandbox_when_backend_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuilt: list[tuple[object, str, bool]] = []

    async def _publish(cache: str, user_id: str | None = None) -> None:
        return None

    async def _ensure_sandbox_mcp(
        backend: object,
        user_id: str,
        *,
        force_rebuild: bool = False,
    ) -> None:
        rebuilt.append((backend, user_id, force_rebuild))

    monkeypatch.setattr(
        "src.infra.envvar.sync.publish_tool_cache_invalidation",
        _publish,
    )
    monkeypatch.setattr(
        "src.infra.envvar.sync.ensure_sandbox_mcp",
        _ensure_sandbox_mcp,
    )

    backend = object()
    await sync_envvar_change("user-1", backend=backend)

    assert rebuilt == [(backend, "user-1", True)]


@pytest.mark.asyncio
async def test_sync_envvar_change_uses_cached_session_sandbox_when_backend_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuilt: list[tuple[object, str, bool]] = []

    async def _publish(cache: str, user_id: str | None = None) -> None:
        return None

    async def _ensure_sandbox_mcp(
        backend: object,
        user_id: str,
        *,
        force_rebuild: bool = False,
    ) -> None:
        rebuilt.append((backend, user_id, force_rebuild))

    class _FakeSandboxManager:
        def get_cached_backend(self, user_id: str) -> object | None:
            return object() if user_id == "user-1" else None

    monkeypatch.setattr(
        "src.infra.envvar.sync.publish_tool_cache_invalidation",
        _publish,
    )
    monkeypatch.setattr(
        "src.infra.envvar.sync.ensure_sandbox_mcp",
        _ensure_sandbox_mcp,
    )
    monkeypatch.setattr(
        "src.infra.envvar.sync.get_session_sandbox_manager",
        lambda: _FakeSandboxManager(),
    )

    await sync_envvar_change("user-1")

    assert len(rebuilt) == 1
    assert rebuilt[0][1] == "user-1"
    assert rebuilt[0][2] is True

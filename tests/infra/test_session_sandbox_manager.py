from __future__ import annotations

from collections import OrderedDict

import pytest

from src.infra.sandbox import session_manager as sandbox_module


class _FakeE2BAdapter:
    def __init__(self) -> None:
        self.method_calls: list[str] = []

    def sandbox_is_running(self, _provider_obj) -> bool:
        self.method_calls.append("sandbox_is_running")
        return True

    def extend_timeout(self, _provider_obj, _timeout: int) -> None:
        self.method_calls.append("extend_timeout")

    def get_work_dir(self, _provider_obj) -> str:
        self.method_calls.append("get_work_dir")
        return "/home/user"


@pytest.mark.asyncio
async def test_e2b_cache_hit_runs_sync_sdk_calls_in_blocking_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeE2BAdapter()
    manager = sandbox_module.SessionSandboxManager()
    manager._e2b_adapter = adapter
    manager._cache = OrderedDict({"user-1": ("sandbox-1", object(), object())})

    blocking_calls: list[str] = []

    async def fake_run_blocking_io(func, *args, **kwargs):
        del kwargs
        blocking_calls.append(func.__name__)
        return func(*args)

    async def fake_save_binding(*_args, **_kwargs) -> None:
        return None

    async def fake_ensure_sandbox_mcp(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sandbox_module, "run_blocking_io", fake_run_blocking_io)
    monkeypatch.setattr(manager, "_save_binding", fake_save_binding)
    monkeypatch.setattr(sandbox_module, "ensure_sandbox_mcp", fake_ensure_sandbox_mcp)
    monkeypatch.setattr(sandbox_module.settings, "E2B_TIMEOUT", 123)

    _backend, work_dir = await manager._get_or_create_e2b("session-1", "user-1")

    assert work_dir == "/home/user"
    assert blocking_calls == ["sandbox_is_running", "extend_timeout", "get_work_dir"]
    assert adapter.method_calls == ["sandbox_is_running", "extend_timeout", "get_work_dir"]

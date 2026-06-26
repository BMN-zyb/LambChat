from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

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


class _FakeMongoClient:
    def __init__(self, collection: Any) -> None:
        self._collection = collection

    def __getitem__(self, name: str):
        if name == sandbox_module.BINDING_COLLECTION:
            return self._collection
        return self


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

    async def fake_ensure_work_dir(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sandbox_module, "run_blocking_io", fake_run_blocking_io)
    monkeypatch.setattr(manager, "_save_binding", fake_save_binding)
    monkeypatch.setattr(manager, "_ensure_work_dir", fake_ensure_work_dir)
    monkeypatch.setattr(sandbox_module, "ensure_sandbox_mcp", fake_ensure_sandbox_mcp)
    monkeypatch.setattr(sandbox_module.settings, "E2B_TIMEOUT", 123)

    _backend, work_dir = await manager._get_or_create_e2b("session-1", "user-1")

    assert work_dir == "/home/user/sessions/session-1"
    assert blocking_calls == ["sandbox_is_running", "extend_timeout", "get_work_dir"]
    assert adapter.method_calls == ["sandbox_is_running", "extend_timeout", "get_work_dir"]


def test_session_work_dir_uses_safe_session_specific_subdirectory() -> None:
    manager = sandbox_module.SessionSandboxManager()

    work_dir = manager._session_work_dir("/home/user", "../session with spaces/中文")

    assert work_dir == "/home/user/sessions/session-with-spaces"


@pytest.mark.asyncio
async def test_close_session_sandbox_manager_does_not_create_unused_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_module._session_sandbox_manager = None

    def _raise_if_created() -> None:
        raise AssertionError("unused sandbox manager should not be created during close")

    monkeypatch.setattr(sandbox_module, "SessionSandboxManager", _raise_if_created)

    await sandbox_module.close_session_sandbox_manager()

    assert sandbox_module._session_sandbox_manager is None


@pytest.mark.asyncio
async def test_close_session_sandbox_manager_closes_existing_singleton() -> None:
    class _Manager:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close_all(self) -> None:
            self.close_calls += 1

    manager = _Manager()
    sandbox_module._session_sandbox_manager = manager

    await sandbox_module.close_session_sandbox_manager()

    assert manager.close_calls == 1
    assert sandbox_module._session_sandbox_manager is None


@pytest.mark.asyncio
async def test_close_all_clears_collection_reference() -> None:
    manager = sandbox_module.SessionSandboxManager()
    manager._collection = object()

    await manager.close_all()

    assert manager._collection is None


@pytest.mark.asyncio
async def test_bindings_reuses_inflight_index_task_across_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowIndexCollection:
        def __init__(self) -> None:
            self.create_index_calls = 0

        async def create_index(self, *_args, **_kwargs) -> None:
            self.create_index_calls += 1
            started.set()
            await release.wait()

    collection = _SlowIndexCollection()
    monkeypatch.setattr(
        "src.infra.storage.mongodb.get_mongo_client",
        lambda: _FakeMongoClient(collection),
    )
    monkeypatch.setattr(sandbox_module.SessionSandboxManager, "_index_task", None, raising=False)
    monkeypatch.setattr(
        sandbox_module.SessionSandboxManager,
        "_index_ensured",
        False,
        raising=False,
    )

    first = sandbox_module.SessionSandboxManager()
    second = sandbox_module.SessionSandboxManager()

    first._bindings
    await asyncio.wait_for(started.wait(), timeout=1)
    second._bindings
    await asyncio.sleep(0)

    release.set()
    task = sandbox_module.SessionSandboxManager._index_task
    if task is not None:
        await task

    assert collection.create_index_calls == 1

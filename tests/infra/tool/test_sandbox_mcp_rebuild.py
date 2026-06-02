from __future__ import annotations

import asyncio
import json

import pytest

from src.infra.tool import sandbox_mcp_rebuild


class _FakeBackend:
    def __init__(self) -> None:
        self.default = type("_Sandbox", (), {"id": "sandbox-1", "env_vars": {}})()


@pytest.mark.asyncio
async def test_get_mcporter_server_names_offloads_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def fake_run_blocking_io(func, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    class _Backend:
        async def aexecute(self, command: str, timeout: int):
            assert command == "mcporter list --json"
            assert timeout == 15
            return type(
                "_Result",
                (),
                {"exit_code": 0, "output": '{"servers":[{"name":"alpha"},{"name":"beta"}]}'},
            )()

    monkeypatch.setattr(sandbox_mcp_rebuild, "run_blocking_io", fake_run_blocking_io)

    names = await sandbox_mcp_rebuild._get_mcporter_server_names(_Backend())

    assert calls == [json.loads]
    assert names == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_ensure_sandbox_mcp_skips_recent_rebuild_but_still_syncs_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    rebuild_calls: list[str] = []
    sync_calls: list[str] = []

    async def fake_rebuild(_backend: object, user_id: str) -> None:
        rebuild_calls.append(user_id)

    async def fake_sync(_backend: object, user_id: str) -> None:
        sync_calls.append(user_id)

    async def fake_acquire_lock(_cache_key: str) -> bool:
        return True

    monkeypatch.setattr(sandbox_mcp_rebuild, "rebuild_sandbox_mcp", fake_rebuild)
    monkeypatch.setattr(sandbox_mcp_rebuild, "_sync_user_env_vars", fake_sync)
    monkeypatch.setattr(
        sandbox_mcp_rebuild,
        "_acquire_distributed_rebuild_lock",
        fake_acquire_lock,
    )
    monkeypatch.setattr(
        "src.infra.tool.sandbox_mcp_prompt.invalidate_sandbox_mcp_prompt_cache",
        lambda _user_id: None,
    )
    sandbox_mcp_rebuild.clear_sandbox_mcp_rebuild_cache()

    await sandbox_mcp_rebuild.ensure_sandbox_mcp(backend, "user-1")
    await sandbox_mcp_rebuild.ensure_sandbox_mcp(backend, "user-1")

    assert rebuild_calls == ["user-1"]
    assert sync_calls == ["user-1", "user-1"]


@pytest.mark.asyncio
async def test_ensure_sandbox_mcp_force_rebuild_bypasses_recent_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    rebuild_calls: list[str] = []

    async def fake_rebuild(_backend: object, user_id: str) -> None:
        rebuild_calls.append(user_id)

    async def fake_sync(_backend: object, _user_id: str) -> None:
        return None

    async def fake_acquire_lock(_cache_key: str) -> bool:
        return True

    monkeypatch.setattr(sandbox_mcp_rebuild, "rebuild_sandbox_mcp", fake_rebuild)
    monkeypatch.setattr(sandbox_mcp_rebuild, "_sync_user_env_vars", fake_sync)
    monkeypatch.setattr(
        sandbox_mcp_rebuild,
        "_acquire_distributed_rebuild_lock",
        fake_acquire_lock,
    )
    monkeypatch.setattr(
        "src.infra.tool.sandbox_mcp_prompt.invalidate_sandbox_mcp_prompt_cache",
        lambda _user_id: None,
    )
    sandbox_mcp_rebuild.clear_sandbox_mcp_rebuild_cache()

    await sandbox_mcp_rebuild.ensure_sandbox_mcp(backend, "user-1")
    await sandbox_mcp_rebuild.ensure_sandbox_mcp(backend, "user-1", force_rebuild=True)

    assert rebuild_calls == ["user-1", "user-1"]


@pytest.mark.asyncio
async def test_ensure_sandbox_mcp_force_rebuild_respects_distributed_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    rebuild_calls: list[str] = []
    sync_calls: list[str] = []

    async def fake_rebuild(_backend: object, user_id: str) -> None:
        rebuild_calls.append(user_id)

    async def fake_sync(_backend: object, user_id: str) -> None:
        sync_calls.append(user_id)

    async def fake_acquire_lock(_cache_key: str) -> bool:
        return False

    monkeypatch.setattr(sandbox_mcp_rebuild, "rebuild_sandbox_mcp", fake_rebuild)
    monkeypatch.setattr(sandbox_mcp_rebuild, "_sync_user_env_vars", fake_sync)
    monkeypatch.setattr(
        sandbox_mcp_rebuild,
        "_acquire_distributed_rebuild_lock",
        fake_acquire_lock,
    )
    monkeypatch.setattr(
        "src.infra.tool.sandbox_mcp_prompt.invalidate_sandbox_mcp_prompt_cache",
        lambda _user_id: None,
    )
    sandbox_mcp_rebuild.clear_sandbox_mcp_rebuild_cache()

    await sandbox_mcp_rebuild.ensure_sandbox_mcp(backend, "user-1", force_rebuild=True)

    assert rebuild_calls == []
    assert sync_calls == ["user-1"]


@pytest.mark.asyncio
async def test_ensure_sandbox_mcp_coalesces_concurrent_force_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    rebuild_calls: list[str] = []
    sync_calls: list[str] = []
    rebuild_started = asyncio.Event()
    allow_rebuild_finish = asyncio.Event()

    async def fake_rebuild(_backend: object, user_id: str) -> None:
        rebuild_calls.append(user_id)
        rebuild_started.set()
        await allow_rebuild_finish.wait()

    async def fake_sync(_backend: object, user_id: str) -> None:
        sync_calls.append(user_id)

    async def fake_acquire_lock(_cache_key: str) -> bool:
        return True

    monkeypatch.setattr(sandbox_mcp_rebuild, "rebuild_sandbox_mcp", fake_rebuild)
    monkeypatch.setattr(sandbox_mcp_rebuild, "_sync_user_env_vars", fake_sync)
    monkeypatch.setattr(
        sandbox_mcp_rebuild,
        "_acquire_distributed_rebuild_lock",
        fake_acquire_lock,
    )
    monkeypatch.setattr(
        "src.infra.tool.sandbox_mcp_prompt.invalidate_sandbox_mcp_prompt_cache",
        lambda _user_id: None,
    )
    sandbox_mcp_rebuild.clear_sandbox_mcp_rebuild_cache()

    first = asyncio.create_task(
        sandbox_mcp_rebuild.ensure_sandbox_mcp(backend, "user-1", force_rebuild=True)
    )
    await asyncio.wait_for(rebuild_started.wait(), timeout=1)
    second = asyncio.create_task(
        sandbox_mcp_rebuild.ensure_sandbox_mcp(backend, "user-1", force_rebuild=True)
    )
    await asyncio.sleep(0)

    assert rebuild_calls == ["user-1"]

    allow_rebuild_finish.set()
    await asyncio.gather(first, second)

    assert rebuild_calls == ["user-1"]
    assert sync_calls == ["user-1", "user-1"]


@pytest.mark.asyncio
async def test_ensure_sandbox_mcp_skips_rebuild_when_distributed_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    rebuild_calls: list[str] = []
    sync_calls: list[str] = []

    class _FakeRedis:
        async def set(self, *args, **kwargs):
            assert kwargs["nx"] is True
            assert kwargs["ex"] == sandbox_mcp_rebuild._REBUILD_LOCK_TTL_SECONDS
            return False

    async def fake_rebuild(_backend: object, user_id: str) -> None:
        rebuild_calls.append(user_id)

    async def fake_sync(_backend: object, user_id: str) -> None:
        sync_calls.append(user_id)

    monkeypatch.setattr(sandbox_mcp_rebuild, "rebuild_sandbox_mcp", fake_rebuild)
    monkeypatch.setattr(sandbox_mcp_rebuild, "_sync_user_env_vars", fake_sync)
    monkeypatch.setattr(
        "src.infra.storage.redis.get_redis_client",
        lambda: _FakeRedis(),
    )
    monkeypatch.setattr(
        "src.infra.tool.sandbox_mcp_prompt.invalidate_sandbox_mcp_prompt_cache",
        lambda _user_id: None,
    )
    sandbox_mcp_rebuild.clear_sandbox_mcp_rebuild_cache()

    await sandbox_mcp_rebuild.ensure_sandbox_mcp(backend, "user-1")

    assert rebuild_calls == []
    assert sync_calls == ["user-1"]


@pytest.mark.asyncio
async def test_ensure_sandbox_mcp_bounds_recent_rebuild_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild_calls: list[str] = []

    class _Backend:
        def __init__(self, sandbox_id: str) -> None:
            self.default = type("_Sandbox", (), {"id": sandbox_id, "env_vars": {}})()

    async def fake_rebuild(_backend: object, user_id: str) -> None:
        rebuild_calls.append(user_id)

    async def fake_sync(_backend: object, _user_id: str) -> None:
        return None

    async def fake_acquire_lock(_cache_key: str) -> bool:
        return True

    monkeypatch.setattr(
        sandbox_mcp_rebuild,
        "_REBUILD_CACHE_MAX_ENTRIES",
        2,
        raising=False,
    )
    monkeypatch.setattr(sandbox_mcp_rebuild, "rebuild_sandbox_mcp", fake_rebuild)
    monkeypatch.setattr(sandbox_mcp_rebuild, "_sync_user_env_vars", fake_sync)
    monkeypatch.setattr(
        sandbox_mcp_rebuild,
        "_acquire_distributed_rebuild_lock",
        fake_acquire_lock,
    )
    monkeypatch.setattr(
        "src.infra.tool.sandbox_mcp_prompt.invalidate_sandbox_mcp_prompt_cache",
        lambda _user_id: None,
    )
    sandbox_mcp_rebuild.clear_sandbox_mcp_rebuild_cache()

    for index in range(4):
        await sandbox_mcp_rebuild.ensure_sandbox_mcp(
            _Backend(f"sandbox-{index}"),
            f"user-{index}",
        )

    assert len(sandbox_mcp_rebuild._recent_rebuilds) == 2
    assert list(sandbox_mcp_rebuild._recent_rebuilds) == [
        "user-2:sandbox-2",
        "user-3:sandbox-3",
    ]


@pytest.mark.asyncio
async def test_rebuild_sandbox_mcp_registers_servers_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_started: list[str] = []
    first_add_saw_second_start = False
    first_add_can_finish = asyncio.Event()

    class _Backend:
        async def aexecute(self, command: str, timeout: int):
            if command == "mcporter --version":
                return type("_Result", (), {"exit_code": 0, "output": "mcporter 1.0"})()
            if command == "mcporter list --json":
                return type("_Result", (), {"exit_code": 0, "output": '{"servers":[]}'})()
            if command.startswith("mcporter config remove"):
                return type("_Result", (), {"exit_code": 0, "output": ""})()
            if command.startswith("mcporter config add"):
                nonlocal first_add_saw_second_start
                add_started.append(command)
                if len(add_started) == 1:
                    try:
                        await asyncio.wait_for(first_add_can_finish.wait(), timeout=0.05)
                        first_add_saw_second_start = True
                    except asyncio.TimeoutError:
                        pass
                else:
                    first_add_can_finish.set()
                return type("_Result", (), {"exit_code": 0, "output": ""})()
            if command.startswith("env "):
                return type("_Result", (), {"exit_code": 0, "output": ""})()
            raise AssertionError(f"unexpected command: {command}")

    class _FakeMCPStorage:
        async def get_sandbox_servers(self, *args, **kwargs):
            return [
                {"name": "one", "command": "cmd-one", "env_keys": []},
                {"name": "two", "command": "cmd-two", "env_keys": []},
            ]

    class _FakeEnvVarStorage:
        async def get_decrypted_vars(self, user_id: str):
            return {}

    async def fake_resolve_user_mcp_access(user_id: str):
        return [], False

    async def fake_build_env_flags(user_id: str, env_keys: list[str]):
        return ""

    monkeypatch.setattr("src.infra.mcp.storage.MCPStorage", _FakeMCPStorage)
    monkeypatch.setattr("src.infra.envvar.storage.EnvVarStorage", _FakeEnvVarStorage)
    monkeypatch.setattr(
        "src.infra.mcp.quota.resolve_user_mcp_access",
        fake_resolve_user_mcp_access,
    )
    monkeypatch.setattr(
        "src.infra.tool.sandbox_mcp_utils.build_env_flags",
        fake_build_env_flags,
    )

    await sandbox_mcp_rebuild.rebuild_sandbox_mcp(_Backend(), "user-1")

    assert len(add_started) == 2
    assert first_add_saw_second_start is True

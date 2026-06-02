from __future__ import annotations

import pytest

from src.infra.sandbox.base import SandboxFactory


@pytest.fixture(autouse=True)
def _clear_sandbox_factory_registry() -> None:
    SandboxFactory._sandbox_registry.clear()
    SandboxFactory._run_id_to_sandbox.clear()


@pytest.mark.asyncio
async def test_close_sandbox_offloads_provider_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inside_blocking_io = False

    class _DaytonaProvider:
        __module__ = "daytona.fake"

        def __init__(self) -> None:
            self.deleted = False

        def delete(self) -> None:
            assert inside_blocking_io, "provider delete must be offloaded"
            self.deleted = True

    provider = _DaytonaProvider()
    SandboxFactory._sandbox_registry["sandbox-1"] = (object(), provider)

    async def _fake_run_blocking_io(func, /, *args, **kwargs):
        nonlocal inside_blocking_io
        assert inside_blocking_io is False
        inside_blocking_io = True
        try:
            return func(*args, **kwargs)
        finally:
            inside_blocking_io = False

    monkeypatch.setattr(
        "src.infra.sandbox.base.run_blocking_io",
        _fake_run_blocking_io,
        raising=False,
    )

    closed = await SandboxFactory.close_sandbox("sandbox-1")

    assert closed is True
    assert provider.deleted is True
    assert "sandbox-1" not in SandboxFactory._sandbox_registry

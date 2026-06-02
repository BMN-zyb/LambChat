from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.infra.tool import sandbox_mcp_tool


class _Runtime:
    def __init__(self, backend: object) -> None:
        self.config = {
            "configurable": {"backend": backend, "context": SimpleNamespace(user_id="u1")}
        }


@pytest.mark.asyncio
async def test_sandbox_mcp_add_truncates_large_mcporter_errors() -> None:
    class _Backend:
        async def aexecute(self, command: str, timeout: int):
            return SimpleNamespace(exit_code=1, output="x" * 100_000)

    result = json.loads(
        await sandbox_mcp_tool.sandbox_mcp_add.coroutine(
            server_name="bad",
            command="broken command",
            runtime=_Runtime(_Backend()),
        )
    )

    assert len(result["error"]) < 20_000
    assert "truncated" in result["error"]


@pytest.mark.asyncio
async def test_sandbox_mcp_add_offloads_result_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class _Backend:
        async def aexecute(self, command: str, timeout: int):
            return SimpleNamespace(exit_code=1, output="x" * 100_000)

    async def fake_run_blocking_io(func, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(sandbox_mcp_tool, "run_blocking_io", fake_run_blocking_io, raising=False)

    result = json.loads(
        await sandbox_mcp_tool.sandbox_mcp_add.coroutine(
            server_name="bad",
            command="broken command",
            runtime=_Runtime(_Backend()),
        )
    )

    assert "truncated" in result["error"]
    assert json.dumps in calls

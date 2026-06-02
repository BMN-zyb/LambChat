from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from src.infra.agent import middleware_subagent
from src.infra.agent.middleware_subagent import SubagentActivityMiddleware


def test_subagent_activity_log_drops_old_entries_when_memory_cap_is_exceeded() -> None:
    middleware = SubagentActivityMiddleware(backend=object())
    middleware._max_log_chars = 120

    for index in range(10):
        middleware._append_entry(f"entry-{index}-" + ("x" * 30))

    rendered = middleware._render_log()

    assert middleware._total_chars <= middleware._max_log_chars
    assert "older activity entries omitted" in rendered
    assert "entry-0-" not in rendered
    assert "entry-9-" in rendered


@pytest.mark.asyncio
async def test_subagent_tool_result_serialization_is_offloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    async def _fake_run_blocking_io(func, /, *args: Any, **kwargs: Any):
        calls.append(func)
        return func(*args, **kwargs)

    class _Backend:
        async def awrite(self, _path: str, _content: str):
            return SimpleNamespace(error=None)

    monkeypatch.setattr(
        middleware_subagent,
        "run_blocking_io",
        _fake_run_blocking_io,
        raising=False,
    )

    middleware = SubagentActivityMiddleware(backend=_Backend())
    request = SimpleNamespace(
        runtime=object(),
        tool_call={"name": "large_tool", "args": {}},
    )

    async def _handler(_request: Any) -> dict[str, Any]:
        return {"items": ["x" * 20_000]}

    result = await middleware.awrap_tool_call(request, _handler)

    assert result == {"items": ["x" * 20_000]}
    assert calls == [json.dumps]

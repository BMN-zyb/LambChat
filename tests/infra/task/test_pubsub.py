from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from src.infra.task import pubsub as task_pubsub_module
from src.infra.task.pubsub import TaskPubSub


@pytest.mark.asyncio
async def test_cancel_pubsub_offloads_message_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    handled: list[dict[str, Any]] = []

    async def _fake_run_blocking_io(func, /, *args: Any, **kwargs: Any):
        calls.append(func)
        return func(*args, **kwargs)

    async def _on_message(data: dict[str, Any]) -> None:
        handled.append(data)

    monkeypatch.setattr(
        task_pubsub_module,
        "run_blocking_io",
        _fake_run_blocking_io,
        raising=False,
    )

    pubsub = TaskPubSub(asyncio.Lock(), {})

    await pubsub._handle_cancel_message(
        {"data": json.dumps({"run_id": "run-1"})},
        _on_message,
    )

    assert calls == [json.loads]
    assert handled == [{"run_id": "run-1"}]

"""Tests for scheduled task execution status handling."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.infra.scheduler.runner import ScheduledTaskRunner
from src.infra.task.status import TaskStatus
from src.kernel.schemas.scheduled_task import (
    RunStatus,
    ScheduledTask,
    ScheduledTaskStatus,
    TriggerType,
)


def _make_task(**overrides: Any) -> ScheduledTask:
    defaults = dict(
        _id="task_1",
        name="Test Task",
        agent_id="agent_1",
        trigger_type=TriggerType.INTERVAL,
        trigger_config={"seconds": 300},
        input_payload={"message": "hello"},
        status=ScheduledTaskStatus.ACTIVE,
        enabled=True,
        owner_id="user_1",
        timeout_seconds=60,
        max_retries=0,
    )
    defaults.update(overrides)
    return ScheduledTask(**defaults)


@pytest.fixture
def mock_storage():
    with patch("src.infra.scheduler.runner.get_scheduled_task_storage") as mock:
        storage = AsyncMock()
        mock.return_value = storage
        yield storage


@pytest.fixture
def mock_lock():
    with (
        patch(
            "src.infra.scheduler.runner.acquire_task_lock",
            new=AsyncMock(return_value="token"),
        ),
        patch("src.infra.scheduler.runner.release_task_lock", new=AsyncMock()),
    ):
        yield


@pytest.mark.asyncio
async def test_runner_lock_ttl_covers_all_attempts(
    mock_storage: AsyncMock,
) -> None:
    task = _make_task(timeout_seconds=60, max_retries=2)
    mock_storage.get_task = AsyncMock(return_value=task)

    with (
        patch(
            "src.infra.scheduler.runner.acquire_task_lock",
            new=AsyncMock(return_value="token"),
        ) as acquire_lock,
        patch("src.infra.scheduler.runner.release_task_lock", new=AsyncMock()),
    ):
        runner = ScheduledTaskRunner()
        runner._execute_agent = AsyncMock(  # type: ignore[method-assign]
            return_value={"session_status": "completed", "session_id": "session_1"}
        )

        await runner.run("task_1")

    acquire_lock.assert_awaited_once()
    assert acquire_lock.call_args.kwargs["ttl"] >= 180


@pytest.mark.asyncio
async def test_runner_records_failed_agent_status_as_failed(
    mock_storage: AsyncMock,
    mock_lock: None,
) -> None:
    task = _make_task()
    mock_storage.get_task = AsyncMock(return_value=task)
    runner = ScheduledTaskRunner()
    runner._execute_agent = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "session_status": "failed",
            "session_id": "session_1",
            "trace_id": "trace_1",
        }
    )

    result = await runner.run("task_1")

    assert result["status"] == RunStatus.FAILED.value
    final_update = mock_storage.update_run.call_args_list[-1].args[1]
    assert final_update["status"] == RunStatus.FAILED
    assert final_update["error_message"] == "Agent run ended with status: failed"
    mock_storage.update_task_run_stats.assert_awaited_once()
    assert mock_storage.update_task_run_stats.call_args.args[2] == RunStatus.FAILED


@pytest.mark.asyncio
async def test_runner_retries_until_success(
    mock_storage: AsyncMock,
    mock_lock: None,
) -> None:
    task = _make_task(max_retries=1)
    mock_storage.get_task = AsyncMock(return_value=task)
    runner = ScheduledTaskRunner()
    runner._execute_agent = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"session_status": "failed", "session_id": "session_1"},
            {
                "session_status": "completed",
                "session_id": "session_2",
                "trace_id": "trace_2",
            },
        ]
    )

    result = await runner.run("task_1")

    assert result["status"] == RunStatus.SUCCESS.value
    assert runner._execute_agent.call_count == 2
    retry_updates = [
        call.args[1]["retry_count"]
        for call in mock_storage.update_run.call_args_list
        if "retry_count" in call.args[1]
    ]
    assert retry_updates == [0, 1]
    final_update = mock_storage.update_run.call_args_list[-1].args[1]
    assert final_update["status"] == RunStatus.SUCCESS


@pytest.mark.asyncio
async def test_runner_does_not_retry_timeout(
    mock_storage: AsyncMock,
    mock_lock: None,
) -> None:
    task = _make_task(max_retries=1)
    mock_storage.get_task = AsyncMock(return_value=task)
    runner = ScheduledTaskRunner()
    runner._execute_agent = AsyncMock(  # type: ignore[method-assign]
        return_value={"session_status": "timeout", "session_id": "session_1"}
    )

    result = await runner.run("task_1")

    assert result["status"] == RunStatus.TIMEOUT.value
    assert runner._execute_agent.call_count == 1
    final_update = mock_storage.update_run.call_args_list[-1].args[1]
    assert final_update["status"] == RunStatus.TIMEOUT


@pytest.mark.asyncio
async def test_wait_for_completion_times_out_and_cancels_run() -> None:
    manager = AsyncMock()
    manager.get_run_status = AsyncMock(return_value=TaskStatus.RUNNING)
    manager.cancel_run = AsyncMock(return_value={"success": True})
    runner = ScheduledTaskRunner()

    result = await runner._wait_for_completion(
        manager,
        session_id="session_1",
        run_id="run_1",
        user_id="user_1",
        timeout_seconds=0,
    )

    assert result == {"session_status": "timeout"}
    manager.cancel_run.assert_awaited_once_with("run_1", user_id="user_1")


@pytest.mark.asyncio
async def test_execute_agent_hides_injected_timestamp_from_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _make_task(
        input_payload={
            "message": "Summarize the latest AI news",
            "user_timezone": "Asia/Shanghai",
        }
    )
    submitted: dict[str, Any] = {}

    class _FakeTaskManager:
        async def submit(self, **kwargs: Any) -> tuple[str, str]:
            submitted.update(kwargs)
            return "run_1", "trace_1"

        async def get_run_status(self, session_id: str, run_id: str) -> TaskStatus:
            assert session_id == "session_1"
            assert run_id == "run_1"
            return TaskStatus.COMPLETED

    class _FakeSessionManager:
        def __init__(self) -> None:
            self.metadata: dict[str, Any] | None = None

        async def update_session_metadata(
            self,
            session_id: str,
            metadata: dict[str, Any],
        ) -> None:
            assert session_id == "session_1"
            self.metadata = metadata

    session_manager = _FakeSessionManager()

    monkeypatch.setattr(
        "src.infra.task.manager.get_task_manager",
        lambda: _FakeTaskManager(),
    )
    monkeypatch.setattr("src.kernel.config.settings.TASK_BACKEND", "local")
    monkeypatch.setattr(
        "src.infra.task.concurrency.get_registered_executor",
        lambda key: (lambda *args, **kwargs: None) if key == "agent_stream" else None,
    )
    monkeypatch.setattr(
        "src.infra.session.manager.SessionManager",
        lambda: session_manager,
    )

    result = await ScheduledTaskRunner()._execute_agent(
        task,
        run_id="run_1",
        session_id="session_1",
    )

    assert result == {
        "session_status": "completed",
        "session_id": "session_1",
        "trace_id": "trace_1",
    }
    assert submitted["message"].startswith("[User message sent at: ")
    assert " +08:00 Asia/Shanghai] Summarize the latest AI news" in submitted["message"]
    assert submitted["display_message"] == "Summarize the latest AI news"
    assert submitted["recommendation_input"] == "Summarize the latest AI news"
    assert "[User message sent at:" not in submitted["display_message"]
    assert "[User message sent at:" not in submitted["recommendation_input"]
    assert submitted["write_user_message_immediately"] is True
    assert session_manager.metadata == {
        "source": "scheduled_task",
        "scheduled_task_id": "task_1",
        "scheduled_task_run_id": "run_1",
        "hidden_from_conversation_list": True,
    }


@pytest.mark.asyncio
async def test_execute_agent_uses_arq_backend_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _make_task(input_payload={"message": "Run distributed report"})
    submitted: dict[str, Any] = {}

    class _FakeTaskManager:
        async def submit(self, **kwargs: Any) -> tuple[str, str]:
            raise AssertionError("scheduled task should not use local submit with arq")

        async def submit_arq(self, **kwargs: Any) -> tuple[str, str]:
            submitted.update(kwargs)
            return "run_1", "trace_1"

        async def get_run_status(self, session_id: str, run_id: str) -> TaskStatus:
            assert session_id == "session_1"
            assert run_id == "run_1"
            return TaskStatus.COMPLETED

    class _FakeSessionManager:
        async def update_session_metadata(
            self,
            session_id: str,
            metadata: dict[str, Any],
        ) -> None:
            assert session_id == "session_1"
            assert metadata["source"] == "scheduled_task"

    monkeypatch.setattr("src.kernel.config.settings.TASK_BACKEND", "arq")
    monkeypatch.setattr(
        "src.infra.task.manager.get_task_manager",
        lambda: _FakeTaskManager(),
    )
    monkeypatch.setattr(
        "src.infra.task.concurrency.get_registered_executor",
        lambda key: (lambda *args, **kwargs: None) if key == "agent_stream" else None,
    )
    monkeypatch.setattr(
        "src.infra.session.manager.SessionManager",
        lambda: _FakeSessionManager(),
    )

    result = await ScheduledTaskRunner()._execute_agent(
        task,
        run_id="run_1",
        session_id="session_1",
    )

    assert result == {
        "session_status": "completed",
        "session_id": "session_1",
        "trace_id": "trace_1",
    }
    assert submitted["executor_key"] == "agent_stream"
    assert submitted["run_id"] == "run_1"
    assert submitted["session_id"] == "session_1"
    assert submitted["display_message"] == "Run distributed report"
    assert submitted["write_user_message_immediately"] is True

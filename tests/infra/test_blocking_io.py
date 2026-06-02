from __future__ import annotations

import asyncio
import threading

import pytest

from src.infra.async_utils.blocking import run_blocking_io


def test_blocking_io_default_worker_count_is_bounded_for_api_process_memory() -> None:
    import src.infra.async_utils.blocking as blocking

    assert blocking._DEFAULT_MAX_WORKERS == 8


@pytest.mark.asyncio
async def test_run_blocking_io_runs_callable_away_from_event_loop_thread() -> None:
    loop_thread = threading.get_ident()

    def blocking_call(value: str, *, suffix: str) -> tuple[str, int]:
        return f"{value}{suffix}", threading.get_ident()

    result, worker_thread = await run_blocking_io(blocking_call, "ok", suffix="-done")

    assert result == "ok-done"
    assert worker_thread != loop_thread


@pytest.mark.asyncio
async def test_run_blocking_io_applies_timeout() -> None:
    def slow_call() -> None:
        import time

        time.sleep(0.2)

    with pytest.raises(asyncio.TimeoutError):
        await run_blocking_io(slow_call, timeout=0.01)


@pytest.mark.asyncio
async def test_run_blocking_io_keeps_slot_until_timed_out_call_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    import src.infra.async_utils.blocking as blocking

    class _RecordingExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=1, thread_name_prefix="test-blocking-io")
            self.submitted_names: list[str] = []

        def submit(self, fn, /, *args, **kwargs):
            self.submitted_names.append(getattr(getattr(fn, "func", fn), "__name__", "unknown"))
            return super().submit(fn, *args, **kwargs)

    executor = _RecordingExecutor()
    monkeypatch.setattr(blocking, "_BLOCKING_IO_EXECUTOR", executor)

    def slow_call() -> None:
        import time

        time.sleep(0.1)

    with pytest.raises(asyncio.TimeoutError):
        await blocking.run_blocking_io(slow_call, timeout=0.01)

    with pytest.raises(asyncio.TimeoutError):
        await blocking.run_blocking_io(lambda: "second", timeout=0.02)

    await asyncio.sleep(0.12)

    assert await blocking.run_blocking_io(lambda: "released", timeout=0.05) == "released"
    executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_run_blocking_io_applies_pending_submission_backpressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    import src.infra.async_utils.blocking as blocking

    class _RecordingExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=1, thread_name_prefix="test-blocking-io")
            self.submitted_names: list[str] = []

        def submit(self, fn, /, *args, **kwargs):
            self.submitted_names.append(getattr(getattr(fn, "func", fn), "__name__", "unknown"))
            return super().submit(fn, *args, **kwargs)

    executor = _RecordingExecutor()
    monkeypatch.setattr(blocking, "_BLOCKING_IO_EXECUTOR", executor)
    monkeypatch.setattr(blocking, "_MAX_PENDING_BLOCKING_IO", 1)
    monkeypatch.setattr(blocking, "_LOOP_LIMITERS", {})

    started = threading.Event()
    release = threading.Event()
    submitted: list[str] = []

    def slow_call() -> str:
        submitted.append("slow")
        started.set()
        release.wait(timeout=1)
        return "slow"

    def queued_call() -> str:
        submitted.append("queued")
        return "queued"

    def backpressured_call() -> str:
        submitted.append("backpressured")
        return "backpressured"

    slow_task = asyncio.create_task(blocking.run_blocking_io(slow_call))
    await asyncio.to_thread(started.wait, 1)

    queued_task = asyncio.create_task(blocking.run_blocking_io(queued_call))
    await asyncio.sleep(0.02)

    backpressured_task = asyncio.create_task(blocking.run_blocking_io(backpressured_call))
    await asyncio.sleep(0.02)

    try:
        assert executor.submitted_names == ["slow_call", "queued_call"]
        assert submitted == ["slow"]
        assert backpressured_task.done() is False

        release.set()
        assert await slow_task == "slow"
        assert await queued_task == "queued"
        assert await backpressured_task == "backpressured"
        assert executor.submitted_names == [
            "slow_call",
            "queued_call",
            "backpressured_call",
        ]
    finally:
        release.set()
        for task in (slow_task, queued_task, backpressured_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(slow_task, queued_task, backpressured_task, return_exceptions=True)
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_run_blocking_io_timeout_covers_pending_backpressure_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    import src.infra.async_utils.blocking as blocking

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-blocking-io")
    monkeypatch.setattr(blocking, "_BLOCKING_IO_EXECUTOR", executor)
    monkeypatch.setattr(blocking, "_MAX_PENDING_BLOCKING_IO", 0)
    monkeypatch.setattr(blocking, "_LOOP_LIMITERS", {})

    started = threading.Event()
    release = threading.Event()
    submitted: list[str] = []

    def slow_call() -> str:
        submitted.append("slow")
        started.set()
        release.wait(timeout=1)
        return "slow"

    def should_not_submit() -> str:
        submitted.append("blocked")
        return "blocked"

    slow_task = asyncio.create_task(blocking.run_blocking_io(slow_call))
    await asyncio.to_thread(started.wait, 1)

    try:
        with pytest.raises(asyncio.TimeoutError):
            await blocking.run_blocking_io(should_not_submit, timeout=0.01)
        assert submitted == ["slow"]
    finally:
        release.set()
        assert await slow_task == "slow"
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_run_blocking_io_still_completes_after_settings_import() -> None:
    from src.kernel.config import settings

    assert settings is not None

    result = await run_blocking_io(lambda: "ok", timeout=1.0)

    assert result == "ok"

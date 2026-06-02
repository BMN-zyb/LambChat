from __future__ import annotations

import asyncio

from src.infra.async_utils.background_tasks import BestEffortTaskLimiter


async def test_best_effort_task_limiter_skips_when_active_limit_is_reached() -> None:
    limiter = BestEffortTaskLimiter("cleanup", max_tasks=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def first() -> None:
        calls.append("first")
        first_started.set()
        await release_first.wait()

    async def second() -> None:
        calls.append("second")

    first_task = limiter.create_task(first())
    await first_started.wait()

    second_task = limiter.create_task(second())
    await second_task

    assert calls == ["first"]
    assert limiter.active_count == 1

    release_first.set()
    await first_task
    assert limiter.active_count == 0


async def test_best_effort_task_limiter_releases_slot_after_failure() -> None:
    limiter = BestEffortTaskLimiter("cleanup", max_tasks=1)

    async def failing() -> None:
        raise RuntimeError("boom")

    async def succeeding() -> None:
        return None

    await limiter.create_task(failing())
    assert limiter.active_count == 0

    await limiter.create_task(succeeding())
    assert limiter.active_count == 0


async def test_best_effort_task_limiter_drain_waits_for_active_tasks() -> None:
    limiter = BestEffortTaskLimiter("cleanup", max_tasks=1)
    release = asyncio.Event()
    completed = False

    async def cleanup() -> None:
        nonlocal completed
        await release.wait()
        completed = True

    limiter.create_task(cleanup())
    await asyncio.sleep(0)

    release.set()
    await limiter.drain(timeout=1)

    assert completed is True
    assert limiter.active_count == 0

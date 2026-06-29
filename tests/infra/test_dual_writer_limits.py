from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from src.infra.session import dual_writer


def test_dual_writer_limits_follow_runtime_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_MONGO_BUFFER_MAX",
        123,
        raising=False,
    )
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_TTL_CACHE_MAX",
        456,
        raising=False,
    )

    assert dual_writer._get_mongo_buffer_max() == 123
    assert dual_writer._get_ttl_set_keys_max() == 456


def test_dual_writer_keeps_fifty_thousand_events_per_trace_by_default() -> None:
    assert dual_writer._get_max_events_per_trace() == 50_000


def test_dual_writer_live_stream_read_timeout_is_24_hours() -> None:
    assert dual_writer._LIVE_STREAM_READ_TIMEOUT_SECONDS == 24 * 60 * 60


def test_dual_writer_idle_xread_block_matches_heartbeat_interval() -> None:
    assert dual_writer._SSE_HEARTBEAT_INTERVAL_SECONDS == 15
    assert dual_writer._REDIS_XREAD_BLOCK_MS == 5_000


class _FakeRedis:
    def __init__(self) -> None:
        self.xadd_calls: list[tuple[str, dict]] = []
        self.ttl_calls: list[str] = []
        self.expire_calls: list[tuple[str, int]] = []

    async def xadd(self, stream_key: str, fields: dict) -> None:
        self.xadd_calls.append((stream_key, fields))

    async def ttl(self, stream_key: str) -> int:
        self.ttl_calls.append(stream_key)
        return -1

    async def expire(self, stream_key: str, ttl: int) -> None:
        self.expire_calls.append((stream_key, ttl))

    async def xrange(self, stream_key: str, min: str = "-", max: str = "+") -> list:
        return []

    async def xread(self, streams: dict[str, str], block: int | None = None) -> list:
        return []


@pytest.mark.asyncio
async def test_dual_writer_refreshes_ttl_for_long_running_streams(monkeypatch) -> None:
    fake_redis = _FakeRedis()
    writer = dual_writer.DualEventWriter()
    writer._redis = fake_redis

    now = 1000.0
    monkeypatch.setattr(dual_writer.time, "monotonic", lambda: now)
    monkeypatch.setattr(dual_writer.settings, "SSE_CACHE_TTL", 86400, raising=False)

    await writer._write_to_redis_direct("session:s1:run:r1:events", {"event_type": "chunk"})
    await writer._write_to_redis_direct("session:s1:run:r1:events", {"event_type": "chunk"})

    assert fake_redis.expire_calls == [("session:s1:run:r1:events", 86400)]

    now += 301
    await writer._write_to_redis_direct("session:s1:run:r1:events", {"event_type": "chunk"})

    assert fake_redis.expire_calls == [
        ("session:s1:run:r1:events", 86400),
        ("session:s1:run:r1:events", 86400),
    ]


@pytest.mark.asyncio
async def test_dual_writer_shortens_terminal_stream_ttl() -> None:
    fake_redis = _FakeRedis()
    writer = dual_writer.DualEventWriter()
    writer._redis = fake_redis
    writer._ttl_set_keys["session:s1:run:r1:events"] = 1234.0

    await writer.expire_stream("s1", run_id="r1", ttl_seconds=60)

    assert fake_redis.expire_calls == [("session:s1:run:r1:events", 60)]
    assert "session:s1:run:r1:events" not in writer._ttl_set_keys


@pytest.mark.asyncio
async def test_write_event_offloads_redis_json_serialization_for_dict_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    writer = dual_writer.DualEventWriter()
    writer._redis = fake_redis
    calls = []

    async def _fake_run_blocking_io(func, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(dual_writer, "run_blocking_io", _fake_run_blocking_io, raising=False)

    await writer.write_event(
        session_id="s1",
        event_type="message:chunk",
        data={"content": "x" * 20_000},
        run_id="r1",
    )

    assert calls == [json.dumps]
    assert fake_redis.xadd_calls[0][1]["data"].startswith('{"content":')


@pytest.mark.asyncio
async def test_read_from_redis_offloads_replayed_event_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class _ReplayRedis:
        async def xrange(
            self,
            stream_key: str,
            min: str = "-",
            max: str = "+",
            count: int | None = None,
        ) -> list:
            del stream_key, min, max, count
            return [
                (
                    "1-0",
                    {
                        "event_type": "done",
                        "data": '{"content":"' + ("x" * 20_000) + '"}',
                        "timestamp": "t1",
                    },
                )
            ]

        async def xread(self, streams: dict[str, str], block: int | None = None) -> list:
            raise AssertionError("terminal replay event should stop before xread")

    async def _fake_run_blocking_io(func, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(dual_writer, "run_blocking_io", _fake_run_blocking_io, raising=False)

    writer = dual_writer.DualEventWriter()
    writer._redis = _ReplayRedis()

    events = [
        event
        async for event in writer.read_from_redis(
            "s1",
            run_id="r1",
            overall_timeout=1,
        )
    ]

    assert events[0]["data"]["content"] == "x" * 20_000
    assert calls == [json.loads]


@pytest.mark.asyncio
async def test_read_from_redis_replays_existing_events_in_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dual_writer, "_get_redis_replay_batch_size", lambda: 1)

    class _PagedRedis:
        def __init__(self) -> None:
            self.xrange_calls: list[tuple[str, str, str, int | None]] = []

        async def xrange(
            self,
            stream_key: str,
            min: str = "-",
            max: str = "+",
            count: int | None = None,
        ) -> list:
            self.xrange_calls.append((stream_key, min, max, count))
            if count is None:
                raise AssertionError("initial replay must use paged xrange")
            if min == "-":
                return [
                    (
                        "1-0",
                        {
                            "event_type": "message:chunk",
                            "data": '{"content":"hello"}',
                            "timestamp": "t1",
                        },
                    )
                ]
            if min == "(1-0":
                return [
                    (
                        "2-0",
                        {
                            "event_type": "done",
                            "data": "{}",
                            "timestamp": "t2",
                        },
                    )
                ]
            return []

        async def xread(self, streams: dict[str, str], block: int | None = None) -> list:
            raise AssertionError("terminal event in replay should stop before xread")

    redis = _PagedRedis()
    writer = dual_writer.DualEventWriter()
    writer._redis = redis

    events = [
        event
        async for event in writer.read_from_redis(
            "s1",
            run_id="r1",
            overall_timeout=1,
        )
    ]

    assert [event["id"] for event in events] == ["1-0", "2-0"]
    assert all(call[3] is not None for call in redis.xrange_calls)


@pytest.mark.asyncio
async def test_read_from_redis_limits_live_xread_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dual_writer, "_get_redis_replay_batch_size", lambda: 2)

    class _LiveRedis:
        def __init__(self) -> None:
            self.xread_calls: list[tuple[dict[str, str], int | None, int | None]] = []

        async def xrange(
            self,
            stream_key: str,
            min: str = "-",
            max: str = "+",
            count: int | None = None,
        ) -> list:
            return []

        async def xread(
            self,
            streams: dict[str, str],
            count: int | None = None,
            block: int | None = None,
        ) -> list:
            self.xread_calls.append((streams, count, block))
            return [
                (
                    "session:s1:run:r1:events",
                    [
                        (
                            "1-0",
                            {
                                "event_type": "done",
                                "data": "{}",
                                "timestamp": "t1",
                            },
                        )
                    ],
                )
            ]

    redis = _LiveRedis()
    writer = dual_writer.DualEventWriter()
    writer._redis = redis

    events = [
        event
        async for event in writer.read_from_redis(
            "s1",
            run_id="r1",
            overall_timeout=1,
        )
    ]

    assert [event["id"] for event in events] == ["1-0"]
    assert redis.xread_calls == [
        ({"session:s1:run:r1:events": "0"}, 2, dual_writer._REDIS_XREAD_BLOCK_MS)
    ]


@pytest.mark.asyncio
async def test_flush_mongo_buffer_does_not_wait_for_delayed_flush_event() -> None:
    writer = dual_writer.DualEventWriter()
    writer._flush_event.clear()
    flush_calls = 0

    async def _fake_do_flush() -> None:
        nonlocal flush_calls
        flush_calls += 1
        writer._flush_event.set()

    writer._do_flush = _fake_do_flush  # type: ignore[method-assign]

    await asyncio.wait_for(writer.flush_mongo_buffer(), timeout=0.1)

    assert flush_calls == 1


@pytest.mark.asyncio
async def test_flush_mongo_buffer_drains_pending_delayed_flush_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dual_writer, "_MONGO_FLUSH_INTERVAL", 60.0)

    writer = dual_writer.DualEventWriter()
    writer._flush_event.clear()
    flush_calls = 0

    async def _fake_do_flush() -> None:
        nonlocal flush_calls
        flush_calls += 1
        writer._flush_event.set()

    writer._do_flush = _fake_do_flush  # type: ignore[method-assign]
    task = asyncio.create_task(writer._schedule_flush())
    writer._flush_task = task  # type: ignore[attr-defined]

    await asyncio.sleep(0)
    await asyncio.wait_for(writer.flush_mongo_buffer(), timeout=0.1)

    assert task.done()
    assert writer._flush_task is None  # type: ignore[attr-defined]
    assert flush_calls == 1


@pytest.mark.asyncio
async def test_flush_mongo_buffer_offloads_bulk_operation_building(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _fake_run_blocking_io(func, *args, **kwargs):
        calls.append(func.__name__)
        return func(*args, **kwargs)

    class _FakeCollection:
        def __init__(self) -> None:
            self.operations = []

        async def bulk_write(self, operations, ordered: bool = False):
            del ordered
            self.operations.extend(operations)
            return type("_Result", (), {"modified_count": len(operations), "upserted_count": 0})()

    class _FakeTrace:
        def __init__(self) -> None:
            self.collection = _FakeCollection()

    monkeypatch.setattr(dual_writer, "run_blocking_io", _fake_run_blocking_io, raising=False)

    writer = dual_writer.DualEventWriter()
    writer._trace = _FakeTrace()
    writer._mongo_buffer = [
        (
            "trace-1",
            "message:chunk",
            {"content": "a"},
            "session-1",
            "run-1",
            datetime(2026, 1, 1),
        ),
        (
            "trace-1",
            "message:chunk",
            {"content": "b"},
            "session-1",
            "run-1",
            datetime(2026, 1, 1),
        ),
    ]

    await writer._do_flush()

    assert calls == ["_build_mongo_bulk_operations"]
    assert len(writer.trace.collection.operations) == 1
    assert writer._mongo_buffer == []


@pytest.mark.asyncio
async def test_flush_mongo_buffer_writes_chunks_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_CHUNK_STORAGE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_CHUNK_DUAL_WRITE_LEGACY",
        False,
        raising=False,
    )

    class _FakeTrace:
        def __init__(self) -> None:
            self.collection = object()
            self.reservations: list[tuple[str, int]] = []
            self.chunk_appends: list[tuple[dict, list[dict], int]] = []

        async def reserve_event_sequence_range(self, trace_id: str, event_count: int):
            self.reservations.append((trace_id, event_count))
            return {
                "trace_id": trace_id,
                "session_id": "session-1",
                "run_id": "run-1",
                "event_count": event_count,
            }

        async def append_events_to_chunks(
            self,
            trace_doc: dict,
            events: list[dict],
            start_seq: int,
        ) -> None:
            self.chunk_appends.append((trace_doc, events, start_seq))

    writer = dual_writer.DualEventWriter()
    writer._trace = _FakeTrace()
    writer._mongo_buffer = [
        (
            "trace-1",
            "message:chunk",
            {"content": "a"},
            "session-1",
            "run-1",
            datetime(2026, 1, 1),
        ),
        (
            "trace-1",
            "done",
            {},
            "session-1",
            "run-1",
            datetime(2026, 1, 1),
        ),
    ]

    await writer._do_flush()

    assert writer.trace.reservations == [("trace-1", 2)]
    trace_doc, events, start_seq = writer.trace.chunk_appends[0]
    assert trace_doc["trace_id"] == "trace-1"
    assert [event["event_type"] for event in events] == ["message:chunk", "done"]
    assert start_seq == 1
    assert writer._mongo_buffer == []


@pytest.mark.asyncio
async def test_flush_mongo_buffer_uses_legacy_path_when_chunk_storage_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_CHUNK_STORAGE_ENABLED",
        False,
        raising=False,
    )

    class _FakeCollection:
        def __init__(self) -> None:
            self.operations = []

        async def bulk_write(self, operations, ordered: bool = False):
            del ordered
            self.operations.extend(operations)
            return type("_Result", (), {"modified_count": len(operations), "upserted_count": 0})()

    class _FakeTrace:
        def __init__(self) -> None:
            self.collection = _FakeCollection()

        async def reserve_event_sequence_range(self, trace_id: str, event_count: int):
            raise AssertionError("legacy path must not reserve chunk sequence ranges")

        async def append_events_to_chunks(
            self,
            trace_doc: dict,
            events: list[dict],
            start_seq: int,
        ) -> None:
            raise AssertionError("legacy path must not append chunk events")

    writer = dual_writer.DualEventWriter()
    writer._trace = _FakeTrace()
    writer._mongo_buffer = [
        (
            "trace-1",
            "message:chunk",
            {"content": "a"},
            "session-1",
            "run-1",
            datetime(2026, 1, 1),
        )
    ]

    await writer._do_flush()

    assert len(writer.trace.collection.operations) == 1
    assert writer._mongo_buffer == []


@pytest.mark.asyncio
async def test_flush_mongo_buffer_requeues_failed_chunk_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_CHUNK_STORAGE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_CHUNK_DUAL_WRITE_LEGACY",
        False,
        raising=False,
    )

    class _FakeTrace:
        collection = object()

        async def reserve_event_sequence_range(self, trace_id: str, event_count: int):
            del trace_id, event_count
            return None

    writer = dual_writer.DualEventWriter()
    writer._trace = _FakeTrace()
    event = (
        "trace-1",
        "message:chunk",
        {"content": "a"},
        "session-1",
        "run-1",
        datetime(2026, 1, 1),
    )
    writer._mongo_buffer = [event]

    await writer._do_flush()

    assert writer._mongo_buffer == [event]


@pytest.mark.asyncio
async def test_flush_mongo_buffer_dual_writes_legacy_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_CHUNK_STORAGE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_CHUNK_DUAL_WRITE_LEGACY",
        True,
        raising=False,
    )

    class _FakeCollection:
        def __init__(self) -> None:
            self.operations = []

        async def bulk_write(self, operations, ordered: bool = False):
            del ordered
            self.operations.extend(operations)
            return type("_Result", (), {"modified_count": len(operations), "upserted_count": 0})()

    class _FakeTrace:
        def __init__(self) -> None:
            self.collection = _FakeCollection()

        async def reserve_event_sequence_range(self, trace_id: str, event_count: int):
            return {
                "trace_id": trace_id,
                "session_id": "session-1",
                "run_id": "run-1",
                "event_count": event_count,
            }

        async def append_events_to_chunks(
            self,
            trace_doc: dict,
            events: list[dict],
            start_seq: int,
        ) -> None:
            del trace_doc, events, start_seq

    writer = dual_writer.DualEventWriter()
    writer._trace = _FakeTrace()
    writer._mongo_buffer = [
        (
            "trace-1",
            "message:chunk",
            {"content": "a"},
            "session-1",
            "run-1",
            datetime(2026, 1, 1),
        )
    ]

    await writer._do_flush()

    assert len(writer.trace.collection.operations) == 1


@pytest.mark.asyncio
async def test_flush_mongo_buffer_dual_write_preserves_legacy_when_chunk_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_CHUNK_STORAGE_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        dual_writer.settings,
        "SESSION_EVENT_CHUNK_DUAL_WRITE_LEGACY",
        True,
        raising=False,
    )

    class _FakeCollection:
        def __init__(self) -> None:
            self.operations = []

        async def bulk_write(self, operations, ordered: bool = False):
            del ordered
            self.operations.extend(operations)
            return type("_Result", (), {"modified_count": len(operations), "upserted_count": 0})()

    class _FakeTrace:
        def __init__(self) -> None:
            self.collection = _FakeCollection()

        async def reserve_event_sequence_range(self, trace_id: str, event_count: int):
            del event_count
            return {"trace_id": trace_id, "event_count": 1}

        async def append_events_to_chunks(
            self,
            trace_doc: dict,
            events: list[dict],
            start_seq: int,
        ) -> None:
            del trace_doc, events, start_seq
            raise RuntimeError("chunk write unavailable")

    writer = dual_writer.DualEventWriter()
    writer._trace = _FakeTrace()
    writer._mongo_buffer = [
        (
            "trace-1",
            "message:chunk",
            {"content": "a"},
            "session-1",
            "run-1",
            datetime(2026, 1, 1),
        )
    ]

    await writer._do_flush()

    assert len(writer.trace.collection.operations) == 1
    assert writer._mongo_buffer == []


@pytest.mark.asyncio
async def test_mongo_buffer_overflow_flushes_instead_of_dropping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dual_writer, "_get_mongo_buffer_max", lambda: 4)

    writer = dual_writer.DualEventWriter()
    writer._redis = _FakeRedis()
    writer._flush_event.clear()
    writer._mongo_buffer = [
        (
            f"trace-{index}",
            "message:chunk",
            {"content": str(index)},
            "session-1",
            "run-1",
            datetime(2026, 1, 1),
        )
        for index in range(4)
    ]
    flush_calls = 0

    async def _fake_flush_mongo_buffer() -> None:
        nonlocal flush_calls
        flush_calls += 1
        writer._mongo_buffer = []

    writer.flush_mongo_buffer = _fake_flush_mongo_buffer  # type: ignore[method-assign]

    await writer.write_event(
        session_id="session-1",
        event_type="message:chunk",
        data={"content": "new"},
        trace_id="trace-new",
        run_id="run-1",
    )

    diagnostics = writer.get_diagnostics()

    assert flush_calls == 1
    assert diagnostics["mongo_buffer_dropped_total"] == 0
    assert diagnostics["mongo_buffer_last_drop"] is None
    assert diagnostics["mongo_buffer_size"] == 1


@pytest.mark.asyncio
async def test_terminal_events_flush_mongo_buffer_immediately() -> None:
    writer = dual_writer.DualEventWriter()
    writer._redis = _FakeRedis()
    flush_calls = 0

    async def _fake_flush_mongo_buffer() -> None:
        nonlocal flush_calls
        flush_calls += 1
        writer._mongo_buffer = []

    writer.flush_mongo_buffer = _fake_flush_mongo_buffer  # type: ignore[method-assign]

    await writer.write_event(
        session_id="session-1",
        event_type="done",
        data={},
        trace_id="trace-1",
        run_id="run-1",
    )

    assert flush_calls == 1


@pytest.mark.asyncio
async def test_close_dual_writer_flushes_and_releases_singleton() -> None:
    writer = dual_writer.DualEventWriter()
    flush_calls = 0

    async def _fake_flush_mongo_buffer() -> None:
        nonlocal flush_calls
        flush_calls += 1

    writer.flush_mongo_buffer = _fake_flush_mongo_buffer  # type: ignore[method-assign]
    dual_writer._dual_writer = writer

    await dual_writer.close_dual_writer()

    assert flush_calls == 1
    assert dual_writer._dual_writer is None


@pytest.mark.asyncio
async def test_close_dual_writer_does_not_create_singleton_when_unused() -> None:
    dual_writer._dual_writer = None

    await dual_writer.close_dual_writer()

    assert dual_writer._dual_writer is None

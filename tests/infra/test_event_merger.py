from __future__ import annotations

import pytest

from src.infra.session.event_merger import EventMerger


class _DedicatedRedis:
    def __init__(self) -> None:
        self.set_calls: list[tuple[tuple, dict]] = []
        self.eval_calls: list[tuple[tuple, dict]] = []
        self.closed = False

    async def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))
        return True

    async def eval(self, *args, **kwargs):
        self.eval_calls.append((args, kwargs))
        return 1

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_event_merger_uses_dedicated_redis_for_locking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dedicated = _DedicatedRedis()
    isolated_pool_flags: list[bool] = []

    monkeypatch.setattr(
        "src.infra.session.event_merger.create_redis_client",
        lambda isolated_pool=False: isolated_pool_flags.append(isolated_pool) or dedicated,
    )

    merger = EventMerger(trace_storage=None)

    assert await merger._acquire_lock() is True
    assert dedicated.set_calls
    assert isolated_pool_flags == [True]

    await merger._release_lock()
    assert dedicated.eval_calls

    await merger.stop()
    assert dedicated.closed is True


def test_event_merger_limits_follow_runtime_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.infra.session.event_merger as event_merger

    monkeypatch.setattr(event_merger.settings, "EVENT_MERGE_BATCH_SIZE", 17, raising=False)
    monkeypatch.setattr(event_merger.settings, "EVENT_MERGE_CONCURRENCY", 3, raising=False)
    monkeypatch.setattr(event_merger.settings, "EVENT_MERGE_TIMEOUT_SECONDS", 11, raising=False)
    monkeypatch.setattr(
        event_merger.settings,
        "EVENT_MERGE_MAX_EVENTS_PER_TRACE",
        222,
        raising=False,
    )

    assert event_merger._get_merge_batch_size() == 17
    assert event_merger._get_merge_concurrency() == 3
    assert event_merger._get_merge_timeout() == 11
    assert event_merger._get_merge_max_events_per_trace() == 222


@pytest.mark.asyncio
async def test_event_merger_processes_traces_with_bounded_coroutines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.infra.session.event_merger as event_merger

    batch_size = 9
    concurrency = 3
    monkeypatch.setattr(event_merger.settings, "EVENT_MERGE_BATCH_SIZE", batch_size, raising=False)
    monkeypatch.setattr(
        event_merger.settings,
        "EVENT_MERGE_CONCURRENCY",
        concurrency,
        raising=False,
    )

    class _Cursor:
        def __init__(self) -> None:
            self._index = 0
            self._docs = [
                {
                    "trace_id": f"trace-{index}",
                    "events": [{"event_type": "message:chunk", "data": {"text": "x"}}],
                }
                for index in range(batch_size)
            ]

        def limit(self, _limit: int):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._index >= len(self._docs):
                raise StopAsyncIteration
            item = self._docs[self._index]
            self._index += 1
            return dict(item)

        async def to_list(self, length: int):
            return [dict(doc) for doc in self._docs[:length]]

    class _Collection:
        def __init__(self) -> None:
            self.operations = []

        def find(self, *args, **kwargs):
            return _Cursor()

        async def bulk_write(self, operations, ordered: bool = False):
            del ordered
            self.operations.extend(operations)
            return type("_Result", (), {"modified_count": len(operations)})()

    class _TraceStorage:
        def __init__(self) -> None:
            self.collection = _Collection()

    merger = EventMerger(_TraceStorage())
    real_gather = event_merger.asyncio.gather
    gather_sizes: list[int] = []

    async def tracking_gather(*aws, **kwargs):
        gather_sizes.append(len(aws))
        return await real_gather(*aws, **kwargs)

    monkeypatch.setattr(event_merger.asyncio, "gather", tracking_gather)

    await merger._merge_completed_traces()

    assert gather_sizes
    assert max(gather_sizes) <= concurrency


@pytest.mark.asyncio
async def test_event_merger_streams_cursor_without_materializing_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.infra.session.event_merger as event_merger

    monkeypatch.setattr(event_merger.settings, "EVENT_MERGE_BATCH_SIZE", 3, raising=False)
    monkeypatch.setattr(event_merger.settings, "EVENT_MERGE_CONCURRENCY", 1, raising=False)

    class _Cursor:
        def __init__(self) -> None:
            self._docs = [
                {
                    "trace_id": f"trace-{index}",
                    "events": [{"event_type": "message:chunk", "data": {"content": "x"}}],
                }
                for index in range(3)
            ]
            self._index = 0

        def limit(self, _limit: int):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._index >= len(self._docs):
                raise StopAsyncIteration
            item = self._docs[self._index]
            self._index += 1
            return dict(item)

        async def to_list(self, length: int):
            raise AssertionError("event merger should stream cursor instead of to_list")

    class _Collection:
        def __init__(self) -> None:
            self.operations = []

        def find(self, *args, **kwargs):
            return _Cursor()

        async def bulk_write(self, operations, ordered: bool = False):
            del ordered
            self.operations.extend(operations)
            return type("_Result", (), {"modified_count": len(operations)})()

    class _TraceStorage:
        def __init__(self) -> None:
            self.collection = _Collection()

    storage = _TraceStorage()
    merger = EventMerger(storage)

    await merger._merge_completed_traces()

    assert len(storage.collection.operations) == 3


@pytest.mark.asyncio
async def test_event_merger_offloads_cpu_merge_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.infra.session.event_merger as event_merger

    offloaded: list[str] = []

    async def fake_run_blocking_io(func, /, *args, **kwargs):
        del kwargs
        offloaded.append(func.__name__)
        return func(*args)

    monkeypatch.setattr(event_merger, "run_blocking_io", fake_run_blocking_io, raising=False)

    merger = EventMerger(trace_storage=None)
    traces = [
        {
            "trace_id": "trace-1",
            "events": [
                {"event_type": "message:chunk", "data": {"content": "a"}},
                {"event_type": "message:chunk", "data": {"content": "b"}},
            ],
        }
    ]

    results = await merger._process_trace_merges_bounded(traces, concurrency=1)

    assert offloaded == ["_merge_events"]
    assert results[0][0] == "trace-1"
    assert results[0][2][0]["data"]["content"] == "ab"


@pytest.mark.asyncio
async def test_event_merger_filters_out_giant_traces_before_loading_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.infra.session.event_merger as event_merger

    monkeypatch.setattr(
        event_merger.settings,
        "EVENT_MERGE_MAX_EVENTS_PER_TRACE",
        5,
        raising=False,
    )

    class _Cursor:
        def limit(self, _limit: int):
            return self

        async def to_list(self, length: int):
            return []

    class _Collection:
        def __init__(self) -> None:
            self.query = None
            self.projection = None

        def find(self, query, projection):
            self.query = query
            self.projection = projection
            return _Cursor()

    class _TraceStorage:
        def __init__(self) -> None:
            self.collection = _Collection()

    storage = _TraceStorage()
    merger = EventMerger(storage)

    await merger._merge_completed_traces()

    assert storage.collection.query == {
        "status": {"$ne": "running"},
        "metadata.merged": {"$ne": True},
        "$or": [
            {"event_count": {"$lte": 5}},
            {"event_count": {"$exists": False}},
        ],
    }
    assert storage.collection.projection == {"trace_id": 1, "events": 1, "event_count": 1}

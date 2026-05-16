from types import SimpleNamespace

import pytest

from src.infra.session.trace_storage import TraceStorage


class _FakeTraceCollection:
    def __init__(self, *, has_usage: bool) -> None:
        self.has_usage = has_usage
        self.calls = []

    async def update_one(self, query, update):
        self.calls.append((query, update))
        if "events.event_type" in query:
            return SimpleNamespace(modified_count=0 if self.has_usage else 1)
        return SimpleNamespace(modified_count=1)


def _usage_event_from_pipeline(update):
    return (
        update[0]["$set"]["events"]["$let"]["vars"].get("usage_event")
        or update[0]["$set"]["events"]["$let"]["in"]["$cond"][1]["$concatArrays"][1][0]
    )


@pytest.mark.asyncio
async def test_complete_trace_adds_zero_token_usage_when_missing() -> None:
    storage = TraceStorage()
    storage._collection = _FakeTraceCollection(has_usage=False)

    assert await storage.complete_trace("trace-1", status="error") is True

    usage_query, usage_update = storage.collection.calls[0]
    assert usage_query == {
        "trace_id": "trace-1",
        "events.event_type": {"$ne": "token:usage"},
    }
    usage_event = _usage_event_from_pipeline(usage_update)
    assert usage_event["event_type"] == "token:usage"
    assert usage_event["data"]["input_tokens"] == 0
    assert usage_event["data"]["output_tokens"] == 0
    assert usage_event["data"]["total_tokens"] == 0
    done_branch = usage_update[0]["$set"]["events"]["$let"]["in"]["$cond"][1]
    assert done_branch["$concatArrays"][1] == [usage_event]


@pytest.mark.asyncio
async def test_complete_trace_does_not_duplicate_existing_token_usage() -> None:
    storage = TraceStorage()
    storage._collection = _FakeTraceCollection(has_usage=True)

    assert await storage.complete_trace("trace-1", status="completed") is True

    assert len(storage.collection.calls) == 2
    usage_update = storage.collection.calls[0][1]
    assert _usage_event_from_pipeline(usage_update)["event_type"] == "token:usage"
    assert storage.collection.calls[1][0] == {"trace_id": "trace-1"}

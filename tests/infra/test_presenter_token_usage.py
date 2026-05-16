from src.infra.writer.present import create_presenter


def test_present_token_usage_includes_model_identifiers() -> None:
    presenter = create_presenter(session_id="session-1", agent_id="search", agent_name="Search")

    event = presenter.present_token_usage(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        duration=1.2,
        model_id="b715de30-38",
        model="openai/gpt-4.1",
    )

    assert event["event"] == "token:usage"
    assert event["data"]["model_id"] == "b715de30-38"
    assert event["data"]["model"] == "openai/gpt-4.1"


class _FakeDualWriter:
    def __init__(self) -> None:
        self.events = []
        self.completed = []

    async def create_trace(self, **kwargs):
        return True

    async def write_event(self, **kwargs):
        self.events.append(kwargs)
        return True

    async def flush_mongo_buffer(self):
        return None

    async def complete_trace(self, trace_id: str, status: str, metadata=None):
        self.completed.append((trace_id, status, metadata))
        return True


async def test_complete_writes_zero_token_usage_when_missing(monkeypatch) -> None:
    writer = _FakeDualWriter()
    monkeypatch.setattr("src.infra.session.dual_writer.get_dual_writer", lambda: writer)
    presenter = create_presenter(
        session_id="session-1",
        agent_id="search",
        agent_name="Search",
        run_id="run-1",
        trace_id="trace-1",
    )

    await presenter.complete("error")

    usage_events = [event for event in writer.events if event["event_type"] == "token:usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["data"]["input_tokens"] == 0
    assert usage_events[0]["data"]["output_tokens"] == 0
    assert usage_events[0]["data"]["total_tokens"] == 0


async def test_complete_does_not_duplicate_existing_token_usage(monkeypatch) -> None:
    writer = _FakeDualWriter()
    monkeypatch.setattr("src.infra.session.dual_writer.get_dual_writer", lambda: writer)
    presenter = create_presenter(
        session_id="session-1",
        agent_id="search",
        agent_name="Search",
        run_id="run-1",
        trace_id="trace-1",
    )

    await presenter.emit(presenter.present_token_usage(input_tokens=2, output_tokens=3))
    await presenter.complete("completed")

    usage_events = [event for event in writer.events if event["event_type"] == "token:usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["data"]["input_tokens"] == 2
    assert usage_events[0]["data"]["output_tokens"] == 3


async def test_done_event_writes_zero_token_usage_first_in_same_trace(monkeypatch) -> None:
    writer = _FakeDualWriter()
    monkeypatch.setattr("src.infra.session.dual_writer.get_dual_writer", lambda: writer)
    presenter = create_presenter(
        session_id="session-1",
        agent_id="search",
        agent_name="Search",
        run_id="run-1",
        trace_id="trace-1",
    )

    await presenter.save_event(presenter.done())

    assert [event["event_type"] for event in writer.events] == ["token:usage", "done"]
    assert [event["trace_id"] for event in writer.events] == ["trace-1", "trace-1"]
    assert [event["run_id"] for event in writer.events] == ["run-1", "run-1"]

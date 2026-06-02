from types import SimpleNamespace

import pytest

from src.infra.writer.present import Presenter, PresenterConfig


class _FakeDualWriter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_trace(self, **kwargs):
        self.calls.append(kwargs)
        return True


class _FakeUserStorage:
    def __init__(self, user):
        self._user = user

    async def get_by_id(self, user_id: str):
        return self._user


@pytest.mark.asyncio
async def test_ensure_trace_includes_user_id_and_username_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    presenter = Presenter(
        PresenterConfig(
            session_id="session-1",
            agent_id="search",
            agent_name="Search Agent",
            user_id="user-123",
        )
    )
    writer = _FakeDualWriter()

    async def fake_get_dual_writer():
        return writer

    monkeypatch.setattr(presenter, "_get_dual_writer", fake_get_dual_writer)
    monkeypatch.setattr(
        "src.infra.user.storage.UserStorage",
        lambda: _FakeUserStorage(SimpleNamespace(username="alice")),
    )

    await presenter._ensure_trace()

    assert len(writer.calls) == 1
    assert writer.calls[0]["metadata"]["user_id"] == "user-123"
    assert writer.calls[0]["metadata"]["username"] == "alice"


@pytest.mark.asyncio
async def test_ensure_trace_keeps_user_id_when_username_lookup_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    presenter = Presenter(
        PresenterConfig(
            session_id="session-1",
            agent_id="search",
            agent_name="Search Agent",
            user_id="user-456",
        )
    )
    writer = _FakeDualWriter()

    async def fake_get_dual_writer():
        return writer

    monkeypatch.setattr(presenter, "_get_dual_writer", fake_get_dual_writer)
    monkeypatch.setattr("src.infra.user.storage.UserStorage", lambda: _FakeUserStorage(None))

    await presenter._ensure_trace()

    assert len(writer.calls) == 1
    assert writer.calls[0]["metadata"]["user_id"] == "user-456"
    assert "username" not in writer.calls[0]["metadata"]


def test_present_tool_start_compacts_large_input_and_tool_call_state() -> None:
    large_input = "x" * 20_000
    presenter = Presenter()

    event = presenter.present_tool_start(
        "large_tool",
        {"query": large_input},
        tool_call_id="tool-1",
    )

    assert event["data"]["args"]["query"] != large_input
    assert len(event["data"]["args"]["query"]) < 3_000
    assert "truncated from 20000 chars" in event["data"]["args"]["query"]
    assert len(presenter._tool_calls) == 1
    assert large_input not in str(presenter._tool_calls[0])

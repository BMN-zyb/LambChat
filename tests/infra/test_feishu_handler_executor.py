from __future__ import annotations

from typing import Any

import pytest

from src.infra.channel.feishu import handler as feishu_handler


class _FakeManager:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str, str]] = []

    async def send_message(self, user_id: str, chat_id: str, content: str) -> None:
        self.sent_messages.append((user_id, chat_id, content))


class _FakeReactionManager:
    def __init__(self) -> None:
        self.add_calls: list[tuple[str, str, str]] = []
        self.delete_calls: list[tuple[str, str, str]] = []

    async def add_reaction(self, user_id: str, message_id: str, emoji_type: str) -> str:
        self.add_calls.append((user_id, message_id, emoji_type))
        return "reaction-1"

    async def delete_reaction(self, user_id: str, message_id: str, reaction_id: str) -> bool:
        self.delete_calls.append((user_id, message_id, reaction_id))
        return True


class _FakeTaskManager:
    def __init__(self) -> None:
        self.submit_calls: list[dict[str, Any]] = []

    async def submit(self, **kwargs: Any) -> tuple[str, str]:
        self.submit_calls.append(kwargs)
        executor = kwargs["executor"]
        async for _event in executor(
            kwargs["session_id"],
            kwargs["agent_id"],
            kwargs["message"],
            kwargs["user_id"],
            enabled_skills=["planning"],
            persona_system_prompt="Persona prompt",
            disabled_mcp_tools=["mcp.tool"],
        ):
            pass
        return "run-1", ""


@pytest.mark.asyncio
async def test_feishu_executor_accepts_task_runtime_skill_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    fake_task_manager = _FakeTaskManager()
    fake_manager = _FakeManager()

    async def _fake_execute_feishu_agent(**kwargs: Any):
        captured.update(kwargs)
        yield {"event": "done", "data": {}}

    async def _no_op_process_events(**kwargs: Any) -> None:
        return None

    async def _no_op_collector_method(self) -> None:
        return None

    monkeypatch.setattr(
        feishu_handler,
        "_get_feishu_session_id",
        lambda chat_id: _async_return(f"feishu_{chat_id}"),
    )
    monkeypatch.setattr(
        "src.infra.task.manager.get_task_manager",
        lambda: fake_task_manager,
    )
    monkeypatch.setattr(feishu_handler, "execute_feishu_agent", _fake_execute_feishu_agent)
    monkeypatch.setattr(feishu_handler, "_process_events", _no_op_process_events)
    monkeypatch.setattr(
        feishu_handler.FeishuResponseCollector,
        "stop_processing_indicator",
        _no_op_collector_method,
    )
    monkeypatch.setattr(
        feishu_handler.FeishuResponseCollector,
        "send_card_message",
        _no_op_collector_method,
    )
    monkeypatch.setattr(
        feishu_handler.FeishuResponseCollector,
        "upload_and_send_files",
        _no_op_collector_method,
    )

    handler = feishu_handler.create_feishu_message_handler(fake_manager, default_agent="search")

    await handler(
        user_id="user-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="hello",
        metadata={},
    )

    assert fake_manager.sent_messages == []
    assert captured["enabled_skills"] == ["planning"]
    assert captured["persona_system_prompt"] == "Persona prompt"
    assert captured["disabled_mcp_tools"] == ["mcp.tool"]


async def _async_return(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_feishu_processing_indicator_adds_once_and_removes_on_stop() -> None:
    manager = _FakeReactionManager()
    collector = feishu_handler.FeishuResponseCollector(
        manager=manager,
        user_id="user-1",
        chat_id="chat-1",
    )

    await collector.start_processing_indicator("message-1")
    await collector.start_processing_indicator("message-1")
    await collector.stop_processing_indicator()
    await collector.stop_processing_indicator()

    assert manager.add_calls == [("user-1", "message-1", "StatusInFlight")]
    assert manager.delete_calls == [("user-1", "message-1", "reaction-1")]

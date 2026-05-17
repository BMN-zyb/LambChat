from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import Any

import pytest

from src.infra.channel.feishu import handler as feishu_handler


class _FakeManager:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str, str]] = []

    async def send_message(
        self, user_id: str, chat_id: str, content: str, instance_id: str | None = None
    ) -> None:
        self.sent_messages.append((user_id, chat_id, content))


class _FakeReactionManager:
    def __init__(self) -> None:
        self.add_calls: list[tuple[str, str, str]] = []
        self.delete_calls: list[tuple[str, str, str]] = []

    async def add_reaction(
        self,
        user_id: str,
        message_id: str,
        emoji_type: str,
        instance_id: str | None = None,
    ) -> str:
        self.add_calls.append((user_id, message_id, emoji_type))
        return "reaction-1"

    async def delete_reaction(
        self,
        user_id: str,
        message_id: str,
        reaction_id: str,
        instance_id: str | None = None,
    ) -> bool:
        self.delete_calls.append((user_id, message_id, reaction_id))
        return True


class _FakeStreamingClient:
    def __init__(self) -> None:
        self.created = 0
        self.initial_texts: list[str] = []
        self.sent: list[tuple[str, str, str | None]] = []
        self.updates: list[tuple[str, str, int]] = []
        self.finalized: list[tuple[str, str, int]] = []

    async def create_stream_card(self, initial_text: str = "...") -> str:
        self.created += 1
        self.initial_texts.append(initial_text)
        return "card-1"

    async def send_card_by_id(
        self, chat_id: str, card_id: str, *, reply_to_id: str | None = None
    ) -> tuple[bool, str]:
        self.sent.append((chat_id, card_id, reply_to_id))
        return True, "message-1"

    async def update_stream_card(self, card_id: str, content: str, sequence: int) -> bool:
        self.updates.append((card_id, content, sequence))
        return True

    async def finalize_stream_card(self, card_id: str, content: str, sequence: int) -> bool:
        self.finalized.append((card_id, content, sequence))
        return True


class _FakeFileClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[bytes, str]] = []
        self.sent_files: list[tuple[str, str, str, str | None]] = []

    async def upload_bytes(self, file_data: bytes, file_name: str) -> str:
        self.uploads.append((file_data, file_name))
        return f"feishu-{file_name}"

    async def send_file_by_key(
        self,
        chat_id: str,
        file_key: str,
        file_name: str,
        reply_to_id: str | None = None,
    ) -> bool:
        self.sent_files.append((chat_id, file_key, file_name, reply_to_id))
        return True


class _FakeStreamingManager:
    def __init__(self, client: Any) -> None:
        self.client = client

    def _find_channel(self, user_id: str, instance_id: str | None = None):
        return self.client if user_id == "user-1" else None


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


def _install_fake_task_manager_module(
    monkeypatch: pytest.MonkeyPatch, fake_task_manager: _FakeTaskManager
) -> None:
    task_module = ModuleType("src.infra.task")
    manager_module = ModuleType("src.infra.task.manager")
    manager_module.get_task_manager = lambda: fake_task_manager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.infra.task", task_module)
    monkeypatch.setitem(sys.modules, "src.infra.task.manager", manager_module)


class _FakeChannelStorage:
    def __init__(self) -> None:
        self.cleared_configs: list[tuple[str, Any, str]] = []

    async def get_config(self, user_id: str, channel_type: Any, instance_id: str):
        return {
            "name": "Feishu Channel",
            "agent_id": "search",
            "model_id": None,
            "project_id": "deleted-project",
        }

    async def clear_config_project_id(
        self, user_id: str, channel_type: Any, instance_id: str
    ) -> int:
        self.cleared_configs.append((user_id, channel_type, instance_id))
        return 1


class _FakePersonaChannelStorage:
    async def get_config(self, user_id: str, channel_type: Any, instance_id: str):
        return {
            "name": "Feishu Channel",
            "agent_id": "search",
            "model_id": "model-1",
            "project_id": None,
            "persona_preset_id": "persona-1",
        }


class _FakePersonaPresetManager:
    async def use_preset(self, preset_id: str, *, user_id: str, is_admin: bool):
        assert preset_id == "persona-1"
        assert user_id == "user-1"
        assert is_admin is False
        return type(
            "Snapshot",
            (),
            {
                "preset_id": "persona-1",
                "name": "Planner",
                "system_prompt": "Plan first.",
                "skill_names": ["planning"],
                "missing_skill_names": [],
                "version": 2,
                "avatar": "icon:brain",
                "starter_prompts": [],
                "model_dump": lambda self: {
                    "preset_id": "persona-1",
                    "name": "Planner",
                    "system_prompt": "Plan first.",
                    "skill_names": ["planning"],
                    "missing_skill_names": [],
                    "version": 2,
                    "avatar": "icon:brain",
                    "starter_prompts": [],
                },
            },
        )()


class _FakeSessionManager:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def update_session(self, session_id: str, session_update: Any):
        self.updates.append((session_id, session_update))
        return None


class _FakeProjectStorage:
    def __init__(self) -> None:
        self.created_names: list[tuple[str, str]] = []

    async def get_by_id(self, project_id: str, user_id: str):
        return None

    async def get_or_create_by_name(self, user_id: str, name: str):
        self.created_names.append((user_id, name))
        return type("Project", (), {"id": "project-from-channel-name"})()


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
    _install_fake_task_manager_module(monkeypatch, fake_task_manager)
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


@pytest.mark.asyncio
async def test_feishu_handler_ignores_stale_channel_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_task_manager = _FakeTaskManager()
    fake_manager = _FakeManager()
    fake_project_storage = _FakeProjectStorage()
    fake_channel_storage = _FakeChannelStorage()

    async def _fake_execute_feishu_agent(**kwargs: Any):
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
    _install_fake_task_manager_module(monkeypatch, fake_task_manager)
    monkeypatch.setattr(
        "src.infra.channel.channel_storage.ChannelStorage",
        lambda: fake_channel_storage,
    )
    monkeypatch.setattr(
        "src.infra.folder.storage.get_project_storage",
        lambda: fake_project_storage,
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

    handler = feishu_handler.create_feishu_message_handler(fake_manager, default_agent="fast")

    await handler(
        user_id="user-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="hello",
        metadata={"instance_id": "instance-1"},
    )

    assert fake_project_storage.created_names == [("user-1", "Feishu Channel")]
    assert fake_channel_storage.cleared_configs
    assert fake_task_manager.submit_calls[0]["project_id"] == "project-from-channel-name"


@pytest.mark.asyncio
async def test_feishu_handler_applies_channel_persona_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_task_manager = _FakeTaskManager()
    fake_manager = _FakeManager()
    fake_session_manager = _FakeSessionManager()

    async def _fake_execute_feishu_agent(**kwargs: Any):
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
    _install_fake_task_manager_module(monkeypatch, fake_task_manager)
    monkeypatch.setattr(
        "src.infra.channel.channel_storage.ChannelStorage",
        lambda: _FakePersonaChannelStorage(),
    )
    monkeypatch.setattr(
        "src.infra.persona_preset.manager.PersonaPresetManager",
        lambda: _FakePersonaPresetManager(),
    )
    monkeypatch.setattr(
        "src.infra.session.manager.SessionManager",
        lambda: fake_session_manager,
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

    handler = feishu_handler.create_feishu_message_handler(fake_manager, default_agent="fast")

    await handler(
        user_id="user-1",
        sender_id="sender-1",
        chat_id="chat-1",
        content="hello",
        metadata={"instance_id": "instance-1"},
    )

    submit_call = fake_task_manager.submit_calls[0]
    assert submit_call["agent_options"] == {"model_id": "model-1"}
    assert submit_call["enabled_skills"] == ["planning"]
    assert submit_call["persona_system_prompt"] == "Plan first."
    session_id, session_update = fake_session_manager.updates[0]
    metadata = session_update.metadata
    assert session_id == "feishu_chat-1"
    assert metadata["persona_preset_id"] == "persona-1"
    assert metadata["persona_preset_name"] == "Planner"
    assert metadata["persona_avatar"] == "icon:brain"
    assert metadata["persona_snapshot"]["system_prompt"] == "Plan first."
    assert metadata["enabled_skills"] == ["planning"]


@pytest.mark.asyncio
async def test_feishu_handler_uses_event_chat_id_for_p2p_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_task_manager = _FakeTaskManager()
    fake_manager = _FakeManager()
    captured_collector: dict[str, Any] = {}

    async def _fake_execute_feishu_agent(**kwargs: Any):
        yield {"event": "done", "data": {}}

    async def _no_op_process_events(**kwargs: Any) -> None:
        return None

    class _CaptureCollector:
        def __init__(self, **kwargs: Any) -> None:
            captured_collector.update(kwargs)

        async def start_processing_indicator(self, message_id: str) -> None:
            return None

        async def stop_processing_indicator(self) -> None:
            return None

        async def finalize_stream_message(self) -> bool:
            return False

        async def send_card_message(self) -> bool:
            return True

        async def upload_and_send_files(self) -> None:
            return None

    monkeypatch.setattr(
        feishu_handler,
        "_get_feishu_session_id",
        lambda chat_id: _async_return(f"feishu_{chat_id}"),
    )
    _install_fake_task_manager_module(monkeypatch, fake_task_manager)
    monkeypatch.setattr(feishu_handler, "execute_feishu_agent", _fake_execute_feishu_agent)
    monkeypatch.setattr(feishu_handler, "_process_events", _no_op_process_events)
    monkeypatch.setattr(feishu_handler, "FeishuResponseCollector", _CaptureCollector)

    handler = feishu_handler.create_feishu_message_handler(fake_manager, default_agent="search")

    await handler(
        user_id="user-1",
        sender_id="ou_sender",
        chat_id="ou_sender",
        content="hello",
        metadata={
            "message_id": "om_original",
            "chat_type": "p2p",
            "reply_chat_id": "oc_p2p_chat",
        },
    )

    assert fake_task_manager.submit_calls[0]["session_id"] == "feishu_ou_sender"
    assert captured_collector["chat_id"] == "oc_p2p_chat"
    assert captured_collector["reply_to_message_id"] == "om_original"


@pytest.mark.asyncio
async def test_feishu_handler_does_not_add_processing_indicator_reaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_task_manager = _FakeTaskManager()
    fake_manager = _FakeManager()
    processing_calls: list[str] = []

    async def _fake_execute_feishu_agent(**kwargs: Any):
        yield {"event": "done", "data": {}}

    async def _no_op_process_events(**kwargs: Any) -> None:
        return None

    class _CaptureCollector:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def start_processing_indicator(self, message_id: str) -> None:
            processing_calls.append(message_id)

        async def stop_processing_indicator(self) -> None:
            return None

        async def finalize_stream_message(self) -> bool:
            return False

        async def send_card_message(self) -> bool:
            return True

        async def upload_and_send_files(self) -> None:
            return None

    monkeypatch.setattr(
        feishu_handler,
        "_get_feishu_session_id",
        lambda chat_id: _async_return(f"feishu_{chat_id}"),
    )
    _install_fake_task_manager_module(monkeypatch, fake_task_manager)
    monkeypatch.setattr(feishu_handler, "execute_feishu_agent", _fake_execute_feishu_agent)
    monkeypatch.setattr(feishu_handler, "_process_events", _no_op_process_events)
    monkeypatch.setattr(feishu_handler, "FeishuResponseCollector", _CaptureCollector)

    handler = feishu_handler.create_feishu_message_handler(fake_manager, default_agent="search")

    await handler(
        user_id="user-1",
        sender_id="ou_sender",
        chat_id="oc_chat",
        content="hello",
        metadata={
            "message_id": "om_original",
            "chat_type": "group",
            "sender_id": "ou_sender",
        },
    )

    assert processing_calls == []


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


@pytest.mark.asyncio
async def test_feishu_collector_streams_chunks_and_finalizes_card() -> None:
    client = _FakeStreamingClient()
    collector = feishu_handler.FeishuResponseCollector(
        manager=_FakeStreamingManager(client),
        user_id="user-1",
        chat_id="oc_chat",
        reply_to_message_id="original-message",
        stream_reply=True,
    )

    await collector.append_stream_chunk("hello")
    await asyncio.sleep(0)
    await collector.append_stream_chunk(" world")
    await asyncio.sleep(feishu_handler.FEISHU_STREAM_UPDATE_DEBOUNCE_SECONDS + 0.05)

    assert client.created == 1
    assert client.initial_texts == ["hello"]
    assert client.sent == [("oc_chat", "card-1", "original-message")]
    assert client.updates == [("card-1", "hello world", 1)]

    assert await collector.finalize_stream_message() is True
    assert client.finalized == [("card-1", "hello world", 2)]


@pytest.mark.asyncio
async def test_feishu_collector_splits_large_first_stream_update() -> None:
    client = _FakeStreamingClient()
    collector = feishu_handler.FeishuResponseCollector(
        manager=_FakeStreamingManager(client),
        user_id="user-1",
        chat_id="oc_chat",
        stream_reply=True,
    )

    first_chunk = "这是一段比较长的开头内容，应该先推很短的一小段"
    await collector.append_stream_chunk(first_chunk)
    await asyncio.sleep(feishu_handler.FEISHU_STREAM_UPDATE_DEBOUNCE_SECONDS + 0.05)

    assert client.initial_texts == [first_chunk[: feishu_handler.FEISHU_STREAM_FIRST_PAINT_CHARS]]
    assert client.updates == [("card-1", first_chunk, 1)]


@pytest.mark.asyncio
async def test_feishu_collector_falls_back_when_stream_card_creation_fails() -> None:
    class _FailingClient(_FakeStreamingClient):
        async def create_stream_card(self, initial_text: str = "...") -> None:
            self.created += 1
            return None

    client = _FailingClient()
    collector = feishu_handler.FeishuResponseCollector(
        manager=_FakeStreamingManager(client),
        user_id="user-1",
        chat_id="oc_chat",
        stream_reply=True,
    )

    await collector.append_stream_chunk("hello")

    assert collector.text_parts == ["hello"]
    assert await collector.finalize_stream_message() is False
    assert client.sent == []


@pytest.mark.asyncio
async def test_upload_and_send_files_replies_to_original_message_and_skips_sent_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeFileClient()
    collector = feishu_handler.FeishuResponseCollector(
        manager=_FakeStreamingManager(client),
        user_id="user-1",
        chat_id="oc_chat",
        reply_to_message_id="om_original",
    )
    collector.add_file_to_reveal({"key": "revealed_files/doc.md", "name": "doc.md"})

    class _FakeBackend:
        async def download(self, key: str) -> bytes:
            return b"file-bytes"

    class _FakeStorage:
        def _get_backend(self) -> _FakeBackend:
            return _FakeBackend()

    async def _fake_get_storage() -> _FakeStorage:
        return _FakeStorage()

    monkeypatch.setattr(
        "src.infra.storage.s3.service.get_or_init_storage",
        _fake_get_storage,
    )

    await collector.upload_and_send_files()
    await collector.upload_and_send_files()

    assert client.uploads == [(b"file-bytes", "doc.md")]
    assert client.sent_files == [
        ("oc_chat", "feishu-doc.md", "doc.md", "om_original"),
    ]


@pytest.mark.asyncio
async def test_process_events_uploads_revealed_file_when_tool_result_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {
            "event_type": feishu_handler.EVENT_MESSAGE_CHUNK,
            "data": {"content": "before"},
        },
        {
            "event_type": feishu_handler.EVENT_TOOL_RESULT,
            "data": {
                "tool": "reveal_file",
                "result": {"key": "revealed_files/doc.md", "name": "doc.md"},
            },
        },
        {
            "event_type": feishu_handler.EVENT_MESSAGE_CHUNK,
            "data": {"content": "after"},
        },
        {"event_type": "done", "data": {}},
    ]

    class _FakeDualWriter:
        async def read_from_redis(self, session_id: str, run_id: str):
            for event in events:
                yield event

    class _CaptureCollector:
        def __init__(self) -> None:
            self.files_to_reveal: list[dict[str, Any]] = []
            self.calls: list[str] = []

        async def append_stream_chunk(self, chunk: str) -> None:
            self.calls.append(f"chunk:{chunk}")

        def add_tool(self, tool_name: str) -> None:
            self.calls.append(f"tool:{tool_name}")

        def add_file_to_reveal(self, file_info: dict) -> None:
            self.files_to_reveal.append(file_info)
            self.calls.append(f"add:{file_info['name']}")

        async def upload_and_send_files(self) -> None:
            names = ",".join(file["name"] for file in self.files_to_reveal)
            self.calls.append(f"upload:{names}")

    monkeypatch.setattr(
        "src.infra.session.dual_writer.get_dual_writer",
        lambda: _FakeDualWriter(),
    )

    collector = _CaptureCollector()
    await feishu_handler._process_events(
        collector=collector,
        session_id="session-1",
        run_id="run-1",
        show_tools=True,
    )

    assert collector.calls == [
        "chunk:before",
        "add:doc.md",
        "upload:doc.md",
        "chunk:after",
    ]

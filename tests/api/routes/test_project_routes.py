from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api.routes import project as project_route


class _FakeProjectStorage:
    def __init__(self, project) -> None:
        self.project = project
        self.deleted: list[tuple[str, str]] = []

    async def get_by_id(self, project_id: str, user_id: str):
        if self.project and self.project.id == project_id and self.project.user_id == user_id:
            return self.project
        return None

    async def delete(self, project_id: str, user_id: str) -> bool:
        self.deleted.append((project_id, user_id))
        return True


class _FakeSessionStorage:
    def __init__(self) -> None:
        self.clear_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []

    async def clear_project_id(self, project_id: str, user_id: str) -> int:
        self.clear_calls.append((project_id, user_id))
        return 1

    async def delete_by_project(self, project_id: str, user_id: str) -> int:
        self.delete_calls.append((project_id, user_id))
        return 1


class _FakeChannelStorage:
    def __init__(self) -> None:
        self.clear_calls: list[tuple[str, str]] = []

    async def clear_project_id(self, project_id: str, user_id: str) -> int:
        self.clear_calls.append((project_id, user_id))
        return 1


class _FakeRevealedStorage:
    async def clear_project_id(self, project_id: str) -> int:
        return 1


@pytest.mark.asyncio
async def test_delete_project_clears_channel_config_project_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = SimpleNamespace(id="project-1", user_id="user-1", type="channel")
    project_storage = _FakeProjectStorage(project)
    session_storage = _FakeSessionStorage()
    channel_storage = _FakeChannelStorage()

    monkeypatch.setattr(project_route, "get_project_storage", lambda: project_storage)
    monkeypatch.setattr(project_route, "SessionStorage", lambda: session_storage)
    monkeypatch.setattr(
        "src.infra.revealed_file.storage.get_revealed_file_storage",
        lambda: _FakeRevealedStorage(),
    )
    monkeypatch.setattr(
        "src.infra.channel.channel_storage.ChannelStorage",
        lambda: channel_storage,
    )

    response = await project_route.delete_project(
        "project-1",
        delete_sessions=False,
        user=SimpleNamespace(sub="user-1"),
    )

    assert response == {"status": "deleted"}
    assert session_storage.clear_calls == [("project-1", "user-1")]
    assert channel_storage.clear_calls == [("project-1", "user-1")]

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import deps as api_deps
from src.api.routes import persona_preset as persona_preset_route
from src.kernel.schemas.persona_preset import (
    PersonaPreset,
    PersonaPresetCreate,
    PersonaPresetPreferenceUpdate,
    PersonaPresetScope,
    PersonaPresetStatus,
    PersonaPresetVisibility,
)
from src.kernel.schemas.user import TokenPayload


def _fake_user(*permissions: str) -> TokenPayload:
    return TokenPayload(
        sub="user-1",
        username="tester",
        roles=["user"],
        permissions=list(permissions),
    )


@pytest.mark.asyncio
async def test_list_persona_presets_returns_real_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeManager:
        async def list_presets(self, **kwargs):
            assert kwargs["skip"] == 20
            assert kwargs["limit"] == 10
            return [
                PersonaPreset(
                    id="preset-1",
                    scope=PersonaPresetScope.GLOBAL,
                    owner_user_id=None,
                    name="Planner",
                    description="Plan carefully",
                    avatar=None,
                    tags=["planning"],
                    system_prompt="Plan first.",
                    skill_names=["planner"],
                    visibility=PersonaPresetVisibility.PUBLIC,
                    status=PersonaPresetStatus.PUBLISHED,
                    source_preset_id=None,
                    copied_from_version=None,
                    version=1,
                    usage_count=5,
                    created_by="admin-1",
                    updated_by="admin-1",
                    created_at=datetime(2026, 1, 1),
                    updated_at=datetime(2026, 1, 2),
                )
            ]

        async def count_presets(self, **kwargs):
            assert kwargs["skip"] == 20
            assert kwargs["limit"] == 10
            return 37

    monkeypatch.setattr(persona_preset_route, "_manager", lambda: _FakeManager())

    app = FastAPI()
    app.include_router(persona_preset_route.router, prefix="/api/persona-presets")
    app.dependency_overrides[api_deps.get_current_user_required] = lambda: _fake_user(
        "persona_preset:read"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/persona-presets/?skip=20&limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 37
    assert len(payload["presets"]) == 1


@pytest.mark.asyncio
async def test_update_persona_preset_preference_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeManager:
        async def update_preference(self, preset_id: str, **kwargs):
            assert preset_id == "preset-1"
            assert kwargs["user_id"] == "user-1"
            assert kwargs["is_admin"] is False
            assert kwargs["is_favorite"] is True
            assert kwargs["is_pinned"] is None
            return PersonaPreset(
                id="preset-1",
                scope=PersonaPresetScope.GLOBAL,
                owner_user_id=None,
                name="Planner",
                description="Plan carefully",
                avatar=None,
                tags=["planning"],
                system_prompt="Plan first.",
                skill_names=["planner"],
                visibility=PersonaPresetVisibility.PUBLIC,
                status=PersonaPresetStatus.PUBLISHED,
                source_preset_id=None,
                copied_from_version=None,
                version=1,
                usage_count=5,
                is_favorite=True,
                is_pinned=False,
                last_used_at=None,
                created_by="admin-1",
                updated_by="admin-1",
                created_at=datetime(2026, 1, 1),
                updated_at=datetime(2026, 1, 2),
            )

    assert PersonaPresetPreferenceUpdate(is_favorite=True).is_favorite is True
    monkeypatch.setattr(persona_preset_route, "_manager", lambda: _FakeManager())

    app = FastAPI()
    app.include_router(persona_preset_route.router, prefix="/api/persona-presets")
    app.dependency_overrides[api_deps.get_current_user_required] = lambda: _fake_user(
        "persona_preset:read"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.patch(
            "/api/persona-presets/preset-1/preference",
            json={"is_favorite": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "preset-1"
    assert payload["is_favorite"] is True


@pytest.mark.asyncio
async def test_batch_create_persona_presets_rejects_too_many_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeManager:
        async def batch_create_presets(self, *args, **kwargs):
            raise AssertionError("oversized batch should be rejected before manager call")

    monkeypatch.setattr(persona_preset_route, "_manager", lambda: _FakeManager())

    app = FastAPI()
    app.include_router(persona_preset_route.router, prefix="/api/persona-presets")
    app.dependency_overrides[api_deps.get_current_user_required] = lambda: _fake_user(
        "persona_preset:write"
    )

    item = PersonaPresetCreate(name="Preset", system_prompt="Prompt").model_dump(mode="json")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/persona-presets/batch", json=[item] * 101)

    assert response.status_code == 422

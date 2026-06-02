from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.middleware.tracing import TracingMiddleware
from src.api.middleware.user_context import UserContextMiddleware
from src.infra.logging.context import TraceContext


@pytest.mark.asyncio
async def test_user_context_middleware_adds_user_and_session_to_logging_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "src.api.middleware.user_context.verify_token",
        lambda token: SimpleNamespace(sub="user-1"),
    )
    app = FastAPI()
    app.add_middleware(UserContextMiddleware)
    app.add_middleware(TracingMiddleware)

    @app.get("/context")
    async def context():
        req_ctx = TraceContext.get_request_context()
        return {
            "request_id": req_ctx.request_id,
            "trace_id": req_ctx.trace_id,
            "user_id": req_ctx.user_id,
            "session_id": req_ctx.session_id,
        }

    with caplog.at_level(logging.INFO, logger="src.api.middleware.tracing"):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/context",
                headers={
                    "Authorization": "Bearer token-1",
                    "X-Request-ID": "req-1",
                    "X-Trace-ID": "trace-1",
                    "X-Session-Id": "session-1",
                },
            )

    assert response.status_code == 200
    assert response.json() == {
        "request_id": "req-1",
        "trace_id": "trace-1",
        "user_id": "user-1",
        "session_id": "session-1",
    }
    assert TraceContext.get_request_context().user_id is None
    assert any(
        record.request_id == "req-1"
        and record.user_id == "user-1"
        and record.session_id == "session-1"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_user_context_middleware_clears_logging_context_without_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.api.middleware.user_context.verify_token",
        lambda token: SimpleNamespace(sub="user-1"),
    )
    app = FastAPI()
    app.add_middleware(UserContextMiddleware)

    @app.get("/context")
    async def context():
        req_ctx = TraceContext.get_request_context()
        return {"user_id": req_ctx.user_id, "session_id": req_ctx.session_id}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/context",
            headers={
                "Authorization": "Bearer token-1",
                "X-Session-Id": "session-1",
            },
        )

    assert response.json() == {"user_id": "user-1", "session_id": "session-1"}
    assert TraceContext.get_request_context().user_id is None
    assert TraceContext.get_request_context().session_id is None


@pytest.mark.asyncio
async def test_user_context_middleware_offloads_token_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.middleware import user_context as user_context_module

    inside_blocking_io = False
    calls: list[object] = []

    async def fake_run_blocking_io(func, /, *args: Any, **kwargs: Any) -> Any:
        nonlocal inside_blocking_io
        calls.append(func)
        assert inside_blocking_io is False
        inside_blocking_io = True
        try:
            return func(*args, **kwargs)
        finally:
            inside_blocking_io = False

    def fake_verify_token(token: str) -> SimpleNamespace:
        assert inside_blocking_io, "middleware verify_token must be offloaded"
        assert token == "token-1"
        return SimpleNamespace(sub="user-1")

    monkeypatch.setattr(user_context_module, "run_blocking_io", fake_run_blocking_io, raising=False)
    monkeypatch.setattr(user_context_module, "verify_token", fake_verify_token)

    app = FastAPI()
    app.add_middleware(UserContextMiddleware)

    @app.get("/context")
    async def context():
        req_ctx = TraceContext.get_request_context()
        return {"user_id": req_ctx.user_id}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/context",
            headers={"Authorization": "Bearer token-1"},
        )

    assert response.json() == {"user_id": "user-1"}
    assert calls == [fake_verify_token]

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.middleware.tracing import TracingMiddleware
from src.infra.logging.context import TraceContext


@pytest.mark.asyncio
async def test_tracing_middleware_returns_supplied_request_id_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    app.add_middleware(TracingMiddleware)

    @app.get("/ping")
    async def ping():
        info = TraceContext.get()
        return {"request_id": info.request_id, "trace_id": info.trace_id}

    with caplog.at_level(logging.INFO, logger="src.api.middleware.tracing"):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/ping",
                headers={"X-Request-ID": "req-123", "X-Trace-ID": "trace-123"},
            )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-123"
    assert response.headers["X-Trace-ID"] == "trace-123"
    assert response.json() == {"request_id": "req-123", "trace_id": "trace-123"}
    assert any(
        record.request_id == "req-123"
        and record.trace_id == "trace-123"
        and "http_request_completed" in record.message
        and "method=GET" in record.message
        and "path=/ping" in record.message
        and "status_code=200" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_tracing_middleware_generates_request_id_when_missing() -> None:
    app = FastAPI()
    app.add_middleware(TracingMiddleware)

    @app.get("/ping")
    async def ping():
        return {"request_id": TraceContext.get().request_id}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/ping")

    request_id = response.headers["X-Request-ID"]
    assert request_id
    assert response.json() == {"request_id": request_id}


@pytest.mark.asyncio
async def test_tracing_middleware_replaces_invalid_request_id_header() -> None:
    app = FastAPI()
    app.add_middleware(TracingMiddleware)

    @app.get("/ping")
    async def ping():
        return {"request_id": TraceContext.get().request_id}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/ping", headers={"X-Request-ID": "bad\nid"})

    request_id = response.headers["X-Request-ID"]
    assert request_id != "bad\nid"
    assert len(request_id) == 32
    assert response.json() == {"request_id": request_id}


@pytest.mark.asyncio
async def test_tracing_middleware_clears_context_after_request() -> None:
    app = FastAPI()
    app.add_middleware(TracingMiddleware)

    @app.get("/ping")
    async def ping():
        return {"request_id": TraceContext.get().request_id}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        await client.get("/ping", headers={"X-Request-ID": "req-clear"})

    assert TraceContext.get().request_id is None
    assert TraceContext.get().trace_id is None
    assert TraceContext.get_request_context().request_id is None


@pytest.mark.asyncio
async def test_tracing_middleware_logs_and_clears_context_on_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    app.add_middleware(TracingMiddleware)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="src.api.middleware.tracing"):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/boom", headers={"X-Request-ID": "req-error"})

    assert response.status_code == 500
    assert TraceContext.get().request_id is None
    assert TraceContext.get_request_context().request_id is None
    assert any(
        record.request_id == "req-error"
        and "http_request_failed" in record.message
        and "path=/boom" in record.message
        for record in caplog.records
    )

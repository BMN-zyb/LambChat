from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Scope

from src.api import main as api_main


@pytest.mark.asyncio
async def test_body_size_middleware_rejects_large_json_before_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main, "API_REQUEST_BODY_MAX_BYTES", 8, raising=False)
    app = FastAPI()
    app.add_middleware(api_main.RequestBodyLimitMiddleware)

    called = False

    @app.post("/json")
    async def json_route(request: Request):
        nonlocal called
        called = True
        return await request.json()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/json", content=b'{"message":"too large"}')

    assert response.status_code == 413
    assert called is False


@pytest.mark.asyncio
async def test_body_size_middleware_rejects_streaming_body_without_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main, "API_REQUEST_BODY_MAX_BYTES", 8, raising=False)

    called = False

    async def app(scope: Scope, receive, send):
        nonlocal called
        called = True
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = api_main.RequestBodyLimitMiddleware(app)
    scope: Scope = {"type": "http", "method": "POST", "path": "/json", "headers": []}
    messages: list[Message] = [
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"56789", "more_body": False},
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope, receive, send)

    assert called is False
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_body_size_middleware_streams_allowed_content_length_without_prebuffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main, "API_REQUEST_BODY_MAX_BYTES", 8, raising=False)

    app_started = False
    receive_observed_app_started: list[bool] = []
    received_body = bytearray()

    async def app(scope: Scope, receive, send):
        nonlocal app_started
        app_started = True
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            received_body.extend(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": bytes(received_body)})

    middleware = api_main.RequestBodyLimitMiddleware(app)
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/json",
        "headers": [(b"content-length", b"6")],
    }
    messages: list[Message] = [
        {"type": "http.request", "body": b"123", "more_body": True},
        {"type": "http.request", "body": b"456", "more_body": False},
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        receive_observed_app_started.append(app_started)
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope, receive, send)

    assert receive_observed_app_started == [True, True]
    assert received_body == b"123456"
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_body_size_middleware_allows_multipart_upload_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main, "API_REQUEST_BODY_MAX_BYTES", 8, raising=False)
    app = FastAPI()
    app.add_middleware(api_main.RequestBodyLimitMiddleware)

    @app.post("/upload/file")
    async def upload_route():
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/upload/file",
            files={"file": ("large.txt", b"x" * 1024, "text/plain")},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}

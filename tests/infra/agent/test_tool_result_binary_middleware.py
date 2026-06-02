import base64
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from src.infra.agent.middleware.tool_interception import ToolResultBinaryMiddleware
from src.infra.storage.s3.types import UploadResult


class FakeStorage:
    async def upload_file(
        self,
        file,
        folder: str,
        filename: str,
        content_type: str,
        skip_size_limit: bool = False,
    ):
        assert file.read() == b"fake-image"
        assert folder == "tool_binaries"
        assert content_type == "image/png"
        assert skip_size_limit is True
        return UploadResult(
            key=f"{folder}/{filename}",
            url=f"/api/upload/file/{folder}/{filename}",
            size=len(b"fake-image"),
            content_type=content_type,
        )


@pytest.mark.asyncio
async def test_binary_middleware_rewrites_mcp_image_blocks_to_llm_safe_json(monkeypatch):
    async def fake_get_or_init_storage():
        return FakeStorage()

    monkeypatch.setattr(
        "src.infra.storage.s3.service.get_or_init_storage",
        fake_get_or_init_storage,
    )

    middleware = ToolResultBinaryMiddleware(base_url="https://app.example.com")
    b64_image = base64.b64encode(b"fake-image").decode("ascii")
    request = SimpleNamespace(tool_call={"name": "chart", "id": "call-1", "args": {}})

    async def handler(_request):
        return ToolMessage(
            content=[
                {"type": "text", "text": "generated chart"},
                {"type": "image", "mime_type": "image/png", "base64": b64_image},
            ],
            tool_call_id="call-1",
            name="chart",
        )

    result = await middleware.awrap_tool_call(request, handler)

    assert isinstance(result.content, str)
    assert "generated chart" in result.content
    assert "https://app.example.com/api/upload/file/tool_binaries/" in result.content
    assert '"type": "image"' in result.content
    assert "base64" not in result.content


@pytest.mark.asyncio
async def test_binary_middleware_redacts_inline_base64_when_upload_fails(monkeypatch):
    async def fake_get_or_init_storage():
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(
        "src.infra.storage.s3.service.get_or_init_storage",
        fake_get_or_init_storage,
    )

    middleware = ToolResultBinaryMiddleware(base_url="https://app.example.com")
    b64_image = base64.b64encode(b"fake-image").decode("ascii")
    request = SimpleNamespace(tool_call={"name": "chart", "id": "call-1", "args": {}})

    async def handler(_request):
        return ToolMessage(
            content=[
                {"type": "text", "text": "generated chart"},
                {"type": "image", "mime_type": "image/png", "base64": b64_image},
            ],
            tool_call_id="call-1",
            name="chart",
        )

    result = await middleware.awrap_tool_call(request, handler)

    payload = json.loads(result.content)
    assert payload["text"] == "generated chart"
    assert payload["blocks"] == [
        {
            "type": "image",
            "mime_type": "image/png",
            "upload_error": "binary_upload_failed",
        }
    ]
    assert "base64" not in result.content
    assert b64_image not in result.content


@pytest.mark.asyncio
async def test_binary_middleware_redacts_blocks_over_batch_count_limit(monkeypatch):
    upload_calls = 0

    class CountingStorage:
        async def upload_file(
            self,
            file,
            folder: str,
            filename: str,
            content_type: str,
            skip_size_limit: bool = False,
        ):
            nonlocal upload_calls
            upload_calls += 1
            assert file.read() in (b"first-image", b"second-image")
            return UploadResult(
                key=f"{folder}/{filename}",
                url=f"/api/upload/file/{folder}/{filename}",
                size=1,
                content_type=content_type,
            )

    async def fake_get_or_init_storage():
        return CountingStorage()

    monkeypatch.setattr(
        "src.infra.storage.s3.service.get_or_init_storage",
        fake_get_or_init_storage,
    )
    monkeypatch.setattr(
        "src.infra.agent.middleware.tool_interception._BINARY_BLOCK_UPLOAD_MAX_BLOCKS",
        1,
        raising=False,
    )

    middleware = ToolResultBinaryMiddleware(base_url="https://app.example.com")
    request = SimpleNamespace(tool_call={"name": "chart", "id": "call-1", "args": {}})
    first_b64 = base64.b64encode(b"first-image").decode("ascii")
    second_b64 = base64.b64encode(b"second-image").decode("ascii")

    async def handler(_request):
        return ToolMessage(
            content=[
                {"type": "image", "mime_type": "image/png", "base64": first_b64},
                {"type": "image", "mime_type": "image/jpeg", "base64": second_b64},
            ],
            tool_call_id="call-1",
            name="chart",
        )

    result = await middleware.awrap_tool_call(request, handler)
    payload = json.loads(result.content)

    assert upload_calls == 1
    assert "base64" not in result.content
    assert payload["blocks"][0]["url"].startswith(
        "https://app.example.com/api/upload/file/tool_binaries/"
    )
    assert payload["blocks"][1] == {
        "type": "image",
        "mime_type": "image/jpeg",
        "upload_error": "binary_upload_too_many_blocks",
    }


@pytest.mark.asyncio
async def test_binary_middleware_redacts_blocks_over_batch_byte_limit(monkeypatch):
    upload_calls = 0

    class CountingStorage:
        async def upload_file(
            self,
            file,
            folder: str,
            filename: str,
            content_type: str,
            skip_size_limit: bool = False,
        ):
            nonlocal upload_calls
            upload_calls += 1
            assert file.read() == b"first"
            return UploadResult(
                key=f"{folder}/{filename}",
                url=f"/api/upload/file/{folder}/{filename}",
                size=1,
                content_type=content_type,
            )

    async def fake_get_or_init_storage():
        return CountingStorage()

    monkeypatch.setattr(
        "src.infra.storage.s3.service.get_or_init_storage",
        fake_get_or_init_storage,
    )
    monkeypatch.setattr(
        "src.infra.agent.middleware.tool_interception._BINARY_BLOCK_UPLOAD_TOTAL_MAX_BYTES",
        8,
        raising=False,
    )

    middleware = ToolResultBinaryMiddleware(base_url="https://app.example.com")
    request = SimpleNamespace(tool_call={"name": "chart", "id": "call-1", "args": {}})

    async def handler(_request):
        return ToolMessage(
            content=[
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "base64": base64.b64encode(b"first").decode("ascii"),
                },
                {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "base64": base64.b64encode(b"second").decode("ascii"),
                },
            ],
            tool_call_id="call-1",
            name="chart",
        )

    result = await middleware.awrap_tool_call(request, handler)
    payload = json.loads(result.content)

    assert upload_calls == 1
    assert "base64" not in result.content
    assert payload["blocks"][1] == {
        "type": "image",
        "mime_type": "image/jpeg",
        "upload_error": "binary_upload_too_large",
    }

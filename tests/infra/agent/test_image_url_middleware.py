from langchain_core.messages import HumanMessage

from src.infra.agent.middleware.image_url import ImageUrlToBase64Middleware


async def test_image_url_middleware_converts_model_request_blocks(monkeypatch):
    async def fake_download(url, mime_type, *, max_bytes):
        assert url == "https://app.example.com/api/upload/file/uploads/img.png"
        assert mime_type == "image/png"
        assert max_bytes > 0
        return "data:image/png;base64,aW1hZ2U="

    monkeypatch.setattr(
        "src.infra.agent.middleware.image_url._download_image_url_as_data_url",
        fake_download,
    )

    class Request:
        def __init__(self, messages):
            self.messages = messages

        def override(self, **kwargs):
            return Request(kwargs.get("messages", self.messages))

    seen = {}

    async def handler(request):
        seen["request"] = request
        return request

    middleware = ImageUrlToBase64Middleware()
    message = HumanMessage(
        content=[
            {"type": "text", "text": "what is this?"},
            {
                "type": "image_url",
                "image_url": {
                    "url": "https://app.example.com/api/upload/file/uploads/img.png",
                    "mime_type": "image/png",
                },
            },
        ]
    )

    await middleware.awrap_model_call(Request([message]), handler)

    converted = seen["request"].messages[0]
    assert converted is not message
    assert converted.content[1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="

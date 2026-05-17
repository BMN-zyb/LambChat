from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from src.infra.channel.feishu import sender as feishu_sender
from src.infra.channel.feishu.sender import FeishuSenderMixin


class _DummySender(FeishuSenderMixin):
    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self.config = SimpleNamespace(
            app_id="app-id",
            app_secret="app-secret",
            user_id="user-1",
        )


class _FakeHttpResponse:
    is_error = False
    status_code = 200
    text = ""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def test_feishu_rest_calls_reuse_one_async_client(monkeypatch: pytest.MonkeyPatch) -> None:
    created_clients = 0

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal created_clients
            created_clients += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, *_args: Any, **_kwargs: Any) -> _FakeHttpResponse:
            return _FakeHttpResponse(
                {"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}
            )

        async def request(self, *_args: Any, **_kwargs: Any) -> _FakeHttpResponse:
            return _FakeHttpResponse({"code": 0, "data": {"ok": True}})

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(feishu_sender.httpx, "AsyncClient", _FakeAsyncClient)

    dummy = _DummySender()

    async def _run() -> None:
        assert await dummy._feishu_json("GET", "/one") == {"code": 0, "data": {"ok": True}}
        assert await dummy._feishu_json("GET", "/two") == {"code": 0, "data": {"ok": True}}

    import asyncio

    asyncio.run(_run())

    assert created_clients == 1


class _Builder:
    def __getattr__(self, _name: str):
        def _method(*_args: Any, **_kwargs: Any):
            return self

        return _method

    def build(self):
        return object()


class _Response:
    def __init__(self, code: int, success: bool) -> None:
        self.code = code
        self.msg = f"msg-{code}"
        self.data = SimpleNamespace(message_id=f"message-{code}")
        self._success = success

    def success(self) -> bool:
        return self._success


class _MessageApi:
    def __init__(self, reply_code: int) -> None:
        self.reply_code = reply_code
        self.create_calls = 0

    def reply(self, _request: Any) -> _Response:
        return _Response(self.reply_code, False)

    def create(self, _request: Any) -> _Response:
        self.create_calls += 1
        return _Response(0, True)


def _install_fake_lark_im_module(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("lark_oapi.api.im.v1")
    for name in (
        "CreateMessageRequest",
        "CreateMessageRequestBody",
        "ReplyMessageRequest",
        "ReplyMessageRequestBody",
    ):
        setattr(module, name, type(name, (), {"builder": staticmethod(lambda: _Builder())}))

    monkeypatch.setitem(sys.modules, "lark_oapi", ModuleType("lark_oapi"))
    monkeypatch.setitem(sys.modules, "lark_oapi.api", ModuleType("lark_oapi.api"))
    monkeypatch.setitem(sys.modules, "lark_oapi.api.im", ModuleType("lark_oapi.api.im"))
    monkeypatch.setitem(sys.modules, "lark_oapi.api.im.v1", module)


def test_card_reply_does_not_fallback_for_unexpected_reply_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_lark_im_module(monkeypatch)
    message_api = _MessageApi(reply_code=999999)
    dummy = _DummySender(
        SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
    )

    assert dummy._send_card_message_sync("chat_id", "oc_1", "{}", "om_1") == (False, None)
    assert message_api.create_calls == 0


def test_card_reply_falls_back_for_withdrawn_original_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_lark_im_module(monkeypatch)
    message_api = _MessageApi(reply_code=230011)
    dummy = _DummySender(
        SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))
    )

    assert dummy._send_card_message_sync("chat_id", "oc_1", "{}", "om_1") == (
        True,
        "message-0",
    )
    assert message_api.create_calls == 1


def test_send_file_by_key_passes_reply_to_id_to_file_sender() -> None:
    class _CaptureFileSender(_DummySender):
        def __init__(self) -> None:
            super().__init__(client=object())
            self.calls: list[tuple[str, str, str, str, str | None]] = []

        def _send_file_message_sync(
            self,
            chat_id: str,
            file_key: str,
            file_name: str,
            msg_type: str = "file",
            reply_to_id: str | None = None,
        ) -> bool:
            self.calls.append((chat_id, file_key, file_name, msg_type, reply_to_id))
            return True

    dummy = _CaptureFileSender()

    import asyncio

    assert asyncio.run(
        dummy.send_file_by_key(
            chat_id="oc_chat",
            file_key="file-key",
            file_name="doc.md",
            reply_to_id="om_original",
        )
    )
    assert dummy.calls == [
        ("oc_chat", "file-key", "doc.md", "file", "om_original"),
    ]

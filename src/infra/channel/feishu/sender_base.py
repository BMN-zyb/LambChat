"""Shared Feishu sender HTTP helpers and streaming card operations."""

import json
import time
from collections import OrderedDict
from typing import Any

import httpx

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger

logger = get_logger(__name__)


class FeishuBaseSenderMixin:
    """Mixin providing message sending, file transfer, and card operations for FeishuChannel.

    Requires the host class to provide:
        - self._client: The lark SDK client instance
        - self.config.user_id: For logging purposes
    """

    _client: Any
    config: Any
    _chat_mode_cache: OrderedDict
    _feishu_http_client: httpx.AsyncClient | None = None

    _FILE_TYPE_MAP = {
        ".opus": "opus",
        ".mp4": "mp4",
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "doc",
        ".xls": "xls",
        ".xlsx": "xls",
        ".ppt": "ppt",
        ".pptx": "ppt",
    }
    _FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
    _REPLY_FALLBACK_ERROR_CODES = {230011}
    _tenant_access_token: str | None = None
    _tenant_access_token_expires_at: float = 0.0

    def _get_feishu_http_client(self) -> httpx.AsyncClient:
        client: httpx.AsyncClient | None = getattr(self, "_feishu_http_client", None)
        if client is None or getattr(client, "is_closed", False):
            client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
            self._feishu_http_client = client
        return client

    async def close_feishu_http_client(self) -> None:
        client = getattr(self, "_feishu_http_client", None)
        if client is not None:
            await client.aclose()
            self._feishu_http_client = None

    def _resolve_receive_id(self, chat_id: str) -> tuple[str, str]:
        """Return Feishu receive_id_type and receive_id, stripping local thread suffixes."""
        receive_id = chat_id.split("#", 1)[0]
        receive_id_type = "chat_id" if receive_id.startswith("oc_") else "open_id"
        return receive_id_type, receive_id

    async def _get_tenant_access_token(self) -> str | None:
        """Fetch and cache tenant_access_token for CardKit REST APIs."""
        now = time.time()
        if self._tenant_access_token and now < self._tenant_access_token_expires_at:
            return self._tenant_access_token

        app_id = getattr(self.config, "app_id", "")
        app_secret = getattr(self.config, "app_secret", "")
        if not app_id or not app_secret:
            return None

        try:
            client = self._get_feishu_http_client()
            response = await client.post(
                f"{self._FEISHU_API_BASE}/auth/v3/tenant_access_token/internal/",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            logger.warning("[Feishu] Failed to fetch tenant_access_token: %s", e)
            return None

        if payload.get("code") != 0:
            logger.warning(
                "[Feishu] tenant_access_token failed: code=%s msg=%s",
                payload.get("code"),
                payload.get("msg"),
            )
            return None

        token = payload.get("tenant_access_token")
        if not token:
            return None
        expire = int(payload.get("expire", 7200))
        self._tenant_access_token = token
        self._tenant_access_token_expires_at = now + max(expire - 300, 60)
        return token

    async def _feishu_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        token = await self._get_tenant_access_token()
        if not token:
            return None
        try:
            client = self._get_feishu_http_client()
            request_kwargs: dict[str, Any] = {
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                "params": params,
            }
            if json_body is not None:
                request_kwargs["content"] = await run_blocking_io(
                    json.dumps,
                    json_body,
                    ensure_ascii=False,
                )
            response = await client.request(
                method,
                f"{self._FEISHU_API_BASE}{path}",
                **request_kwargs,
            )
            try:
                payload = response.json()
            except ValueError:
                payload = {"code": response.status_code, "msg": response.text}
            if response.is_error:
                logger.warning(
                    "[Feishu] REST %s %s failed: status=%s payload=%s",
                    method,
                    path,
                    response.status_code,
                    payload,
                )
            return payload
        except Exception as e:
            logger.warning("[Feishu] REST %s %s failed: %s", method, path, e)
            return None

    def _build_stream_card_json(self, content: str, streaming: bool = True) -> str:
        preview = content.strip().replace("\n", " ")[:30] or (
            "正在生成回复..." if streaming else " "
        )
        return json.dumps(
            {
                "schema": "2.0",
                "config": {
                    "streaming_mode": streaming,
                    "summary": {"content": preview},
                    "streaming_config": {
                        "print_frequency_ms": {"default": 40},
                        "print_step": {"default": 4},
                        "print_strategy": "fast",
                    },
                },
                "body": {
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": content or "...",
                            "element_id": "stream_md",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )

    async def create_stream_card(self, initial_text: str = "...") -> str | None:
        card_json = await run_blocking_io(self._build_stream_card_json, initial_text)
        payload = await self._feishu_json(
            "POST",
            "/cardkit/v1/cards",
            json_body={"type": "card_json", "data": card_json},
        )
        if not payload or payload.get("code") != 0:
            logger.warning("[Feishu] Create stream card failed: %s", payload)
            return None
        return (payload.get("data") or {}).get("card_id")

    async def send_card_by_id(
        self,
        chat_id: str,
        card_id: str,
        *,
        reply_to_id: str | None = None,
    ) -> tuple[bool, str | None]:
        content = await run_blocking_io(
            json.dumps,
            {"type": "card", "data": {"card_id": card_id}},
            ensure_ascii=False,
        )
        if reply_to_id:
            payload = await self._feishu_json(
                "POST",
                f"/im/v1/messages/{reply_to_id}/reply",
                json_body={"msg_type": "interactive", "content": content},
            )
            if payload and payload.get("code") == 0:
                data = payload.get("data") or {}
                return True, data.get("message_id")
            reply_code = payload.get("code") if payload else None
            logger.warning("[Feishu] Reply stream card failed: %s", payload)
            if reply_code not in self._REPLY_FALLBACK_ERROR_CODES:
                return False, None
            logger.info(
                "[Feishu] Falling back to create stream card after reply code=%s",
                reply_code,
            )

        receive_id_type, receive_id = self._resolve_receive_id(chat_id)
        payload = await self._feishu_json(
            "POST",
            "/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_body={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": content,
            },
        )
        if not payload or payload.get("code") != 0:
            logger.warning(
                "[Feishu] Send stream card failed: receive_id_type=%s receive_id=%s payload=%s",
                receive_id_type,
                receive_id,
                payload,
            )
            return False, None
        data = payload.get("data") or {}
        return True, data.get("message_id")

    async def update_stream_card(self, card_id: str, content: str, sequence: int) -> bool:
        payload = await self._feishu_json(
            "PUT",
            f"/cardkit/v1/cards/{card_id}/elements/stream_md/content",
            json_body={"content": content or " ", "sequence": sequence},
        )
        if not payload or payload.get("code") != 0:
            logger.warning("[Feishu] Update stream card failed: %s", payload)
            return False
        return True

    async def finalize_stream_card(self, card_id: str, content: str, sequence: int) -> bool:
        card_json = await run_blocking_io(
            self._build_stream_card_json,
            content or " ",
            streaming=False,
        )
        payload = await self._feishu_json(
            "PUT",
            f"/cardkit/v1/cards/{card_id}",
            json_body={
                "card": {
                    "type": "card_json",
                    "data": card_json,
                },
                "sequence": sequence,
            },
        )
        if not payload or payload.get("code") != 0:
            logger.warning("[Feishu] Finalize stream card failed: %s", payload)
            return False
        return True

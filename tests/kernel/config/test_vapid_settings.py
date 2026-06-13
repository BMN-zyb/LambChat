from __future__ import annotations

import base64

from py_vapid import Vapid

from src.kernel.config.base import Settings
from src.kernel.config.definitions import SETTING_DEFINITIONS


def _base64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode((value + padding).encode())


def test_generated_vapid_keys_are_valid_for_browser_and_pywebpush() -> None:
    settings = Settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")

    public_key_bytes = _base64url_decode(settings.VAPID_PUBLIC_KEY)

    assert settings._vapid_keys_generated is True
    assert len(public_key_bytes) == 65
    assert public_key_bytes[0] == 4
    assert Vapid.from_string(settings.VAPID_PRIVATE_KEY).private_key is not None


def test_vapid_settings_are_registered_for_runtime_storage() -> None:
    assert SETTING_DEFINITIONS["VAPID_PUBLIC_KEY"]["default"] == ""
    assert SETTING_DEFINITIONS["VAPID_PRIVATE_KEY"]["is_sensitive"] is True
    assert SETTING_DEFINITIONS["VAPID_SUBJECT"]["default"] == "mailto:admin@example.com"

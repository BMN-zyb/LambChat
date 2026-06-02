from __future__ import annotations

from src.kernel.config.base import Settings
from src.kernel.config.definitions import SETTING_DEFINITIONS


def test_feishu_upload_bytes_limit_default_matches_setting_definition() -> None:
    definition = SETTING_DEFINITIONS["FEISHU_UPLOAD_BYTES_MAX_SIZE"]

    assert Settings().FEISHU_UPLOAD_BYTES_MAX_SIZE == 20 * 1024 * 1024
    assert definition["default"] == 20 * 1024 * 1024
    assert definition.get("frontend_visible", False) is False

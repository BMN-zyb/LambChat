from __future__ import annotations

from src.kernel.config.base import Settings
from src.kernel.config.definitions import SETTING_DEFINITIONS


def test_mongodb_store_batch_concurrency_default_matches_definition() -> None:
    definition = SETTING_DEFINITIONS["MONGODB_STORE_BATCH_CONCURRENCY"]

    assert Settings(_env_file=None).MONGODB_STORE_BATCH_CONCURRENCY == 16
    assert definition["default"] == 16
    assert definition.get("frontend_visible", False) is False

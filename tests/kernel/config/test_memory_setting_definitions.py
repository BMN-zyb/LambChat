from __future__ import annotations

from src.kernel.config.base import Settings
from src.kernel.config.definitions import SETTING_DEFINITIONS


def test_memory_auto_capture_task_limit_default_matches_definition() -> None:
    definition = SETTING_DEFINITIONS["NATIVE_MEMORY_AUTO_CAPTURE_MAX_TASKS"]

    assert Settings(_env_file=None).NATIVE_MEMORY_AUTO_CAPTURE_MAX_TASKS == 8
    assert definition["default"] == 8
    assert definition.get("frontend_visible", False) is False


def test_memory_consolidation_input_limit_default_matches_definition() -> None:
    definition = SETTING_DEFINITIONS["NATIVE_MEMORY_CONSOLIDATION_INPUT_MAX_CHARS"]

    assert Settings(_env_file=None).NATIVE_MEMORY_CONSOLIDATION_INPUT_MAX_CHARS == 4000
    assert definition["default"] == 4000
    assert definition.get("frontend_visible", False) is False

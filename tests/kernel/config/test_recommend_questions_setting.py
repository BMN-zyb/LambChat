from __future__ import annotations

from src.kernel.config.base import Settings
from src.kernel.config.definitions import SETTING_DEFINITIONS, SettingCategory, SettingType


def test_recommend_questions_is_admin_session_setting_enabled_by_default() -> None:
    definition = SETTING_DEFINITIONS["ENABLE_RECOMMEND_QUESTIONS"]

    assert definition["type"] == SettingType.BOOLEAN
    assert definition["category"] == SettingCategory.SESSION
    assert definition["subcategory"] == "recommendations"
    assert definition["default"] is True
    assert definition.get("frontend_visible", False) is False


def test_recommend_questions_background_task_limit_is_hidden_session_setting() -> None:
    definition = SETTING_DEFINITIONS["RECOMMEND_QUESTIONS_MAX_BACKGROUND_TASKS"]

    assert Settings(_env_file=None).RECOMMEND_QUESTIONS_MAX_BACKGROUND_TASKS == 8
    assert definition["type"] == SettingType.NUMBER
    assert definition["category"] == SettingCategory.SESSION
    assert definition["subcategory"] == "recommendations"
    assert definition["default"] == 8
    assert definition.get("frontend_visible", False) is False

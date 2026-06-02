from __future__ import annotations

from src.kernel.config.base import Settings
from src.kernel.config.definitions import SETTING_DEFINITIONS


def test_task_backend_default_matches_settings_definition() -> None:
    assert Settings().TASK_BACKEND == SETTING_DEFINITIONS["TASK_BACKEND"]["default"]


def test_arq_worker_max_jobs_is_admin_setting_not_frontend_visible() -> None:
    definition = SETTING_DEFINITIONS["ARQ_WORKER_MAX_JOBS"]

    assert Settings().ARQ_WORKER_MAX_JOBS == 2
    assert definition["default"] == 2
    assert definition.get("frontend_visible", False) is False


def test_arq_job_timeout_defaults_to_24_hours() -> None:
    definition = SETTING_DEFINITIONS["ARQ_JOB_TIMEOUT_SECONDS"]

    assert definition["default"] == 24 * 60 * 60

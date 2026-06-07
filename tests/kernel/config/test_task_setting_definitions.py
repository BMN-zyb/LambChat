from __future__ import annotations

from src.kernel.config.base import Settings
from src.kernel.config.definitions import SETTING_DEFINITIONS


def test_task_backend_default_matches_settings_definition() -> None:
    assert Settings().TASK_BACKEND == SETTING_DEFINITIONS["TASK_BACKEND"]["default"]


def test_arq_worker_max_jobs_is_admin_setting_not_frontend_visible() -> None:
    definition = SETTING_DEFINITIONS["ARQ_WORKER_MAX_JOBS"]

    assert Settings().ARQ_WORKER_MAX_JOBS == 128
    assert definition["default"] == 128
    assert definition.get("frontend_visible", False) is False


def test_arq_job_timeout_defaults_to_24_hours() -> None:
    definition = SETTING_DEFINITIONS["ARQ_JOB_TIMEOUT_SECONDS"]

    assert definition["default"] == 24 * 60 * 60


def test_arq_settings_depend_on_arq_task_backend() -> None:
    arq_setting_keys = [
        "ARQ_EMBEDDED_WORKER",
        "ARQ_QUEUE_NAME",
        "ARQ_WORKER_MAX_JOBS",
        "ARQ_JOB_TIMEOUT_SECONDS",
        "TASK_STARTUP_CLEANUP_CONCURRENCY",
    ]

    for key in arq_setting_keys:
        assert SETTING_DEFINITIONS[key]["depends_on"] == {
            "key": "TASK_BACKEND",
            "value": "arq",
        }

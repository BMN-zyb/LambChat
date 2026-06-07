"""
Runtime service orchestration for distributed listeners.

Centralizes startup/shutdown of lightweight process-local listeners that
coordinate through shared Redis/Mongo infrastructure.
"""

from __future__ import annotations

from src.agents.core.recommendations import drain_recommend_background_tasks
from src.infra.async_utils import shutdown_blocking_io_executor
from src.infra.channel.pubsub import get_channel_config_pubsub
from src.infra.llm.pubsub import get_model_config_pubsub
from src.infra.monitoring.event_loop import (
    start_event_loop_lag_monitor,
    stop_event_loop_lag_monitor,
)
from src.infra.scheduler import ScheduledJob, get_runtime_scheduler
from src.infra.scheduler.service import ScheduledTaskService
from src.infra.scheduler.storage import get_scheduled_task_storage
from src.infra.settings.pubsub import get_settings_pubsub
from src.infra.task.arq_runtime import start_arq_runtime, stop_arq_runtime
from src.infra.task.manager import get_task_manager
from src.infra.tool.cache_pubsub import get_tool_cache_pubsub
from src.infra.tool.mcp_cache import drain_background_tasks as drain_mcp_cache_background_tasks
from src.infra.tool.mcp_global import (
    drain_background_tasks as drain_mcp_global_background_tasks,
)
from src.infra.tool.mcp_global import get_mcp_cache_pubsub
from src.infra.tool.mcp_pool import close_all_connections as close_mcp_pool_connections
from src.infra.websocket import get_connection_manager
from src.kernel.config import settings


def get_memory_pubsub():
    from src.infra.memory.distributed import get_memory_pubsub

    return get_memory_pubsub()


async def memory_shutdown() -> None:
    from src.infra.memory.tools import shutdown

    await shutdown()


async def drain_dual_writer_event_buffer() -> None:
    from src.infra.session.dual_writer import get_dual_writer

    await get_dual_writer().flush_mongo_buffer()


async def drain_upload_delete_tasks() -> None:
    from src.api.routes.upload import drain_upload_delete_tasks as _drain_upload_delete_tasks

    await _drain_upload_delete_tasks()


async def drain_user_s3_cleanup_tasks() -> None:
    from src.infra.user.manager import drain_s3_cleanup_tasks

    await drain_s3_cleanup_tasks()


async def drain_project_cleanup_tasks() -> None:
    from src.infra.tool.reveal_project_tool import drain_project_cleanup_tasks as _drain

    await _drain()


async def drain_llm_client_close_tasks() -> None:
    from src.infra.llm.client import LLMClient

    await LLMClient.drain_close_tasks()


def start_memory_compaction_agent() -> None:
    from src.infra.memory.tools import start_memory_compaction_agent

    start_memory_compaction_agent()


def register_scheduled_task_reconcile_job(
    scheduled_task_service: ScheduledTaskService,
) -> None:
    """Keep process-local scheduled jobs in sync with MongoDB in multi-instance runs."""
    get_runtime_scheduler().register_job(
        ScheduledJob.from_interval(
            id="scheduled_tasks.reconcile",
            interval_seconds=30,
            handler=scheduled_task_service.load_persisted_tasks,
            name="Scheduled task reconcile",
            max_instances=1,
            coalesce=True,
        )
    )


async def start_runtime_services() -> None:
    """Start distributed runtime listeners needed by the current process."""
    import asyncio

    await start_event_loop_lag_monitor()

    task_manager = get_task_manager()
    await task_manager.start_pubsub_listener()
    await start_arq_runtime()

    # Launch all pub/sub listeners concurrently to reduce startup wall-clock time.
    settings_pubsub = get_settings_pubsub()
    model_config_pubsub = get_model_config_pubsub()
    channel_pubsub = get_channel_config_pubsub()
    tool_cache_pubsub = get_tool_cache_pubsub()
    mcp_cache_pubsub = get_mcp_cache_pubsub()
    websocket_manager = get_connection_manager()

    listeners = [
        settings_pubsub.start_listener(),
        model_config_pubsub.start_listener(),
        channel_pubsub.start_listener(),
        tool_cache_pubsub.start_listener(),
        mcp_cache_pubsub.start_listener(),
        websocket_manager.start_pubsub_listener(),
    ]
    if settings.ENABLE_MEMORY:
        listeners.append(get_memory_pubsub().start_listener())

    await asyncio.gather(
        *listeners,
    )

    if settings.ENABLE_MEMORY:
        start_memory_compaction_agent()

    # Load dynamically-created scheduled tasks from DB
    await get_scheduled_task_storage().ensure_indexes()
    scheduled_task_service = ScheduledTaskService()
    await scheduled_task_service.load_persisted_tasks()
    register_scheduled_task_reconcile_job(scheduled_task_service)

    get_runtime_scheduler().start()


async def stop_runtime_services() -> None:
    """Stop distributed runtime listeners in reverse dependency order."""
    await stop_event_loop_lag_monitor()

    # Close debug log file handle to prevent FD leak
    try:
        from src.infra.agent.events.debug_logger import shutdown as debug_logger_shutdown

        debug_logger_shutdown()
    except Exception:
        pass

    websocket_manager = get_connection_manager()
    await websocket_manager.stop_pubsub_listener()

    mcp_cache_pubsub = get_mcp_cache_pubsub()
    await mcp_cache_pubsub.stop_listener()
    await drain_mcp_cache_background_tasks()
    await drain_mcp_global_background_tasks()
    await drain_recommend_background_tasks()
    await close_mcp_pool_connections()

    tool_cache_pubsub = get_tool_cache_pubsub()
    await tool_cache_pubsub.stop_listener()

    channel_pubsub = get_channel_config_pubsub()
    await channel_pubsub.stop_listener()

    await get_runtime_scheduler().stop()

    if settings.ENABLE_MEMORY:
        memory_pubsub = get_memory_pubsub()
        await memory_pubsub.stop_listener()
        await memory_shutdown()

    model_config_pubsub = get_model_config_pubsub()
    await model_config_pubsub.stop_listener()

    settings_pubsub = get_settings_pubsub()
    await settings_pubsub.stop_listener()

    task_manager = get_task_manager()
    await stop_arq_runtime()
    await task_manager.stop_pubsub_listener()
    await drain_upload_delete_tasks()
    await drain_user_s3_cleanup_tasks()
    await drain_project_cleanup_tasks()
    await drain_llm_client_close_tasks()
    await drain_dual_writer_event_buffer()
    shutdown_blocking_io_executor()

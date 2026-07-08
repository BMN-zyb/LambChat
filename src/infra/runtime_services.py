"""
Runtime service orchestration for distributed listeners.

Centralizes startup/shutdown of lightweight process-local listeners that
coordinate through shared Redis/Mongo infrastructure.
"""

from __future__ import annotations

# 下面这一大片 import 汇集了当前进程运行期需要「启动/关闭」的各个基础设施子系统：
# 阻塞 IO 线程池、各类 Redis pub/sub 多路复用中枢（频道配置、模型配置、工具缓存、
# MCP 缓存等）、事件循环延迟监控、调度器（scheduler）、arq 任务运行时、WebSocket
# 连接管理器等。本模块的职责就是把它们的生命周期集中编排起来（见文件底部的
# start_runtime_services / stop_runtime_services）。
from src.agents.core.recommendations import drain_recommend_background_tasks
from src.infra.async_utils import shutdown_blocking_io_executor
from src.infra.channel.pubsub import close_channel_config_pubsub, get_channel_config_pubsub
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
from src.infra.tool.cache_pubsub import (
    close_tool_cache_pubsub,
    get_tool_cache_pubsub,
)
from src.infra.tool.mcp_cache import drain_background_tasks as drain_mcp_cache_background_tasks
from src.infra.tool.mcp_global import (
    close_global_mcp_cache,
    close_mcp_cache_pubsub,
    get_mcp_cache_pubsub,
)
from src.infra.tool.mcp_global import (
    drain_background_tasks as drain_mcp_global_background_tasks,
)
from src.infra.tool.mcp_pool import close_all_connections as close_mcp_pool_connections
from src.infra.websocket import get_connection_manager
from src.kernel.config import settings


# 下面这一组小函数都是「延迟导入（lazy import）」包装器：把 import 推迟到函数被调用时
# 才执行，既避免了模块加载期的循环依赖，也避免过早加载重量级依赖。它们统一在
# stop_runtime_services 中按序调用，用于关闭连接 / 排空后台任务缓冲区。
def get_memory_pubsub():
    # 返回记忆(memory)子系统的分布式 pub/sub 单例，用于跨实例的记忆缓存失效广播。
    from src.infra.memory.distributed import get_memory_pubsub

    return get_memory_pubsub()


async def close_memory_pubsub() -> None:
    # 关闭记忆子系统的 pub/sub 连接（进程退出时释放 Redis 订阅资源）。
    from src.infra.memory.distributed import close_memory_pubsub as _close_memory_pubsub

    await _close_memory_pubsub()


async def memory_shutdown() -> None:
    # 关闭记忆工具链（例如后台压缩 agent、内部缓存等）。
    from src.infra.memory.tools import shutdown

    await shutdown()


async def drain_dual_writer_event_buffer() -> None:
    # 排空并关闭「双写器(dual writer)」：确保待落库的会话事件缓冲区在退出前全部刷写。
    from src.infra.session.dual_writer import close_dual_writer

    await close_dual_writer()


async def drain_upload_delete_tasks() -> None:
    # 等待所有「上传文件删除」后台任务执行完毕，避免进程退出时丢失清理动作。
    from src.api.routes.upload import drain_upload_delete_tasks as _drain_upload_delete_tasks

    await _drain_upload_delete_tasks()


async def drain_user_s3_cleanup_tasks() -> None:
    # 等待用户级 S3 资源清理后台任务完成。
    from src.infra.user.manager import drain_s3_cleanup_tasks

    await drain_s3_cleanup_tasks()


async def drain_project_cleanup_tasks() -> None:
    # 等待项目（reveal project 工具）相关的清理后台任务完成。
    from src.infra.tool.reveal_project_tool import drain_project_cleanup_tasks as _drain

    await _drain()


async def drain_llm_client_close_tasks() -> None:
    # 关闭 LLM 客户端：先关闭被缓存的模型实例，再等待其异步关闭任务排空。
    from src.infra.llm.client import LLMClient

    LLMClient.close_cached_models()
    await LLMClient.drain_close_tasks()


async def close_role_cache_redis() -> None:
    # 关闭角色(role)缓存所用的 Redis 连接。
    from src.infra.role.storage import close_role_cache_redis as _close_role_cache_redis

    await _close_role_cache_redis()


async def close_channel_manager_instances() -> None:
    # 关闭所有 UserChannelManager 实例（释放各用户频道管理器持有的资源）。
    from src.infra.channel.base import UserChannelManager

    await UserChannelManager.close_all_instances()


async def close_pubsub_hub() -> None:
    # 关闭共享的 pub/sub 中枢（pubsub_hub）：多个订阅者复用的公共 Redis 连接。
    from src.infra.pubsub_hub import close_pubsub_hub as _close_pubsub_hub

    await _close_pubsub_hub()


async def close_ws_rate_limiter() -> None:
    # 关闭 WebSocket 限流器所依赖的 Redis 连接。
    from src.infra.websocket_rate_limiter import close_ws_rate_limiter as _close

    await _close()


async def close_s3_storage() -> None:
    # 关闭 S3 存储服务的底层客户端/连接池。
    from src.infra.storage.s3.service import close_storage

    await close_storage()


async def close_runtime_scheduler() -> None:
    # 关闭运行期调度器，需要按序完成多个收尾动作（见函数体注释）。
    from src.infra.scheduler.runner import drain_detached_monitors
    from src.infra.scheduler.runtime import close_runtime_scheduler as _close_runtime_scheduler
    from src.infra.scheduler.service import clear_managed_task_signatures
    from src.infra.scheduler.storage import close_scheduled_task_storage

    # 1) 先排空「游离监视器」协程；2) 关闭调度器本体；3) 清空进程内已登记的任务签名；
    # 4) 关闭调度任务的持久化存储连接。顺序不能乱，否则可能在存储已关闭后仍有任务访问它。
    await drain_detached_monitors()
    await _close_runtime_scheduler()
    clear_managed_task_signatures()
    close_scheduled_task_storage()


async def cleanup_skills_storage_cache() -> None:
    # 清理技能(skills)存储后端的缓存。
    from src.infra.backend.skills_store import SkillsStoreBackend

    await SkillsStoreBackend.cleanup_storage_cache()


async def close_settings_service() -> None:
    # 关闭全局配置(settings)服务单例；单例可能尚未初始化，故需判空。
    from src.infra.settings.service import SettingsService

    service = SettingsService._instance
    if service is not None:
        await service.close()


def start_memory_compaction_agent() -> None:
    # 启动记忆压缩 agent（后台周期性地压缩/整理长期记忆）。
    from src.infra.memory.tools import start_memory_compaction_agent

    start_memory_compaction_agent()


def register_scheduled_task_reconcile_job(
    scheduled_task_service: ScheduledTaskService,
) -> None:
    """Keep process-local scheduled jobs in sync with MongoDB in multi-instance runs."""
    # 多实例部署时，调度任务的「真相源」在 MongoDB。这里注册一个每 30 秒执行一次的对账
    # (reconcile)任务，把进程内的定时任务与库里的最新状态拉齐。
    # max_instances=1 + coalesce=True 保证即使上一轮还没跑完也不会堆叠并发，只保留一次执行。
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

    # 1) 先拉起事件循环延迟监控，便于观测后续启动/运行期的事件循环健康度。
    await start_event_loop_lag_monitor()

    # 2) 启动任务管理器的 pub/sub 监听 + arq 任务运行时（分布式任务的消费端）。
    task_manager = get_task_manager()
    await task_manager.start_pubsub_listener()
    await start_arq_runtime()

    # Launch all pub/sub listeners concurrently to reduce startup wall-clock time.
    # 3) 取得各配置/缓存子系统的 pub/sub 中枢单例。它们各自订阅一个 Redis 频道，用于在集群内
    #    广播「配置变更 / 缓存失效」事件，从而让每个进程本地缓存保持一致。
    settings_pubsub = get_settings_pubsub()
    model_config_pubsub = get_model_config_pubsub()
    channel_pubsub = get_channel_config_pubsub()
    tool_cache_pubsub = get_tool_cache_pubsub()
    mcp_cache_pubsub = get_mcp_cache_pubsub()
    websocket_manager = get_connection_manager()

    # 把所有监听器的 start 协程收集起来，用 asyncio.gather 并发启动，缩短启动耗时
    # （相比逐个 await，可显著降低启动的挂钟时间）。
    listeners = [
        settings_pubsub.start_listener(),
        model_config_pubsub.start_listener(),
        channel_pubsub.start_listener(),
        tool_cache_pubsub.start_listener(),
        mcp_cache_pubsub.start_listener(),
        websocket_manager.start_pubsub_listener(),
    ]
    # 记忆功能为可选特性：仅在开启时才追加记忆 pub/sub 监听器。
    if settings.ENABLE_MEMORY:
        listeners.append(get_memory_pubsub().start_listener())

    await asyncio.gather(
        *listeners,
    )

    # 4) 监听器就绪后，若开启记忆功能则启动后台的记忆压缩 agent。
    if settings.ENABLE_MEMORY:
        start_memory_compaction_agent()

    if settings.ENABLE_SCHEDULED_TASK:
        # Load dynamically-created scheduled tasks from DB only when the feature is enabled.
        # 5) 仅在启用调度任务特性时：确保索引 -> 从库加载已持久化的任务 -> 注册对账任务 -> 启动调度器。
        await get_scheduled_task_storage().ensure_indexes()
        scheduled_task_service = ScheduledTaskService()
        await scheduled_task_service.load_persisted_tasks()
        register_scheduled_task_reconcile_job(scheduled_task_service)

        get_runtime_scheduler().start()


async def stop_runtime_services() -> None:
    """Stop distributed runtime listeners in reverse dependency order."""
    # 关闭顺序整体遵循「与启动相反的依赖顺序」，先停对外/上层，再停底层连接，最后关线程池，
    # 尽量保证在关闭某资源前不再有代码去访问它。
    await stop_event_loop_lag_monitor()

    # Close debug log file handle to prevent FD leak
    # 关闭调试日志文件句柄，防止文件描述符泄漏；即便该子系统未加载/报错也不影响整体关闭流程。
    try:
        from src.infra.agent.events.debug_logger import shutdown as debug_logger_shutdown

        debug_logger_shutdown()
    except Exception:
        pass

    # 先停 WebSocket 的 pub/sub 监听，切断向客户端推送任务完成通知的分布式路由入口。
    websocket_manager = get_connection_manager()
    await websocket_manager.stop_pubsub_listener()

    # 依次关闭 MCP 缓存 pub/sub、排空其后台任务、关闭全局 MCP 缓存及其后台任务、
    # 排空推荐相关后台任务，最后关闭 MCP 连接池。
    await close_mcp_cache_pubsub()
    await drain_mcp_cache_background_tasks()
    await close_global_mcp_cache()
    await drain_mcp_global_background_tasks()
    await drain_recommend_background_tasks()
    await close_mcp_pool_connections()

    # 关闭工具缓存 pub/sub。
    await close_tool_cache_pubsub()

    # 关闭频道配置 pub/sub，并关闭所有用户频道管理器实例。
    await close_channel_config_pubsub()
    await close_channel_manager_instances()

    # 关闭运行期调度器（含其内部多步收尾）。
    await close_runtime_scheduler()

    # 记忆功能可选：开启时才需关闭记忆 pub/sub 并做记忆工具链的收尾。
    if settings.ENABLE_MEMORY:
        await close_memory_pubsub()
        await memory_shutdown()

    # 关闭模型配置与全局设置的 pub/sub 监听器。
    model_config_pubsub = get_model_config_pubsub()
    await model_config_pubsub.stop_listener()

    settings_pubsub = get_settings_pubsub()
    await settings_pubsub.stop_listener()

    # 停止 arq 运行时与任务管理器监听，然后关闭共享 pub/sub 中枢（pubsub_hub）。
    # pubsub_hub 被多个订阅者复用，必须在依赖它的各监听器都停掉之后再关。
    task_manager = get_task_manager()
    await stop_arq_runtime()
    await task_manager.stop_pubsub_listener()
    await close_pubsub_hub()
    # 排空各类后台清理任务缓冲区，关闭底层连接（S3、Redis、settings 服务、双写器）。
    await drain_upload_delete_tasks()
    await drain_user_s3_cleanup_tasks()
    await drain_project_cleanup_tasks()
    await drain_llm_client_close_tasks()
    await close_role_cache_redis()
    await close_ws_rate_limiter()
    await close_s3_storage()
    await cleanup_skills_storage_cache()
    await close_settings_service()
    await drain_dual_writer_event_buffer()
    # 最后关闭阻塞 IO 线程池：确保前面所有可能用到 run_blocking_io 的收尾逻辑都已执行完毕。
    shutdown_blocking_io_executor()

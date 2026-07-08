"""
FastAPI 主应用

API 入口点。
"""

import asyncio
import warnings
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.middleware.auth import AuthMiddleware
from src.api.middleware.tracing import TracingMiddleware
from src.api.middleware.user_context import UserContextMiddleware
from src.api.routes import (
    agent,
    auth,
    channels,
    chat,
    envvar,
    feedback,
    github,
    health,
    human,
    mcp,
    memory,
    notification,
    persona_preset,
    project,
    push,
    revealed_file,
    role,
    scheduled_task,
    session,
    share,
    skill,
    team,
    upload,
    usage,
    user,
    version,
    websocket,
)
from src.api.routes import settings as settings_router
from src.api.routes.agent import config as agent_config
from src.api.routes.agent import model as agent_model
from src.frontend_resolution import resolve_frontend_target
from src.infra.async_utils import run_blocking_io
from src.infra.distributed_validation import validate_distributed_runtime_settings
from src.infra.local_filesystem import ensure_local_filesystem_dirs
from src.infra.logging import get_logger, setup_logging
from src.infra.monitoring import get_memory_monitor
from src.infra.runtime_services import start_runtime_services, stop_runtime_services
from src.infra.share.seo import (
    build_public_route_seo,
    build_shared_page_error_seo,
    build_shared_page_seo,
    inject_public_route_seo_into_html,
    inject_share_seo_into_html,
)
from src.infra.task.constants import HEARTBEAT_TIMEOUT
from src.kernel.config import initialize_settings, settings

# 屏蔽 oss2 SDK 源码中的无效转义序列告警（第三方库自身问题，与本项目无关）
# Suppress SyntaxWarning from oss2 SDK (invalid escape sequence in their source)
warnings.filterwarnings("ignore", message=".*invalid escape sequence.*", category=SyntaxWarning)

logger = get_logger(__name__)

# 静态资源按路径前缀设置 Cache-Control：
#   assets/ 文件名带内容指纹，可长期强缓存且 immutable（一年）；icons/ 缓存一周；images/ 缓存一周并允许 stale-while-revalidate 后台刷新
STATIC_CACHE_CONTROL_BY_PREFIX = {
    "assets/": "public, max-age=31536000, immutable",
    "icons/": "public, max-age=604800",
    "images/": "public, max-age=604800, stale-while-revalidate=86400",
}
# manifest.json 缓存一天
MANIFEST_CACHE_CONTROL = "public, max-age=86400"
# Service Worker 脚本禁用缓存，保证能及时拉到新版本
SERVICE_WORKER_CACHE_CONTROL = "no-cache"
# 离线兜底页禁用缓存
OFFLINE_PAGE_CACHE_CONTROL = "no-cache"
# index.html 内存缓存（LRU）最多保留的条目数
INDEX_HTML_CACHE_MAX_ENTRIES = 4
# index.html 允许进内存缓存的最大字节数，超过则拒绝缓存（异常保护，避免被超大文件撑爆内存）
INDEX_HTML_MAX_BYTES = 2 * 1024 * 1024
# index.html 的 LRU 内存缓存：key 为解析后的绝对路径，value 为 (mtime_ns, size, HTML 文本)
_INDEX_HTML_CACHE: OrderedDict[Path, tuple[int, int, str]] = OrderedDict()
# 非上传类 API 请求体大小上限：8 MiB，超过直接返回 413 拒绝
API_REQUEST_BODY_MAX_BYTES = 8 * 1024 * 1024
# 豁免请求体大小限制的 multipart 上传路径（文件/头像上传需要允许大体积）
API_MULTIPART_UPLOAD_PATHS = {"/api/upload/file", "/api/upload/avatar", "/upload/file"}
# lifespan 期间登记到 app.state 上的后台任务名，应用关闭时统一按名取消
_LIFESPAN_BACKGROUND_TASK_NAMES = (
    "session_search_backfill_task",
    "memory_monitor_startup_reset_task",
    "agent_discovery_task",
    "models_preload_task",
    "stale_task_cleanup_task",
    "stale_task_cleanup_recheck_task",
    "feishu_task",
)
# 残留任务清理的二次复查延迟：取 max(5 秒, 2×心跳超时+5 秒)，等跨节点 heartbeat 判定稳定后再复查一次，避免误清理
_STALE_TASK_CLEANUP_RECHECK_DELAY_SECONDS = max(5.0, HEARTBEAT_TIMEOUT * 2 + 5)


# 判断某请求是否豁免请求体大小限制：仅当 Content-Type 为 multipart/form-data 且路径命中上传白名单（含技能上传/二进制文件）时豁免
def _is_body_limit_exempt(scope: Scope) -> bool:
    path = str(scope.get("path") or "")

    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }
    content_type = headers.get("content-type", "").lower()
    if "multipart/form-data" not in content_type:
        return False
    return (
        path in API_MULTIPART_UPLOAD_PATHS
        or path.startswith("/api/skills/upload")
        or (path.startswith("/api/skills/") and "/binary-files/" in path)
    )


# 请求体大小限制中间件（纯 ASGI 中间件，非 BaseHTTPMiddleware）。
# 在中间件链中作为最外层、最先执行，从而在请求体被下游路由/框架完全读入内存「之前」就拦下超大请求，避免 OOM 风险。
class RequestBodyLimitMiddleware:
    """Reject oversized non-upload request bodies before they are materialized."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 非 HTTP 请求（WebSocket、lifespan 事件等）或命中上传豁免的请求直接透传，不做体积限制
        if scope["type"] != "http" or _is_body_limit_exempt(scope):
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        # 快路径：若带有可信的 Content-Length 头，直接据此判定，无需读取请求体
        content_length = headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > API_REQUEST_BODY_MAX_BYTES:
                    await self._send_too_large(scope, receive, send)
                    return
                await self.app(scope, receive, send)
                return
            except ValueError:
                pass

        # 慢路径：无 Content-Length（如 chunked 传输）时，边接收边累加，一旦超限立即拒绝
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, receive, send)
                return

            body.extend(message.get("body", b""))
            if len(body) > API_REQUEST_BODY_MAX_BYTES:
                await self._send_too_large(scope, receive, send)
                return

            if not message.get("more_body", False):
                break

        replayed = False

        # 关键点：上面为了统计体积已经把请求体从 receive 通道「消费」掉了，下游 app 再直接 receive 将读不到 body。
        # 因此包一层 replay_body：首次调用把已缓存的完整 body 一次性重放给下游，之后再回退到原始 receive，保证下游能正常读取请求体。
        async def replay_body() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_body, send)

    # 直接构造并发送 413 响应，告知客户端请求体过大
    async def _send_too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
        )
        await response(scope, receive, send)


# 根据请求路径返回对应的 Cache-Control：先按前缀匹配 assets/icons/images，再精确匹配 manifest.json / sw.js / offline.html，均不匹配则返回 None
def _cache_control_for_static_path(path: str) -> str | None:
    normalized_path = path.lstrip("/")
    for prefix, cache_control in STATIC_CACHE_CONTROL_BY_PREFIX.items():
        if normalized_path.startswith(prefix):
            return cache_control
    if normalized_path == "manifest.json":
        return MANIFEST_CACHE_CONTROL
    if normalized_path == "sw.js":
        return SERVICE_WORKER_CACHE_CONTROL
    if normalized_path == "offline.html":
        return OFFLINE_PAGE_CACHE_CONTROL
    return None


# 构造静态文件响应，并按路径附加合适的 Cache-Control 头
def _static_file_response(file_path: Path, request_path: str) -> FileResponse:
    headers = {}
    cache_control = _cache_control_for_static_path(request_path)
    if cache_control:
        headers["Cache-Control"] = cache_control
    return FileResponse(str(file_path), headers=headers)


# 判断路径是否为已存在的普通文件（会被包在 run_blocking_io 中调用，避免阻塞事件循环）
def _is_existing_file(file_path: Path) -> bool:
    return file_path.exists() and file_path.is_file()


# 读取 index.html 文本，并用 (mtime_ns, size) 作为版本标识做内存缓存（避免每次 SPA/分享请求都读盘）
def _read_index_html(index_file: Path) -> str:
    stat = index_file.stat()
    # 超过大小上限视为异常情况，直接抛错而不缓存
    if stat.st_size > INDEX_HTML_MAX_BYTES:
        raise ValueError(f"index.html too large: {stat.st_size} bytes (max {INDEX_HTML_MAX_BYTES})")
    cache_key = index_file.resolve()
    cached = _INDEX_HTML_CACHE.get(cache_key)
    # 缓存命中且文件未变更（mtime 与 size 都一致）：移到末尾维持 LRU 顺序并直接返回
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        _INDEX_HTML_CACHE.move_to_end(cache_key)
        return cached[2]

    # 未命中或文件已变更：重新读盘并写入缓存
    html_doc = index_file.read_text(encoding="utf-8")
    _INDEX_HTML_CACHE[cache_key] = (stat.st_mtime_ns, stat.st_size, html_doc)
    # 超过条目上限时按 LRU 淘汰最久未使用的项
    while len(_INDEX_HTML_CACHE) > INDEX_HTML_CACHE_MAX_ENTRIES:
        _INDEX_HTML_CACHE.popitem(last=False)
    return html_doc


async def _warm_agent_registry() -> None:
    """Preload agent registrations without blocking startup."""
    try:
        from src.agents import discover_agents

        await run_blocking_io(discover_agents)
        logger.info("Agents discovered")
    except Exception as e:
        logger.warning("Agent discovery warm-up failed: %s", e, exc_info=True)


async def _warm_models_cache() -> None:
    """Preload model metadata without blocking application startup."""
    try:
        from src.infra.llm.models_service import refresh_models

        await refresh_models()
        logger.info("Models preloaded into memory cache")
    except Exception as e:
        logger.warning("Model cache warm-up failed: %s", e, exc_info=True)


def _schedule_models_cache_warmup(app: FastAPI) -> asyncio.Task[None]:
    """Schedule model cache warm-up and keep a task reference for shutdown."""
    task = asyncio.create_task(_warm_models_cache())
    app.state.models_preload_task = task
    return task


async def _cleanup_stale_tasks() -> None:
    """Recover stale tasks in the background after runtime listeners start."""
    try:
        from src.infra.task.manager import get_task_manager

        task_manager = get_task_manager()
        await task_manager.cleanup_stale_tasks()
        logger.info("Stale tasks cleaned up")
    except Exception as e:
        logger.warning("Stale task cleanup failed: %s", e, exc_info=True)


def _schedule_stale_task_cleanup(app: FastAPI) -> asyncio.Task[None]:
    """Schedule stale task reconciliation without blocking application readiness."""

    async def _run_recheck() -> None:
        await asyncio.sleep(_STALE_TASK_CLEANUP_RECHECK_DELAY_SECONDS)
        await _cleanup_stale_tasks()

    task = asyncio.create_task(_cleanup_stale_tasks())
    app.state.stale_task_cleanup_task = task
    app.state.stale_task_cleanup_recheck_task = asyncio.create_task(_run_recheck())
    return task


# 按名批量取消登记在 app.state 上的后台任务：逐个取出并清空引用，对未完成的发起 cancel，最后统一 await 收敛（吞掉取消异常）
async def _cancel_background_tasks(app: FastAPI, *task_names: str) -> None:
    tasks = []
    for task_name in task_names:
        task = getattr(app.state, task_name, None)
        if task is None:
            continue
        setattr(app.state, task_name, None)
        if not task.done():
            task.cancel()
            tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# 关闭时优先停止飞书长连接：先取消其后台任务，再调用 stop_feishu_channels 释放 lease，避免快速重启时旧锁阻止新实例启动
async def _stop_feishu_channels_for_shutdown(app: FastAPI) -> None:
    await _cancel_background_tasks(app, "feishu_task")
    try:
        from src.infra.channel.feishu import stop_feishu_channels

        await stop_feishu_channels()
        logger.info("Feishu channels stopped")
    except Exception as e:
        logger.warning(f"Failed to stop Feishu channels: {e}")


# 关闭各路由依赖所持有的单例资源（反馈/通知/推送/揭示文件/上传/人设预设等管理器），释放其连接与后台协程
async def _close_route_dependency_singletons() -> None:
    from src.infra.persona_preset.manager import close_persona_preset_manager
    from src.infra.revealed_file.storage import close_revealed_file_storage

    await feedback.close_feedback_manager()
    await notification.close_notification_manager()
    from src.infra.push.manager import close_push_manager

    await close_push_manager()
    await close_revealed_file_storage()
    await upload.close_upload_route_dependencies()
    await close_persona_preset_manager()


# 关闭用户级会话沙箱管理器（SessionSandboxManager 管理的沙箱）
async def _close_session_sandbox_manager_for_shutdown() -> None:
    from src.infra.sandbox.session_manager import close_session_sandbox_manager

    await close_session_sandbox_manager()


# 统一取消 lifespan 期间启动的所有后台任务（见 _LIFESPAN_BACKGROUND_TASK_NAMES）
async def _cancel_lifespan_background_tasks_for_shutdown(app: FastAPI) -> None:
    await _cancel_background_tasks(app, *_LIFESPAN_BACKGROUND_TASK_NAMES)


# 返回 (名称, 初始化协程函数) 列表；每个初始化器为对应存储创建/确保索引，供启动阶段并发执行
def _startup_index_initializers():
    async def _init_agent_config_storage() -> None:
        from src.infra.agent.config_storage import get_agent_config_storage

        await get_agent_config_storage().ensure_indexes()
        logger.info("Agent config storage indexes initialized")

    async def _init_model_storage() -> None:
        from src.infra.agent.model_storage import get_model_storage

        await get_model_storage().ensure_indexes()
        logger.info("Model storage indexes initialized")

    async def _init_channel_storage() -> None:
        from src.infra.channel.channel_storage import ChannelStorage

        await ChannelStorage().ensure_indexes_if_needed()
        logger.info("Channel storage indexes initialized")

    async def _init_skill_indexes() -> None:
        from src.infra.skill import init_skill_indexes

        await init_skill_indexes()
        logger.info("Skill indexes initialized")

    async def _init_trace_storage() -> None:
        from src.infra.session.trace_storage import get_trace_storage

        await get_trace_storage().ensure_indexes_if_needed()
        logger.info("TraceStorage initialized")

    async def _init_session_storage() -> None:
        from src.infra.session.storage import SessionStorage

        await SessionStorage().ensure_indexes_if_needed()
        logger.info("SessionStorage indexes initialized")

    async def _init_revealed_file_storage() -> None:
        from src.infra.revealed_file.storage import get_revealed_file_storage

        await get_revealed_file_storage().ensure_indexes_if_needed()
        logger.info("RevealedFileStorage indexes initialized")

    async def _init_notification_storage() -> None:
        from src.infra.notification.storage import NotificationStorage

        await NotificationStorage().create_indexes()
        logger.info("NotificationStorage indexes initialized")

    async def _init_push_subscription_storage() -> None:
        from src.infra.push.storage import PushSubscriptionStorage

        await PushSubscriptionStorage().create_indexes()
        logger.info("PushSubscription indexes initialized")

    async def _init_user_storage() -> None:
        from src.infra.user.storage import UserStorage

        await UserStorage().ensure_indexes_if_needed()
        logger.info("UserStorage indexes initialized")

    async def _init_usage_storage() -> None:
        from src.infra.usage.storage import get_usage_storage

        await get_usage_storage().ensure_indexes()
        logger.info("UsageStorage indexes initialized")

    return [
        ("agent_config_storage", _init_agent_config_storage),
        ("model_storage", _init_model_storage),
        ("channel_storage", _init_channel_storage),
        ("skill_indexes", _init_skill_indexes),
        ("trace_storage", _init_trace_storage),
        ("session_storage", _init_session_storage),
        ("revealed_file_storage", _init_revealed_file_storage),
        ("notification_storage", _init_notification_storage),
        ("push_subscription_storage", _init_push_subscription_storage),
        ("user_storage", _init_user_storage),
        ("usage_storage", _init_usage_storage),
    ]


async def _initialize_startup_indexes() -> None:
    """Initialize independent storage indexes concurrently before serving traffic."""

    async def _run_initializer(name, initializer) -> None:
        try:
            await initializer()
        except Exception as e:
            logger.error("Startup index initializer failed: %s", name, exc_info=True)
            raise e

    await asyncio.gather(
        *(
            _run_initializer(name, initializer)
            for name, initializer in _startup_index_initializers()
        )
    )


async def _run_startup_indexes(app: FastAPI) -> None:
    """Initialize storage indexes before app readiness."""
    task = asyncio.create_task(_initialize_startup_indexes())
    app.state.startup_indexes_task = task
    await task
    logger.info("Startup storage indexes initialized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # lifespan 以 yield 为界：yield 之前是「启动阶段」（按依赖顺序初始化各组件），yield 之后的 finally 是「关闭阶段」（按安全顺序优雅释放资源）
    # 启动时初始化
    logger.info("%s v%s starting...", settings.APP_NAME, settings.APP_VERSION)

    # 初始化日志系统
    setup_logging()

    # 初始化默认角色（更新系统角色权限）
    try:
        from src.infra.role.storage import RoleStorage

        role_storage = RoleStorage()
        await role_storage.init_default_roles()
        logger.info("Default roles initialized")
    except Exception as e:
        logger.error("Failed to initialize default roles: %s", e)

    # 配置 uvicorn 访问日志格式，与项目日志完全统一
    import logging

    from src.infra.logging.filter import TraceFilter
    from src.infra.logging.formatter import ColoredFormatter

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(logging.INFO)
    access_logger.handlers.clear()
    access_handler = logging.StreamHandler()
    # 使用项目相同的格式和 ColoredFormatter
    access_handler.setFormatter(
        ColoredFormatter(
            fmt=settings.LOG_FORMAT,
            datefmt=settings.LOG_DATE_FORMAT,
        )
    )
    # 添加 TraceFilter 以支持 trace_info
    access_handler.addFilter(TraceFilter())
    access_logger.addHandler(access_handler)

    # 从数据库初始化设置
    await initialize_settings()
    logger.info("Settings initialized from database")

    validate_distributed_runtime_settings(settings)

    # 初始化本地文件系统目录（使用数据库覆盖后的最终配置）
    ensure_local_filesystem_dirs(settings)

    # 启动进程内存监控
    memory_monitor = get_memory_monitor()
    await memory_monitor.start()
    logger.info("Memory monitor started")

    # 后台预热 Agent 注册，避免阻塞服务启动；请求路径仍有懒发现兜底
    app.state.agent_discovery_task = asyncio.create_task(_warm_agent_registry())

    # 阻塞等待存储索引初始化完成后再继续——索引是后续服务的前置依赖，必须先就绪
    await _run_startup_indexes(app)

    # 启动分布式运行时监听器（任务/设置/模型/记忆/WebSocket）
    await start_runtime_services()
    logger.info("Runtime distributed listeners started")

    # 后台恢复/清理残留任务；恢复逻辑自身有分布式锁与 heartbeat 判断。
    _schedule_stale_task_cleanup(app)

    # 后台预加载模型列表；请求路径仍有 memory -> Redis -> DB 懒加载兜底。
    _schedule_models_cache_warmup(app)

    # 初始化 SessionStorage 搜索索引，并异步回填历史会话
    from src.infra.session.backfill import SessionSearchBackfillWorker

    async def _backfill_session_search():
        worker = SessionSearchBackfillWorker()
        try:
            delay = getattr(settings, "SESSION_SEARCH_BACKFILL_STARTUP_DELAY_SECONDS", 30.0)
            if delay > 0:
                await asyncio.sleep(delay)
            rebuilt = await worker.run_until_complete()
            logger.info("Session search backfill finished, rebuilt %s sessions", rebuilt)
        except Exception as e:
            logger.warning("Session search backfill failed: %s", e)
        finally:
            await worker.close()
            await memory_monitor.reset_baseline()
            logger.info("Memory monitor baseline reset after session search backfill")

    _session_search_backfill_task = asyncio.create_task(_backfill_session_search())
    app.state.session_search_backfill_task = _session_search_backfill_task

    # Start Feishu channels in background (don't block app startup)
    async def _start_feishu():
        try:
            from src.infra.channel.feishu.handler import setup_feishu_handler

            await setup_feishu_handler(
                default_agent=settings.DEFAULT_AGENT,
                show_tools=True,
            )
        except Exception as e:
            logger.warning(f"Failed to start Feishu channels: {e}")

    # 保留任务引用（挂到 app.state），防止被垃圾回收器回收导致任务被提前取消
    # Keep task reference to prevent GC from cancelling it
    _feishu_task = asyncio.create_task(_start_feishu())
    app.state.feishu_task = _feishu_task

    async def _reset_memory_monitor_after_startup() -> None:
        try:
            await memory_monitor.reset_baseline()
            logger.info("Memory monitor baseline reset after startup initialization")
        except Exception as e:
            logger.warning("Memory monitor baseline reset after startup failed: %s", e)

    app.state.memory_monitor_startup_reset_task = asyncio.create_task(
        _reset_memory_monitor_after_startup()
    )

    # 启动阶段结束，交出控制权让应用开始处理请求；直到进程收到关闭信号才会从 yield 恢复并进入下方 finally 做清理
    try:
        yield
    except asyncio.CancelledError:
        # Ctrl+C / server cancellation during lifespan shutdown is a normal exit path.
        logger.info("Application lifespan cancelled, continuing graceful shutdown")
    finally:
        # 关闭时清理
        from src.agents import AgentFactory
        from src.infra.sandbox import SandboxFactory

        # 先关闭飞书长连接并释放 lease，避免快速重启时旧锁阻止新实例启动。
        await _stop_feishu_channels_for_shutdown(app)
        # 再统一取消 lifespan 后台任务，让各任务自己的 finally 在依赖关闭前完成。
        await _cancel_lifespan_background_tasks_for_shutdown(app)

        # 停止事件合并器
        from src.infra.session.event_merger import close_event_merger
        from src.infra.session.trace_storage import close_trace_storage
        from src.infra.task.manager import get_task_manager

        await close_event_merger()
        await close_trace_storage()
        logger.info("EventMerger stopped")

        # 标记所有运行中的任务为失败
        task_manager = get_task_manager()

        # 先停止分布式运行时监听器，再关闭任务
        await stop_runtime_services()
        logger.info("Runtime distributed listeners stopped")

        from src.infra.monitoring import close_memory_monitor

        await close_memory_monitor()
        logger.info("Memory monitor stopped")

        await task_manager.shutdown()
        logger.info("Background tasks marked as failed")

        # 清理 executor 注册表
        from src.infra.task.concurrency import unregister_executor

        unregister_executor("agent_stream")
        logger.info("Executor registry cleaned up")

        # 关闭所有 sandbox
        await SandboxFactory.close_all()

        # 关闭用户级沙箱（SessionSandboxManager 管理的）
        await _close_session_sandbox_manager_for_shutdown()
        logger.info("User sandboxes stopped")

        await AgentFactory.close_all()

        await _close_route_dependency_singletons()

        # 关闭 PostgreSQL 连接池
        from src.infra.storage.checkpoint import (
            close_async_checkpointer,
            close_pg_checkpointer,
        )
        from src.infra.storage.mongodb_store import close_store
        from src.infra.storage.postgres import close_connection_pool

        await close_store()
        close_async_checkpointer()
        close_connection_pool()
        await close_pg_checkpointer()

        # 关闭 EmailService HTTP 客户端
        from src.infra.email import close_email_service

        await close_email_service()

        # 关闭 OAuth 客户端
        from src.infra.auth.oauth import close_oauth_service

        await close_oauth_service()

        # 关闭 MCP 连接池
        from src.infra.tool.mcp_pool import close_all_connections

        await close_all_connections()

        # 关闭 RateLimiter Redis 连接
        from src.api.routes.auth import close_rate_limiter

        await close_rate_limiter()

        # 关闭主 Redis 连接池
        from src.infra.storage.redis import close_redis_client

        await close_redis_client()

        # 释放 MongoDB checkpointer 引用（在关闭连接池之前）
        from src.infra.storage.checkpoint import close_mongo_checkpointer

        close_mongo_checkpointer()

        # 关闭 MongoDB 连接池
        from src.infra.storage.mongodb import close_approval_storage, close_mongo_client

        close_approval_storage()
        await close_mongo_client()

        # 兜底：取消进程内其余仍在运行的后台任务（如第三方库 lark_oapi 的定时清理协程），并等待它们收敛后再退出
        # Cancel remaining background tasks (e.g., lark_oapi ExpiringCache cron)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            logger.info(f"Cancelled {len(pending)} remaining background task(s)")

        logger.info("Shutting down...")


def create_app() -> FastAPI:
    """创建 FastAPI 应用"""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        lifespan=lifespan,
    )

    # CORS：放行所有来源/方法/请求头，并允许携带凭证（Cookie/Authorization）
    # CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 中间件注册顺序至关重要：Starlette 中「后 add 的中间件位于更外层、更先执行」，请求由外到内穿过、响应由内到外返回。
    # 因此下面按「最内层先 add」登记（UserContext 最先 add → 最内层、紧邻路由），最终得到下一行标注的实际执行链。
    # 自定义中间件 (顺序：后添加的先执行)
    # 执行顺序: RequestBodyLimitMiddleware -> TracingMiddleware -> AuthMiddleware -> UserContextMiddleware -> Route
    app.add_middleware(UserContextMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(TracingMiddleware)
    app.add_middleware(RequestBodyLimitMiddleware)

    # 注册各业务路由：除健康检查、WebSocket 等少数无前缀路由外，其余统一挂在 /api/* 前缀下，并用 tags 分组便于 OpenAPI 文档归类
    # 注册路由
    app.include_router(health.router, tags=["Health"])
    app.include_router(version.router, prefix="/api", tags=["Version"])
    # Chat 路由: /api/chat/stream 后台执行, /api/chat/sessions/{id}/stream SSE
    app.include_router(chat.router, prefix="/api/chat", tags=["Chat"])
    # Agent 路由: /api/agents 列表, /api/{agent_id}/stream 和 /api/{agent_id}/chat
    app.include_router(agent.router, prefix="/api", tags=["Agents"])
    # Agent 配置路由: /api/agent/config 全局配置和用户偏好
    app.include_router(agent_config.router, prefix="/api/agent/config", tags=["Agent Config"])
    # Model 配置路由: /api/agent/models CRUD
    app.include_router(agent_model.router, prefix="/api/agent/models", tags=["Models"])
    app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
    app.include_router(user.router, prefix="/api/users", tags=["Users"])
    app.include_router(role.router, prefix="/api/roles", tags=["Roles"])
    app.include_router(
        persona_preset.router,
        prefix="/api/persona-presets",
        tags=["Persona Presets"],
    )
    app.include_router(team.router, prefix="/api/teams", tags=["Teams"])
    app.include_router(session.router, prefix="/api/sessions", tags=["Sessions"])
    app.include_router(project.router, prefix="/api/projects", tags=["Projects"])
    app.include_router(share.router, prefix="/api/share", tags=["Share"])
    app.include_router(skill.router, prefix="/api/skills", tags=["Skills"])
    app.include_router(github.router, prefix="/api/github", tags=["GitHub"])

    # User marketplace API
    from src.api.routes.marketplace import router as marketplace_router

    app.include_router(marketplace_router, prefix="/api/marketplace", tags=["Marketplace"])

    app.include_router(settings_router.router, prefix="/api/settings", tags=["Settings"])
    app.include_router(memory.router, prefix="/api/memory", tags=["Memory"])
    app.include_router(mcp.router, prefix="/api/mcp", tags=["MCP"])
    app.include_router(mcp.admin_router, prefix="/api/admin/mcp", tags=["MCP Admin"])
    app.include_router(envvar.router, prefix="/api/env-vars", tags=["Environment Variables"])
    app.include_router(upload.router, prefix="/api/upload", tags=["Upload"])
    app.include_router(revealed_file.router, prefix="/api/files", tags=["Files"])
    app.include_router(human.router, prefix="/human", tags=["Human"])
    app.include_router(feedback.router, prefix="/api/feedback", tags=["Feedback"])
    app.include_router(usage.router, prefix="/api/usage", tags=["Usage"])
    app.include_router(notification.router, prefix="/api/notifications", tags=["Notifications"])
    app.include_router(push.router, prefix="/api/push", tags=["Push"])
    # Generic channel configuration
    app.include_router(channels.router, prefix="/api/channels", tags=["Channels"])
    # Scheduled tasks
    app.include_router(
        scheduled_task.router, prefix="/api/scheduled-tasks", tags=["Scheduled Tasks"]
    )
    # WebSocket 路由: /ws 用于实时通知
    app.include_router(websocket.router, tags=["WebSocket"])

    # 托管前端资源：resolve_frontend_target 决定运行模式——
    #   ("static", 目录) 为生产模式，由后端直接提供已构建的前端；("redirect", url) 为本地开发，重定向到 Vite dev server
    # Serve frontend static files
    project_root = Path(__file__).parent.parent.parent
    frontend_target = resolve_frontend_target(
        project_root,
        settings.FRONTEND_DEV_URL if hasattr(settings, "FRONTEND_DEV_URL") else "",
    )
    if frontend_target and frontend_target[0] == "static":
        static_dir = frontend_target[1]
        assert isinstance(static_dir, Path)

        # 将带内容指纹的构建产物目录挂载为静态路由（/assets、/icons），交由 StaticFiles 直接高效服务
        assets_dir = static_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        icons_dir = static_dir / "icons"
        if icons_dir.exists():
            app.mount("/icons", StaticFiles(directory=str(icons_dir)), name="icons")

        # PWA 的 manifest.json：存在则带缓存头返回，缺失则返回错误 JSON
        # Serve other static files (manifest.json, etc.)
        @app.get("/manifest.json")
        async def serve_manifest():
            manifest_file = static_dir / "manifest.json"
            if await run_blocking_io(_is_existing_file, manifest_file):
                return _static_file_response(manifest_file, "manifest.json")
            return {"error": "manifest.json not found"}

        # 分享页：服务端把 SEO 元数据注入 index.html，让爬虫/社媒预览能抓到标题、描述与 OG 卡片；
        # 若分享内容需要鉴权或不存在，则注入对应的错误态 SEO 并返回相应状态码（401/404）。
        @app.get("/shared/{share_id}", response_class=HTMLResponse)
        async def serve_shared_page(share_id: str, request: Request):
            """Serve shared pages with server-injected SEO metadata."""
            index_file = static_dir / "index.html"
            if not await run_blocking_io(_is_existing_file, index_file):
                return {"error": "Frontend not built. Run 'npm run build' in frontend directory."}

            # 优先使用配置的 APP_BASE_URL 作为 SEO 绝对链接前缀，缺省则回退到请求自身的 base_url
            base_url = getattr(settings, "APP_BASE_URL", "").rstrip("/") or str(
                request.base_url
            ).rstrip("/")
            html_doc = await run_blocking_io(_read_index_html, index_file)

            # 以匿名身份获取分享内容用于生成 SEO：401 标记为 auth_required，其余异常按 not_found 处理
            try:
                shared_content = await share.get_shared_content(share_id, user=None)
            except HTTPException as exc:
                reason = "auth_required" if exc.status_code == 401 else "not_found"
                seo = build_shared_page_error_seo(
                    base_url=base_url,
                    share_id=share_id,
                    app_name=settings.APP_NAME,
                    reason=reason,
                )
                rendered = inject_share_seo_into_html(html_doc, seo)
                return HTMLResponse(content=rendered, status_code=exc.status_code)

            seo = build_shared_page_seo(
                base_url=base_url,
                share_id=share_id,
                session=shared_content.session,
                owner=shared_content.owner.model_dump(),
                events=shared_content.events,
                app_name=settings.APP_NAME,
                indexable=False,
            )
            rendered = inject_share_seo_into_html(html_doc, seo)
            return HTMLResponse(content=rendered)

        # SPA 兜底路由（匹配所有未命中的路径）：先尝试把它当作真实静态文件返回；否则返回注入了公共路由 SEO 的 index.html，交由前端客户端路由处理
        # SPA fallback - serve index.html for all unmatched routes
        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str, request: Request):
            """Serve SPA index.html for client-side routing."""
            # First, check if it's a static file
            static_file = static_dir / full_path
            if await run_blocking_io(_is_existing_file, static_file):
                return _static_file_response(static_file, full_path)
            # Otherwise, serve index.html for SPA routing
            index_file = static_dir / "index.html"
            if await run_blocking_io(_is_existing_file, index_file):
                base_url = getattr(settings, "APP_BASE_URL", "").rstrip("/") or str(
                    request.base_url
                ).rstrip("/")
                path = f"/{full_path}" if full_path else "/"
                seo = build_public_route_seo(base_url=base_url, path=path)
                html_doc = await run_blocking_io(_read_index_html, index_file)
                rendered = inject_public_route_seo_into_html(html_doc, seo)
                return HTMLResponse(content=rendered)
            return {"error": "Frontend not built. Run 'npm run build' in frontend directory."}

    # 本地开发模式：把所有前端路由请求 302 重定向到 Vite dev server
    elif frontend_target and frontend_target[0] == "redirect":
        frontend_dev_url = frontend_target[1]
        assert isinstance(frontend_dev_url, str)

        @app.get("/{full_path:path}")
        async def serve_frontend_dev(full_path: str):
            """Redirect SPA requests to the Vite dev server during local development."""
            path = f"/{full_path}" if full_path else ""
            return RedirectResponse(url=f"{frontend_dev_url}{path}")

    return app


# 创建应用实例
app = create_app()

"""API middleware for request processing."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src.infra.async_utils import run_blocking_io
from src.infra.auth.jwt import verify_token
from src.infra.backend.context import clear_user_context, set_user_context
from src.infra.logging.context import TraceContext


# 用户上下文中间件：位于自定义中间件链的最内层（最先被 add，因而最后执行、离路由最近，在 Auth 之后运行）。
# 核心职责是把当前请求的 user_id / session_id 注入到「后端上下文变量」和「日志追踪上下文」中，
# 让后续业务代码（存储层按用户隔离、日志自动带用户字段）无需逐层传参即可获取；请求结束后务必清理，防止 contextvar 泄漏。
class UserContextMiddleware(BaseHTTPMiddleware):
    """
    Middleware to set user context for each request.

    This middleware extracts user_id from JWT token and sets it in the context
    for backend operations. Context is always cleared after the request completes.
    """

    async def dispatch(self, request: Request, call_next):
        # 默认视为匿名请求；只有 token 校验成功才会被赋值
        user_id = None
        # 会话 ID 来自自定义请求头 X-Session-Id，用于把日志/上下文关联到某个会话
        session_id = request.headers.get("X-Session-Id")

        # Extract user_id from JWT token
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]  # Remove "Bearer " prefix
            try:
                # verify_token 为同步阻塞调用，放入线程池执行避免阻塞事件循环；
                # 解码出的 payload 缓存到 request.state.auth_payload，供 deps 里的认证依赖复用，避免重复解码同一 token
                payload = await run_blocking_io(verify_token, token)
                request.state.auth_payload = payload
                user_id = str(payload.sub) if payload.sub else None
            except Exception:
                pass  # Token invalid, user_id stays None

        try:
            # 仅在成功解析出用户时写入后端上下文变量（供存储层等按用户隔离数据）
            if user_id:
                set_user_context(user_id, session_id)
            # 同时把用户/会话信息挂到 request.state，供 TracingMiddleware 等其他环节读取
            request.state.logging_user_id = user_id
            request.state.logging_session_id = session_id
            # 将请求级追踪字段注入 TraceContext，日志过滤器据此自动附带 request_id/trace_id/session_id/user_id
            TraceContext.set_request_context(
                request_id=getattr(request.state, "request_id", None),
                session_id=session_id,
                user_id=user_id,
                trace_id=getattr(request.state, "trace_id", None),
            )
            response = await call_next(request)
            return response
        finally:
            # 无论正常返回还是抛异常都必须清理，避免上下文变量残留污染后续复用的协程/请求
            clear_user_context()
            TraceContext.clear_request_context()

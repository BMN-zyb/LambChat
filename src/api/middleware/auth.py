"""
认证中间件
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


# 认证中间件：在中间件链中位于 Tracing 之后、UserContext 之前执行。
# 它只做「粗粒度」门禁——仅检查请求是否携带 Bearer token，真正的 token 解码与用户加载由路由级依赖 get_current_user_required 负责。
# 设计意图：为缺少路由级鉴权保护的路径提供一道兜底防护，同时放行公开路径、CORS 预检以及浏览器页面导航（交给 SPA fallback）。
class AuthMiddleware(BaseHTTPMiddleware):
    """
    认证中间件

    验证请求中的 JWT token。
    Note: Most routes use route-level Depends(get_current_user_required) for auth.
    This middleware provides an additional layer for paths that may not have
    route-level guards.
    """

    # 精确匹配的白名单：路径完全相等即直接放行、不校验 token（健康检查、登录/注册、/docs、PWA 的 sw.js/manifest、robots/sitemap 等）
    # 不需要认证的路径（精确匹配）
    PUBLIC_PATHS = {
        "/",
        "/health",
        "/ready",
        "/api/auth/login",
        "/api/auth/register",
        "/docs",
        "/openapi.json",
        "/api/auth/permissions",
        "/api/push/vapid-public-key",
        "/manifest.json",
        "/sw.js",
        "/offline.html",
        "/api/version",
        "/robots.txt",
        "/sitemap.xml",
        "/index.html",
    }

    # 前缀匹配的白名单：路径以其中任一项开头即放行（OAuth 回调、刷新/找回/重置密码、邮箱验证、公开分享、静态资源目录、/api/agents 列表等）
    # 不需要认证的路径前缀
    PUBLIC_PREFIXES = (
        "/api/auth/oauth/",
        "/api/auth/refresh",
        "/api/auth/forgot-password",
        "/api/auth/reset-password",
        "/api/auth/verify-email",
        "/api/auth/resend-verification",
        "/api/upload/file/",
        "/assets/",
        "/icons/",
        "/images/",
        "/shared/",
        "/api/share/public/",
        "/api/agents",
        "/auth/",
        "/favicon",
        "/static/",
    )

    # 判断是否为浏览器「页面导航」请求（GET/HEAD 且 Accept 含 text/html）。
    # 用途：此类请求应放行到 SPA fallback 交前端路由处理，而 API/XHR（Accept 为 application/json 或 */*）仍需鉴权，从而保护后端接口。
    @staticmethod
    def _is_browser_page_request(request: Request) -> bool:
        """
        Allow unauthenticated browser navigations for SPA routes.

        API/XHR requests usually send ``Accept: application/json`` or ``*/*``,
        while full page navigations include ``text/html``. This keeps backend
        APIs protected and lets the frontend router handle routes like
        ``/models`` after the request reaches the SPA fallback.
        """
        if request.method not in {"GET", "HEAD"}:
            return False

        accept = request.headers.get("accept", "")
        return "text/html" in accept.lower()

    # 构造带 CORS 头的 JSON 响应；中间件里直接返回的 401 不会经过 CORSMiddleware，需手动回显 Origin 相关头，否则浏览器会拦截该响应
    @staticmethod
    def _cors_response(request: Request, status_code: int, content: dict) -> JSONResponse:
        """Build a JSONResponse with CORS headers so browsers don't block it."""
        origin = request.headers.get("origin", "")
        response = JSONResponse(status_code=status_code, content=content)
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response

    # 逐层判定是否放行：CORS 预检(OPTIONS) -> 精确白名单 -> 前缀白名单 -> 浏览器页面导航 -> 其余请求要求携带 Bearer token
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # CORS preflight — always pass
        if request.method == "OPTIONS":
            return await call_next(request)

        # Exact match on public paths
        if path in self.PUBLIC_PATHS:
            return await call_next(request)

        # Prefix match for known public prefixes
        for prefix in self.PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Let browser page navigations reach the SPA fallback / redirect route.
        if self._is_browser_page_request(request):
            return await call_next(request)

        # All other paths require an Authorization header
        auth_header = request.headers.get("Authorization")
        # 缺少 Authorization 或不是 Bearer 形式：直接返回 401（带 CORS 头），不再进入路由
        if not auth_header or not auth_header.startswith("Bearer "):
            return self._cors_response(
                request,
                status_code=401,
                content={"detail": "Not authenticated"},
            )

        # 已带 Bearer token：此处只放行，token 合法性与用户/权限由路由级依赖进一步校验
        return await call_next(request)

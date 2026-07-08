"""
Authentication routes module

Aggregates all authentication-related routes from submodules.
"""

# FastAPI 的路由器类，用于把本认证子模块的各个子路由聚合成一个总路由
from fastapi import APIRouter

# 导入各子模块的路由/工具（每个子文件负责一组相关的认证接口）：
# core：注册/登录/刷新令牌/当前用户/权限等核心认证接口
from .core import router as core_router
# oauth：第三方 OAuth 登录（授权跳转 + 回调换取用户）
from .oauth import router as oauth_router
# profile：当前用户资料查看/修改（头像、用户名、metadata 等）
from .profile import router as profile_router
# rate_limiter：基于 Redis 的限流器（防暴力破解），并导出单例获取/关闭钩子
from .rate_limiter import RateLimiter, close_rate_limiter, get_rate_limiter
# utils：认证工具函数（获取客户端 IP、前端 URL、OAuth state 存取与校验等）
from .utils import _get_client_ip, _get_frontend_url, _store_oauth_state, _verify_oauth_state
# verification：邮箱验证与密码重置相关接口
from .verification import router as verification_router

# Main router that aggregates all sub-routers
# 创建聚合路由器：把上面各子路由挂到一起，最终由上层以 /api/auth 前缀统一挂载
router = APIRouter()
# 依次注册各子路由（注册顺序只影响 OpenAPI 文档展示顺序，不影响实际路径匹配）
router.include_router(core_router)
router.include_router(profile_router)
router.include_router(oauth_router)
router.include_router(verification_router)

# 对外导出的符号：供上层聚合与其他模块 import（含限流器生命周期钩子 close_rate_limiter 等）
__all__ = [
    "router",
    "RateLimiter",
    "close_rate_limiter",
    "get_rate_limiter",
    "_get_client_ip",
    "_get_frontend_url",
    "_store_oauth_state",
    "_verify_oauth_state",
]

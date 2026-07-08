"""
OAuth authentication routes
"""

import secrets
# isawaitable：用于兼容 _verify_oauth_state 可能是同步或异步实现的情况
from inspect import isawaitable
from typing import Annotated
# urlencode：把 token 等参数编码进 URL（query 或 fragment）
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Path, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, StringConstraints

from src.infra.auth.turnstile import get_turnstile_service
from src.infra.logging import get_logger
from src.kernel.config import settings
# OAuthProvider：受支持的第三方登录提供商枚举（google/github/apple）
from src.kernel.schemas.user import OAuthProvider

from .utils import _get_client_ip, _get_frontend_url, _store_oauth_state, _verify_oauth_state

router = APIRouter()
logger = get_logger(__name__)


# OAuth provider path parameter with validation
# OAuth 提供商路径参数类型：用正则限定取值只能是 google/github/apple，非法值会直接返回 422
OAuthProviderParam = Annotated[
    str,
    StringConstraints(pattern="^(google|github|apple)$"),
    Path(description="OAuth provider name", examples=["google", "github", "apple"]),
]


# GET /oauth/providers —— 返回已启用的 OAuth 登录选项及认证相关设置
# 响应体：{ providers: [...], registration_enabled: bool, turnstile: {...} }，供前端渲染登录页
@router.get("/oauth/providers")
async def get_oauth_providers():
    """
    获取可用的 OAuth 提供商列表和认证设置

    返回已启用的 OAuth 登录选项以及注册是否启用。
    """
    providers: list[dict[str, str]] = []
    try:
        from src.infra.auth.oauth import get_oauth_service

        oauth_service = get_oauth_service()
        # 遍历所有受支持的提供商，仅收集"已在配置中启用"的项返回给前端
        for provider in OAuthProvider:
            if oauth_service.is_provider_enabled(provider):
                providers.append(
                    {
                        "id": provider.value,
                        "name": provider.value.capitalize(),
                    }
                )
    except Exception as e:
        logger.error("OAuth providers error: %s", e, exc_info=True)

    # 获取 Turnstile 配置
    turnstile_service = get_turnstile_service()

    return {
        "providers": providers,
        "registration_enabled": settings.ENABLE_REGISTRATION,
        "turnstile": {
            "enabled": turnstile_service.is_enabled,
            "site_key": turnstile_service.site_key,
            "require_on_login": turnstile_service.require_on_login,
            "require_on_register": turnstile_service.require_on_register,
            "require_on_password_change": turnstile_service.require_on_password_change,
        },
    }


# GET /oauth/{provider} —— 发起第三方 OAuth 授权
# provider 路径参数受 OAuthProviderParam 约束（google/github/apple）
# 流程：生成随机 state 存入 Redis（用于 CSRF 防护）→ 拼装 redirect_uri → 直接 302 跳转到提供商授权页
@router.get("/oauth/{provider}")
async def oauth_login(request: Request, provider: OAuthProviderParam):
    """
    发起 OAuth 授权

    返回授权 URL，前端应重定向到该 URL。
    """
    from src.infra.auth.oauth import get_oauth_service

    oauth_service = get_oauth_service()
    oauth_provider = OAuthProvider(provider)

    if not oauth_service.is_provider_enabled(oauth_provider):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OAuth provider '{provider}' is not enabled",
        )

    # 生成 state 用于 CSRF 防护
    state = secrets.token_urlsafe(32)

    # 获取客户端 IP 并存储 state
    client_ip = _get_client_ip(request)
    await _store_oauth_state(provider, state, client_ip)

    # 从请求中获取前端 URL
    frontend_url = _get_frontend_url(request)
    redirect_uri = _oauth_redirect_uri(frontend_url, provider)

    # 获取授权 URL
    auth_url = oauth_service.get_authorization_url(oauth_provider, state, redirect_uri)
    if not auth_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create authorization URL",
        )

    # 直接重定向到 OAuth 提供商授权页面（标准 OAuth 模式）
    return RedirectResponse(url=auth_url, status_code=302)


class OAuthCallbackRequest(BaseModel):
    """OAuth 回调请求"""

    # 授权码：提供商回调时带回，用于向其换取访问令牌
    code: str
    # state 随机串：发起授权时生成并存入 Redis，回调时用于 CSRF 校验（防跨站请求伪造）
    state: str


# 构造回传给 OAuth 提供商的 redirect_uri（须与发起授权时完全一致，否则提供商会拒绝换取）
def _oauth_redirect_uri(frontend_url: str, provider: str) -> str:
    return f"{frontend_url}/api/auth/oauth/{provider}/callback"


# 构造前端 OAuth 回调处理页地址（换取 token 成功后前端跳转到此页完成登录）
def _frontend_callback_url(frontend_url: str) -> str:
    return f"{frontend_url}/auth/callback"


# 将签发的 token 编码为 URL fragment 参数（放在 # 之后，不随请求发往服务器，也不进日志，更安全）
def _token_fragment(token) -> str:
    return urlencode(
        {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_in": token.expires_in,
        }
    )


# 校验 OAuth state：兼容 _verify_oauth_state 返回协程或同步布尔值两种实现
async def _verify_state(provider: str, state: str, client_ip: str) -> bool:
    coro_or_result = _verify_oauth_state(provider, state, client_ip)
    if isawaitable(coro_or_result):
        result = await coro_or_result
    else:
        result = coro_or_result
    return bool(result)


# 用授权码向 OAuth 提供商换取本系统的 JWT token（内部拼接 redirect_uri 并调用 oauth_service）
async def _exchange_oauth_token(
    request: Request,
    provider: str,
    code: str,
    state: str,
):
    from src.infra.auth.oauth import get_oauth_service

    oauth_service = get_oauth_service()
    oauth_provider = OAuthProvider(provider)

    frontend_url = _get_frontend_url(request)
    redirect_uri = _oauth_redirect_uri(frontend_url, provider)
    token = await oauth_service.handle_callback(oauth_provider, code, state, redirect_uri)
    return frontend_url, token


# POST /oauth/{provider}/callback —— 处理 OAuth 回调（用授权码换 token）
# 兼容两种请求：普通 JSON（前端 AJAX）与 Apple 的 form_post 表单回调
# JSON 请求成功返回 JWT Token；form_post 请求则 302 跳转到前端回调页，token 放在 URL fragment 中
# 安全：先用 state 做 CSRF 校验，失败时——表单流重定向报错、JSON 流抛 400
@router.post("/oauth/{provider}/callback")
async def oauth_callback(http_request: Request, provider: OAuthProviderParam):
    """
    处理 OAuth 回调

    接收授权码，交换 token。JSON 请求返回 JWT；Apple form_post 请求重定向到前端回调页。
    """
    # 依据 Content-Type 判断是表单回调（Apple form_post）还是 JSON 回调，二者取参与出参方式不同
    content_type = http_request.headers.get("content-type", "").lower()
    is_form_post = "application/x-www-form-urlencoded" in content_type or (
        "multipart/form-data" in content_type
    )

    if is_form_post:
        form = await http_request.form()
        callback_request = OAuthCallbackRequest(
            code=str(form.get("code") or ""),
            state=str(form.get("state") or ""),
        )
    else:
        try:
            body = await http_request.json()
            callback_request = OAuthCallbackRequest.model_validate(body)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid OAuth callback payload",
            ) from exc

    # 验证 state 以防止 CSRF 攻击
    client_ip = _get_client_ip(http_request)
    if not await _verify_state(provider, callback_request.state, client_ip):
        logger.warning("[OAuth] Invalid state for %s from %s", provider, client_ip)
        if is_form_post:
            frontend_url = _get_frontend_url(http_request)
            error_params = urlencode({"error": "invalid_state", "provider": provider})
            return RedirectResponse(
                url=f"{frontend_url}/auth/login?{error_params}",
                status_code=302,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth state. Please try logging in again.",
        )

    # state 校验通过后，用授权码向提供商换取本系统 token（失败则按请求类型分别处理）
    frontend_url, token = await _exchange_oauth_token(
        http_request,
        provider,
        callback_request.code,
        callback_request.state,
    )
    if not token:
        if is_form_post:
            error_params = urlencode({"error": "oauth_failed", "provider": provider})
            return RedirectResponse(
                url=f"{frontend_url}/auth/login?{error_params}",
                status_code=302,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OAuth authentication failed",
        )

    if is_form_post:
        return RedirectResponse(
            url=f"{_frontend_callback_url(frontend_url)}#{_token_fragment(token)}",
            status_code=302,
        )

    return token


# GET /oauth/{provider}/callback —— 处理 OAuth 回调（GET 重定向式，供浏览器直接跳转）
# query 参数：code（授权码）、state（CSRF 校验串）
# 成功后 302 跳转前端回调页并通过 URL fragment(#) 传递 token；state 校验失败或换取失败则重定向到登录页并带 error
@router.get("/oauth/{provider}/callback")
async def oauth_callback_get(request: Request, provider: OAuthProviderParam, code: str, state: str):
    """
    处理 OAuth 回调 (GET 请求)

    接收授权码，交换 token 并重定向到前端页面。
    Token 通过 URL fragment (#) 传递，更安全且不会被服务器日志记录。
    """
    # 使用与发起 OAuth 时相同的方式获取 frontend_url，确保 redirect_uri 一致
    frontend_url = _get_frontend_url(request)

    # 验证 state 以防止 CSRF 攻击
    client_ip = _get_client_ip(request)
    if not await _verify_state(provider, state, client_ip):
        logger.warning("[OAuth] Invalid state for %s from %s", provider, client_ip)
        error_params = urlencode({"error": "invalid_state", "provider": provider})
        return RedirectResponse(url=f"{frontend_url}/auth/login?{error_params}", status_code=302)

    frontend_url, token = await _exchange_oauth_token(request, provider, code, state)

    # 构建重定向 URL 到前端的 OAuth 回调处理页面
    callback_url = _frontend_callback_url(frontend_url)

    if not token:
        # 认证失败，重定向到登录页面并显示错误
        error_params = urlencode({"error": "oauth_failed", "provider": provider})
        return RedirectResponse(url=f"{frontend_url}/auth/login?{error_params}", status_code=302)

    # 认证成功，通过 URL fragment 传递 token
    # URL fragment (# 后面的内容) 不会发送到服务器，更安全
    fragment_params = _token_fragment(token)
    return RedirectResponse(url=f"{callback_url}#{fragment_params}", status_code=302)

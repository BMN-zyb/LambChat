"""FastAPI dependencies."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.infra.async_utils import run_blocking_io
from src.infra.auth.jwt import verify_token
from src.infra.logging import get_logger
from src.infra.role.storage import RoleStorage
from src.infra.user.manager import UserManager
from src.infra.user.storage import UserStorage
from src.kernel.schemas.user import TokenPayload

# HTTPBearer 负责从 Authorization: Bearer <token> 头中提取凭证；auto_error=False 表示凭证缺失时不自动抛 403，而是返回 None，交由各依赖自行决定「可选」还是「必需」
security = HTTPBearer(auto_error=False)

logger = get_logger(__name__)

# 进程内认证缓存的 TTL：45 秒。用较短 TTL 在「减少每次请求的用户/角色 DB 查询」与「权限/角色变更能较快生效」之间折中；变更时也会主动调用 clear_auth_cache 立即失效
_AUTH_CACHE_TTL_SECONDS = 45.0
# 缓存最大条目数，超限时先清理过期项、必要时再按插入顺序淘汰最旧项，防止内存无限增长
_AUTH_CACHE_MAX_ENTRIES = 2048
# 缓存结构：token -> (过期的单调时间点, TokenPayload)
_auth_cache: dict[str, tuple[float, TokenPayload]] = {}


def clear_auth_cache() -> None:
    """Clear per-process authenticated user cache after user/role changes."""
    _auth_cache.clear()


# 从进程内缓存读取用户 payload：未命中返回 None；命中但已过期则删除并返回 None；命中且有效则返回深拷贝
def _get_cached_user(token: str) -> TokenPayload | None:
    cached = _auth_cache.get(token)
    if not cached:
        return None

    expires_at, payload = cached
    # 用 time.monotonic() 判断是否过期（单调时钟不受系统时间被回拨/前拨影响）
    if expires_at <= time.monotonic():
        _auth_cache.pop(token, None)
        return None
    # 返回深拷贝，避免调用方修改返回对象时污染缓存中共享的实例
    return payload.model_copy(deep=True)


# 写入进程内缓存：容量达到上限时先清理所有过期项，若仍超限再按 FIFO（最旧插入优先）淘汰，最后存入带过期时间点的深拷贝
def _set_cached_user(token: str, payload: TokenPayload) -> None:
    if len(_auth_cache) >= _AUTH_CACHE_MAX_ENTRIES:
        now = time.monotonic()
        # 收集所有已过期的 key
        expired = [key for key, (expires_at, _) in _auth_cache.items() if expires_at <= now]
        for key in expired:
            _auth_cache.pop(key, None)
        # 清理过期项后仍超限，则按插入顺序淘汰最旧的条目直到腾出空间
        while len(_auth_cache) >= _AUTH_CACHE_MAX_ENTRIES:
            _auth_cache.pop(next(iter(_auth_cache)))

    # 存入 payload 的深拷贝，过期时间点 = 当前单调时间 + TTL
    _auth_cache[token] = (time.monotonic() + _AUTH_CACHE_TTL_SECONDS, payload.model_copy(deep=True))


async def _get_user_roles_and_permissions(user_roles: list[str]) -> tuple[list[str], list[str]]:
    """
    获取用户角色列表和合并后的权限列表

    角色数据通过 RoleStorage 的 Redis 缓存获取，无需额外缓存层。

    Args:
        user_roles: 用户角色列表（从 token 中获取）

    Returns:
        (角色列表, 权限列表)
    """
    role_storage = RoleStorage()
    roles = []
    permissions = set()

    for role_name in user_roles:
        role = await role_storage.get_by_name(role_name)
        if role:
            roles.append(role.name)
            for perm in role.permissions:
                permissions.add(perm if isinstance(perm, str) else perm.value)

    return roles, list(permissions)


# 将同步阻塞的 verify_token（JWT 解码/签名与有效期校验）放入线程池执行，避免阻塞事件循环
async def _verify_token_async(token: str) -> TokenPayload:
    return await run_blocking_io(verify_token, token)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[TokenPayload]:
    """
    获取当前用户（可选）

    从 JWT token 中解析用户信息。
    """
    # 可选认证：没有凭证时直接返回 None（视为匿名），不报错
    if not credentials:
        return None

    try:
        # 优先复用本请求内 get_current_user_required 已解析并缓存的完整用户（含角色/权限）
        cached = getattr(request.state, "current_user", None)
        if isinstance(cached, TokenPayload):
            return cached.model_copy(deep=True)

        token = credentials.credentials
        # 复用 UserContextMiddleware 已解码并存入 request.state.auth_payload 的 payload；没有则再解码一次
        parsed = getattr(request.state, "auth_payload", None)
        payload = (
            parsed.model_copy(deep=True)
            if isinstance(parsed, TokenPayload)
            else await _verify_token_async(token)
        )
        # 可选版本只返回 token 内的原始信息，不查库补全角色/权限（那是 required 版本的职责）
        return payload
    except Exception:
        # 可选依赖：任何解码/校验异常都吞掉并返回 None，视为未认证
        return None


# get_current_user_optional 是 get_current_user 的语义别名，使「可选认证」意图在路由签名中更直观
# Alias for clarity
get_current_user_optional = get_current_user


async def get_current_user_required(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> TokenPayload:
    """
    获取当前用户（必需）

    如果未认证则抛出异常。
    用户信息从数据库动态获取，确保权限变更立即生效。
    """
    # 必需认证：没有凭证直接抛 401
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证信息",
        )

    try:
        token = credentials.credentials
        # 三层缓存逐级命中，尽量避免查库：
        # (1) request.state.current_user —— 同一请求内已解析出的完整用户
        cached_user = getattr(request.state, "current_user", None)
        if isinstance(cached_user, TokenPayload):
            return cached_user.model_copy(deep=True)

        # (2) 进程内 45 秒缓存 —— 命中则回填到 request.state 再返回
        cached = _get_cached_user(token)
        if cached is not None:
            request.state.current_user = cached.model_copy(deep=True)
            return cached

        # (3) 两级缓存均未命中：解码 token（优先复用中间件已解码的结果），随后查库补全用户与角色/权限
        parsed = getattr(request.state, "auth_payload", None)
        payload = (
            parsed.model_copy(deep=True)
            if isinstance(parsed, TokenPayload)
            else await _verify_token_async(token)
        )
        user_id = payload.sub

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的 Token",
            )

        # 从数据库获取用户信息
        user_storage = UserStorage()
        user = await user_storage.get_by_id(user_id)

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户不存在",
            )

        # 从缓存/数据库动态获取角色和权限
        roles, permissions = await _get_user_roles_and_permissions(user.roles)

        # 更新 payload
        payload.username = user.username
        payload.roles = roles
        payload.permissions = permissions

        # 写入进程内缓存与本请求缓存，供后续请求/依赖在 TTL 内复用，减少查库
        _set_cached_user(token, payload)
        request.state.current_user = payload.model_copy(deep=True)

        return payload
    # 业务已明确抛出的 HTTPException（如无效 Token/用户不存在）原样抛出
    except HTTPException:
        raise
    # 其余未知异常统一转换为 401，避免向客户端泄漏内部错误细节
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )


async def get_current_user_from_websocket(
    token: str,
) -> TokenPayload:
    """
    从 WebSocket 查询参数获取当前用户

    用于 WebSocket 连接的认证。
    """
    from src.infra.logging import get_logger

    logger = get_logger(__name__)

    if not token:
        logger.warning("[WebSocket] No token provided")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证信息",
        )

    try:
        # WebSocket 握手不经过 HTTP 中间件/依赖缓存，这里总是重新解码 token 并查库加载最新用户与权限
        payload = await _verify_token_async(token)
        user_id = payload.sub

        if not user_id:
            logger.warning("[WebSocket] Invalid token: no user_id")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的 Token",
            )

        # 从数据库获取用户信息
        user_storage = UserStorage()
        user = await user_storage.get_by_id(user_id)

        if not user:
            logger.warning(f"[WebSocket] User not found: {user_id}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户不存在",
            )

        # 从缓存/数据库动态获取角色和权限
        roles, permissions = await _get_user_roles_and_permissions(user.roles)

        # 组装并返回携带最新角色/权限的 TokenPayload（保留原 token 的 sub/exp/iat）
        # 创建新的 TokenPayload，返回用户信息
        return TokenPayload(
            sub=payload.sub,
            username=user.username,
            roles=roles,
            permissions=permissions,
            exp=payload.exp,
            iat=payload.iat,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[WebSocket] Auth error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        )


async def get_user_manager() -> UserManager:
    """获取用户管理器"""
    return UserManager()


def require_permissions(*permissions: str):
    """
    权限检查依赖

    用法:
        @router.get("/", dependencies=[Depends(require_permissions("user:read"))])
    """

    # 实际的依赖：先经 get_current_user_required 完成认证并取到用户的权限集合（RBAC 中权限已由 用户->角色->权限 展开合并而来）
    async def checker(
        user: TokenPayload = Depends(get_current_user_required),
    ) -> TokenPayload:
        user_permissions = set(user.permissions)
        # 要求「全部」列出的权限都具备（AND 语义），缺任意一个即返回 403
        for perm in permissions:
            if perm not in user_permissions:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"缺少权限: {perm}",
                )
        return user

    # 返回该依赖可调用对象，供 Depends(require_permissions(...)) 挂到路由上；校验通过时把 user 传递下去
    return checker

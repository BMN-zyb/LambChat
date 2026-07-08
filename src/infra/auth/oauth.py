"""
OAuth 认证服务

支持 Google、GitHub、Apple OAuth 登录。
"""

import base64
import inspect
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Dict, Optional

import httpx
import jwt
from pydantic import BaseModel

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.user.storage import UserStorage
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.user import OAuthProvider, Token, User, UserCreate

if TYPE_CHECKING:
    from authlib.integrations.httpx_client import AsyncOAuth2Client

logger = get_logger(__name__)

# HTTP 请求超时设置（秒）
HTTP_TIMEOUT = 10.0
# Apple 的 client_secret 是一个自签 JWT，此常量为其有效期（Apple 上限约 6 个月）
APPLE_CLIENT_SECRET_EXPIRE_DAYS = 180


def _build_apple_client_secret() -> str:
    """Build the Sign in with Apple client_secret JWT from private key settings."""
    # 与 Google/GitHub 不同，Apple 不提供静态密钥，
    # 需用开发者私钥（ES256）现签一个短期 JWT 作为 client_secret
    private_key = settings.OAUTH_APPLE_CLIENT_SECRET
    team_id = settings.OAUTH_APPLE_TEAM_ID
    key_id = settings.OAUTH_APPLE_KEY_ID
    client_id = settings.OAUTH_APPLE_CLIENT_ID

    # 任一必需配置缺失则无法签发，原样返回（视为未配置 Apple 登录）
    if not private_key or not team_id or not key_id or not client_id:
        return private_key

    now = utc_now()
    # 按 Apple 规范组装声明：iss=团队ID、sub=客户端ID、aud 固定为 Apple 授权端
    payload = {
        "iss": team_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=APPLE_CLIENT_SECRET_EXPIRE_DAYS)).timestamp()),
        "aud": "https://appleid.apple.com",
        "sub": client_id,
    }
    # 配置里的私钥换行常被转义成字面 "\n"，此处还原为真正的换行符
    normalized_private_key = private_key.replace("\\n", "\n")
    # 使用 ES256 算法签名，并在 header 的 kid 中标明所用密钥ID
    return jwt.encode(
        payload,
        normalized_private_key,
        algorithm="ES256",
        headers={"kid": key_id},
    )


class OAuthUserInfo(BaseModel):
    """OAuth 用户信息"""

    # 将 Google/GitHub/Apple 各不相同的用户资料，归一化为统一字段
    provider: OAuthProvider
    oauth_id: str
    email: str
    username: str
    avatar_url: Optional[str] = None


class OAuthService:
    """
    OAuth 服务类

    处理 OAuth 授权流程。
    """

    def __init__(self):
        # 依赖用户存储用于查找/创建/绑定 OAuth 用户
        self.storage = UserStorage()
        # 按 provider 缓存 OAuth 客户端，避免重复创建 HTTP 连接
        self._oauth_clients: Dict[str, "AsyncOAuth2Client"] = {}

    def _get_client(self, provider: OAuthProvider) -> Optional["AsyncOAuth2Client"]:
        """获取 OAuth 客户端"""
        # 命中缓存直接复用
        if provider.value in self._oauth_clients:
            return self._oauth_clients[provider.value]

        # 未配置 client_id/secret 的提供商视为不可用
        client_id, client_secret = self._get_client_credentials(provider)
        if not client_id or not client_secret:
            return None

        # 延迟导入 authlib，避免未使用 OAuth 时的额外依赖开销
        from authlib.integrations.httpx_client import AsyncOAuth2Client

        # 使用 AsyncOAuth2Client 直接创建客户端
        client = AsyncOAuth2Client(
            client_id=client_id,
            client_secret=client_secret,
        )
        # 存入缓存供后续复用
        self._oauth_clients[provider.value] = client
        return client

    async def close(self) -> None:
        """Close cached OAuth HTTP clients."""
        # 拷贝一份再清空缓存，避免关闭过程中并发修改字典
        clients = list(self._oauth_clients.values())
        self._oauth_clients.clear()
        for client in clients:
            # 不同版本客户端关闭方法名可能为 aclose 或 close，做兼容
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close is None:
                continue
            try:
                result = close()
                # 关闭方法可能是协程，需 await
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                # 关闭失败不应影响主流程，仅记录告警
                logger.warning("Failed to close OAuth client: %s", e)

    def _get_client_credentials(self, provider: OAuthProvider) -> tuple[str, str]:
        """获取 OAuth 客户端凭据"""
        # 按提供商返回对应凭据；Apple 的 secret 需即时签发（见上文）
        if provider == OAuthProvider.GOOGLE:
            return settings.OAUTH_GOOGLE_CLIENT_ID, settings.OAUTH_GOOGLE_CLIENT_SECRET
        elif provider == OAuthProvider.GITHUB:
            return settings.OAUTH_GITHUB_CLIENT_ID, settings.OAUTH_GITHUB_CLIENT_SECRET
        elif provider == OAuthProvider.APPLE:
            return settings.OAUTH_APPLE_CLIENT_ID, _build_apple_client_secret()
        return "", ""

    def _get_register_config(self, provider: OAuthProvider) -> Optional[Dict[str, Any]]:
        """获取 OAuth 注册配置"""

        # 各提供商的授权/令牌/用户信息端点地址
        if provider == OAuthProvider.GOOGLE:
            return {
                "api_base_url": "https://www.googleapis.com/oauth2/v2/",
                "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
                "access_token_url": "https://oauth2.googleapis.com/token",
                "userinfo_url": "https://www.googleapis.com/oauth2/v2/userinfo",
            }
        elif provider == OAuthProvider.GITHUB:
            return {
                "api_base_url": "https://api.github.com/",
                "authorize_url": "https://github.com/login/oauth/authorize",
                "access_token_url": "https://github.com/login/oauth/access_token",
            }
        elif provider == OAuthProvider.APPLE:
            return {
                "api_base_url": "https://appleid.apple.com/",
                "authorize_url": "https://appleid.apple.com/auth/authorize",
                "access_token_url": "https://appleid.apple.com/auth/token",
            }
        return None

    def is_provider_enabled(self, provider: OAuthProvider) -> bool:
        """检查 OAuth 提供商是否启用"""
        # 通过配置开关判断某提供商是否对外开放登录入口
        if provider == OAuthProvider.GOOGLE:
            return bool(settings.OAUTH_GOOGLE_ENABLED)
        elif provider == OAuthProvider.GITHUB:
            return bool(settings.OAUTH_GITHUB_ENABLED)
        elif provider == OAuthProvider.APPLE:
            return bool(settings.OAUTH_APPLE_ENABLED)
        return False

    def get_authorization_url(
        self, provider: OAuthProvider, state: str, redirect_uri: str
    ) -> Optional[str]:
        """
        获取 OAuth 授权 URL

        Args:
            provider: OAuth 提供商
            state: CSRF 状态码
            redirect_uri: OAuth 回调 URL（从请求中构建）

        Returns:
            授权 URL 或 None
        """
        # 第一步（授权码流程）：生成引导用户跳转到提供商的登录授权页地址
        # 未启用则不生成
        if not self.is_provider_enabled(provider):
            logger.warning(f"OAuth provider {provider.value} is not enabled")
            return None

        client = self._get_client(provider)
        if not client:
            logger.error(f"Failed to get OAuth client for {provider.value}")
            return None

        try:
            # 不同提供商所需 scope 与参数略有差异，分别构建授权 URL；
            # state 用于抵御 CSRF，回调时需原样校验
            if provider == OAuthProvider.GOOGLE:
                url, _ = client.create_authorization_url(
                    "https://accounts.google.com/o/oauth2/v2/auth",
                    redirect_uri=redirect_uri,
                    state=state,
                    scope="openid email profile",
                )
                return url
            elif provider == OAuthProvider.GITHUB:
                url, _ = client.create_authorization_url(
                    "https://github.com/login/oauth/authorize",
                    redirect_uri=redirect_uri,
                    state=state,
                    scope="user:email read:user",
                )
                return url
            elif provider == OAuthProvider.APPLE:
                # Apple 在请求 name/email 等 scope 时要求以 form_post 方式回传
                url, _ = client.create_authorization_url(
                    "https://appleid.apple.com/auth/authorize",
                    redirect_uri=redirect_uri,
                    state=state,
                    scope="name email",
                    response_mode="form_post",
                )
                return url
        except Exception as e:
            logger.error(f"Failed to create authorization URL for {provider.value}: {e}")
            return None

        return None

    async def handle_callback(
        self, provider: OAuthProvider, code: str, state: str, redirect_uri: str
    ) -> Optional[Token]:
        """
        处理 OAuth 回调

        Args:
            provider: OAuth 提供商
            code: 授权码
            state: CSRF 状态码
            redirect_uri: OAuth 回调 URL（从请求中构建）

        Returns:
            Token 或 None
        """
        # 第二步（授权码流程）：用户授权后提供商回调，携带 code；
        # 本方法用 code 换 token、拉取用户信息、落库并签发本系统 JWT
        if not self.is_provider_enabled(provider):
            logger.warning(f"OAuth provider {provider.value} is not enabled")
            return None

        client = self._get_client(provider)
        if not client:
            logger.error(f"Failed to get OAuth client for {provider.value}")
            return None

        try:
            # 获取 token URL
            register_config = self._get_register_config(provider)
            token_url = register_config.get("access_token_url") if register_config else None

            # 交换 code 获取 token
            # redirect_uri 必须与授权阶段完全一致，否则提供商会拒绝
            token = await client.fetch_token(
                token_url,
                code=code,
                redirect_uri=redirect_uri,
            )

            # 获取用户信息
            # 拿到 access_token 后调用各提供商的用户信息端点
            user_info = await self._get_user_info(provider, token)
            if not user_info:
                logger.error(f"Failed to get user info from {provider.value}")
                return None

            # 查找或创建用户
            # 已存在则复用/绑定，否则新建本地用户
            user = await self._find_or_create_user(user_info)
            if not user:
                logger.error("Failed to find or create user")
                return None

            # 生成 JWT token
            # 登录成功后签发本系统的访问令牌与刷新令牌（延迟导入以避免循环依赖）
            from src.infra.auth.jwt import create_access_token, create_refresh_token

            access_token = create_access_token(user_id=user.id)
            refresh_token = create_refresh_token(user_id=user.id, username=user.username)

            return Token(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=settings.ACCESS_TOKEN_EXPIRE_HOURS * 3600,
            )
        except Exception as e:
            # 整个回调流程任一环节异常都归一化为 None，由上层决定如何提示
            logger.error(f"Failed to handle OAuth callback for {provider.value}: {e}")
            return None

    async def _get_user_info(
        self, provider: OAuthProvider, token: Dict[str, Any]
    ) -> Optional[OAuthUserInfo]:
        """获取 OAuth 用户信息"""
        # 按提供商分派到各自的用户信息解析方法
        try:
            if provider == OAuthProvider.GOOGLE:
                return await self._get_google_user_info(token)
            elif provider == OAuthProvider.GITHUB:
                return await self._get_github_user_info(token)
            elif provider == OAuthProvider.APPLE:
                return await self._get_apple_user_info(token)
        except Exception as e:
            logger.error(f"Failed to get user info from {provider.value}: {e}")
        return None

    async def _get_google_user_info(self, token: Dict[str, Any]) -> Optional[OAuthUserInfo]:
        """获取 Google 用户信息"""
        access_token = token.get("access_token")
        if not access_token:
            return None

        # 携带 Bearer 令牌调用 Google userinfo 端点
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = resp.json()

        # 归一化为统一的 OAuthUserInfo；无 name 时用邮箱前缀兜底用户名
        return OAuthUserInfo(
            provider=OAuthProvider.GOOGLE,
            oauth_id=data["id"],
            email=data["email"],
            username=data.get("name", data["email"].split("@")[0]),
            avatar_url=data.get("picture"),
        )

    async def _get_github_user_info(self, token: Dict[str, Any]) -> Optional[OAuthUserInfo]:
        """获取 GitHub 用户信息"""
        access_token = token.get("access_token")
        if not access_token:
            return None

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            # 获取用户信息
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = resp.json()
            # 防御：GitHub 异常时可能返回非字典结构
            if not isinstance(data, dict):
                logger.error("Unexpected GitHub user payload: %s", type(data).__name__)
                return None

            # 获取邮箱（如果用户没有公开邮箱）
            # 用户可能隐藏了公开邮箱，此时需额外调用 emails 端点获取
            email = data.get("email")
            if not email:
                # 最多重试 2 次，应对偶发的非预期响应
                for attempt in range(2):
                    resp = await client.get(
                        "https://api.github.com/user/emails",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                    emails = resp.json()
                    if isinstance(emails, list):
                        # 优先级：主邮箱且已验证 > 任一已验证 > 任一邮箱
                        primary_verified = next(
                            (
                                item.get("email")
                                for item in emails
                                if isinstance(item, dict)
                                and item.get("primary")
                                and item.get("verified")
                                and item.get("email")
                            ),
                            None,
                        )
                        any_verified = next(
                            (
                                item.get("email")
                                for item in emails
                                if isinstance(item, dict)
                                and item.get("verified")
                                and item.get("email")
                            ),
                            None,
                        )
                        any_email = next(
                            (
                                item.get("email")
                                for item in emails
                                if isinstance(item, dict) and item.get("email")
                            ),
                            None,
                        )
                        email = primary_verified or any_verified or any_email
                        break

                    # 非列表说明响应异常，记录其 message（若有）便于排查
                    github_message = emails.get("message") if isinstance(emails, dict) else None
                    logger.error(
                        "Unexpected GitHub emails payload: %s%s",
                        type(emails).__name__,
                        f" ({github_message})" if github_message else "",
                    )
                    # 首次失败再重试一次，仍失败则退出循环
                    if attempt == 0:
                        continue

        # 无论如何都拿不到邮箱则无法建号
        if not email:
            logger.error("No email found for GitHub user")
            return None

        # GitHub 的 id 为数字，转成字符串统一存储；无 login 时用邮箱前缀兜底
        return OAuthUserInfo(
            provider=OAuthProvider.GITHUB,
            oauth_id=str(data["id"]),
            email=email,
            username=data.get("login", email.split("@")[0]),
            avatar_url=data.get("avatar_url"),
        )

    async def _get_apple_user_info(self, token: Dict[str, Any]) -> Optional[OAuthUserInfo]:
        """
        获取 Apple 用户信息

        验证 Apple ID Token 的签名，确保令牌未被伪造。
        """
        # Apple 不提供 userinfo 端点，用户信息全部编码在回调的 id_token（JWT）中，
        # 因此必须用 Apple 公钥验签后再取其中声明
        id_token = token.get("id_token")
        if not id_token:
            logger.warning("Apple OAuth: No id_token in response")
            return None

        try:
            # 获取 Apple 公钥
            # Apple 公钥集（JWKS）会轮换，每次实时拉取
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                jwks_resp = await client.get("https://appleid.apple.com/auth/keys")
                jwks_data = jwks_resp.json()

            # 解码 JWT header 获取 kid
            # 仅解 header（不验签）以拿到密钥ID，用于匹配对应公钥；放到线程池执行避免阻塞事件循环
            header = await run_blocking_io(_decode_apple_token_header, id_token)
            kid = header.get("kid")

            # 找到匹配的公钥
            # 从 JWKS 中挑出与 id_token header.kid 一致的那把公钥
            jwk = None
            for key in jwks_data.get("keys", []):
                if key.get("kid") == kid:
                    jwk = key
                    break

            if not jwk:
                logger.error(f"Apple OAuth: No matching public key found for kid={kid}")
                return None

            # 用选定公钥验签并校验 iss/aud，返回可信的声明集合
            claims = await run_blocking_io(
                _decode_apple_identity_token,
                id_token,
                jwk,
                settings.OAUTH_APPLE_CLIENT_ID,
            )

            # sub 是 Apple 用户唯一标识，缺失则视为无效
            if "sub" not in claims:
                logger.warning("Apple OAuth: Missing 'sub' in id_token claims")
                return None

            # Apple 可能不返回邮箱：有邮箱用其前缀作用户名，否则用 apple_+sub 前缀兜底
            return OAuthUserInfo(
                provider=OAuthProvider.APPLE,
                oauth_id=claims["sub"],
                email=claims.get("email", ""),
                username=claims.get("email", "").split("@")[0]
                if claims.get("email")
                else f"apple_{claims['sub'][:8]}",
                avatar_url=None,
            )
        except Exception as e:
            logger.error(f"Apple OAuth: Failed to verify id_token: {e}")
            return None

    async def _find_or_create_user(self, user_info: OAuthUserInfo) -> Optional[User]:
        """
        查找或创建用户（并发安全）

        使用 try-except 捕获重复用户名错误并自动重试。

        Args:
            user_info: OAuth 用户信息

        Returns:
            用户对象或 None
        """
        # 延迟导入业务异常，避免模块级循环依赖
        from src.kernel.exceptions import ValidationError

        # 尝试通过 oauth_id 查找用户
        # 优先级 1：同一提供商 + oauth_id 已绑定过，直接返回该用户
        user = await self.storage.get_by_oauth(user_info.provider.value, user_info.oauth_id)
        if user:
            return User.model_validate(user.model_dump())

        # 尝试通过邮箱查找用户（如果已存在则绑定 OAuth）
        # 优先级 2：邮箱已注册（如本地注册用户）则把此次 OAuth 绑定到该账号，避免重复建号
        existing_user = await self.storage.get_by_email(user_info.email)
        if existing_user:
            # 绑定 OAuth 到现有用户
            from src.kernel.schemas.user import UserUpdate

            await self.storage.update(
                existing_user.id,
                UserUpdate(
                    oauth_provider=user_info.provider,
                    oauth_id=user_info.oauth_id,
                    # 如果用户没有头像，更新头像
                    avatar_url=user_info.avatar_url or existing_user.avatar_url,
                ),
            )
            return await self.storage.get_by_id(existing_user.id)

        # 创建新用户 - 使用重试机制处理并发用户名冲突
        # 优先级 3：全新用户。用户名可能撞车（唯一约束），故带重试与随机后缀
        base_username = user_info.username
        max_retries = 10

        for attempt in range(max_retries):
            if attempt == 0:
                # 首次尝试直接用原始用户名
                username = base_username
            else:
                # 添加随机后缀以避免冲突
                import random
                import string

                suffix = "".join(random.choices(string.digits, k=4))
                username = f"{base_username}_{suffix}"

            # 为新用户分配默认角色（与 UserManager.register 逻辑一致）
            # 系统首个用户自动成为 admin，其余按配置的默认角色（缺省 user）
            existing_users = await self.storage.list_users(limit=1)
            if not existing_users:
                default_roles = ["admin"]
            else:
                default_role = settings.DEFAULT_USER_ROLE
                default_roles = [default_role or "user"]

            user_data = UserCreate(
                username=username,
                email=user_info.email,
                avatar_url=user_info.avatar_url,
                oauth_provider=user_info.provider,
                oauth_id=user_info.oauth_id,
                roles=default_roles,
            )

            try:
                user = await self.storage.create(user_data)
                return User.model_validate(user.model_dump())
            except ValidationError as e:
                # 如果是用户名冲突且还有重试机会，继续尝试
                # 仅对“用户名冲突”重试；邮箱冲突或其他错误直接抛给上层
                if "用户名" in str(e) and attempt < max_retries - 1:
                    logger.debug(
                        f"Username {username} already exists, retrying... (attempt {attempt + 1})"
                    )
                    continue
                # 如果是邮箱冲突或其他错误，直接抛出
                raise

        # 不应该到达这里，但为了完整性
        # 重试用尽仍失败（极端并发）：返回 None 兜底
        logger.error(f"Failed to create user after {max_retries} attempts")
        return None


# 单例
# 进程内共享单个 OAuthService，复用其客户端缓存与存储连接
_oauth_service: Optional[OAuthService] = None


def get_oauth_service() -> OAuthService:
    """获取 OAuth 服务单例"""
    # 懒加载：首次调用时才实例化
    global _oauth_service
    if _oauth_service is None:
        _oauth_service = OAuthService()
    return _oauth_service


async def close_oauth_service() -> None:
    """Close the singleton OAuth service without creating it during shutdown."""
    # 关闭时先取出再置空，确保不会在停机阶段又新建实例
    global _oauth_service
    service = _oauth_service
    _oauth_service = None
    if service is not None:
        await service.close()


def _decode_apple_token_header(id_token: str) -> dict[str, Any]:
    """Decode Apple JWT header off the event loop."""
    # 取 JWT 的第一段（header），base64url 解码；不做验签，仅为读取 kid
    header_b64 = id_token.split(".")[0]
    # base64url 需要按 4 的倍数补齐 "=" 填充位
    padding = 4 - len(header_b64) % 4
    if padding != 4:
        header_b64 += "=" * padding
    return json.loads(base64.urlsafe_b64decode(header_b64))


def _decode_apple_identity_token(
    id_token: str, jwk: dict[str, Any], client_id: str
) -> dict[str, Any]:
    """Decode and verify Apple identity token off the event loop."""
    from authlib.jose import JsonWebKey, jwt

    # 将 JWKS 中的 JWK 导入为公钥对象
    public_key = JsonWebKey.import_key(jwk)
    # 验签并强制校验 iss（必须为 Apple）与 aud（必须为本应用 client_id），防伪造
    return jwt.decode(
        id_token,
        public_key,
        claims_options={
            "iss": {"essential": True, "values": ["https://appleid.apple.com"]},
            "aud": {"essential": True, "values": [client_id]},
        },
    )

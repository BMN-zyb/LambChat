"""Resend email service implementation."""

# 启用未来注解语义：使类型注解延迟求值，允许在类内部引用尚未定义完成的自身类型（如 Optional[EmailService]）
from __future__ import annotations

# asyncio：提供异步锁与 sleep，用于单例/客户端的并发安全与重试退避
import asyncio
# json：解析 RESEND_ACCOUNTS 环境变量中的账号配置 JSON
import json
# secrets：生成密码学安全的随机 token（密码重置/邮箱验证）
import secrets
# time：记录配置缓存加载时间戳，实现热重载（TTL 判断）
import time
from datetime import datetime, timedelta
# formataddr：将「显示名 + 邮箱地址」格式化为标准的发件人字段
from email.utils import formataddr
from typing import Optional

# httpx：异步 HTTP 客户端，直接调用 Resend REST API（避免使用 SDK 引入全局状态）
import httpx

# run_blocking_io：把同步阻塞操作丢到线程池执行，避免阻塞事件循环
from src.infra.async_utils import run_blocking_io
# EmailTemplate：HTML 邮件模板渲染器（含 XSS 转义）
from src.infra.email.template import EmailTemplate
# get_texts：按语言获取邮件文案
from src.infra.email.texts import get_texts
from src.infra.logging import get_logger
# utc_now：统一使用 UTC 时间，避免时区歧义
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings

logger = get_logger(__name__)

# Resend 邮件发送 API 的固定端点
RESEND_API_URL = "https://api.resend.com/emails"
# 允许配置的 Resend 账号数量上限，超出部分会被截断（防止误配置导致资源占用）
RESEND_ACCOUNTS_MAX = 20


class EmailService:
    """Email service using Resend API.

    Provides email functionality for:
    - Password reset
    - Email verification
    - Welcome emails

    Supports multiple accounts with round-robin rotation.
    Each account can have its own API key and sender address.

    Uses httpx for direct API calls to avoid global state issues.
    """

    # 单例实例引用（进程内共享一个 EmailService）
    _instance: Optional[EmailService] = None
    # 保护单例创建、账号缓存加载、轮询索引更新的异步锁
    _lock = asyncio.Lock()
    # 单独用于 HTTP 客户端惰性初始化的锁，避免并发下重复创建客户端
    _http_client_lock = asyncio.Lock()

    def __init__(self) -> None:
        """Initialize the email service."""
        # 是否启用邮件服务（由配置项 EMAIL_ENABLED 控制的开关）
        self._enabled = settings.EMAIL_ENABLED
        # 账号配置缓存，None 表示尚未加载；用于配置热重载
        self._accounts_cache: Optional[list[dict[str, str]]] = None
        # 账号配置最近一次加载的时间戳（秒），配合 TTL 判断是否需要刷新
        self._config_loaded_at: float = 0
        # 轮询发送的当前账号下标（round-robin 起点）
        self._current_index = 0
        # 密码重置 token 的有效期（小时），来自全局配置
        self._reset_expire_hours = settings.PASSWORD_RESET_EXPIRE_HOURS
        # 复用的异步 HTTP 客户端，惰性创建以复用连接池
        self._http_client: Optional[httpx.AsyncClient] = None

        if self._enabled:
            logger.info("[EmailService] Email service enabled")
        else:
            logger.info("[EmailService] Email service disabled")

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client lazily with thread-safe initialization."""
        # 双重检查锁定：先无锁快速判断，未创建时再加锁创建，兼顾性能与并发安全
        if self._http_client is None:
            async with self._http_client_lock:
                if self._http_client is None:
                    self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    def _parse_accounts(self) -> list[dict[str, str]]:
        """Parse account configurations from RESEND_ACCOUNTS JSON."""
        # 解析结果列表：每个元素是一个规范化的账号字典
        accounts: list[dict[str, str]] = []
        resend_accounts = settings.RESEND_ACCOUNTS
        # 未配置任何账号则直接返回空列表
        if not resend_accounts:
            return accounts

        try:
            # 配置可能是 JSON 字符串，需要先反序列化为 Python 对象
            if isinstance(resend_accounts, str):
                resend_accounts = json.loads(resend_accounts)
            if isinstance(resend_accounts, list):
                # 超过上限时打印告警并只取前 RESEND_ACCOUNTS_MAX 个
                if len(resend_accounts) > RESEND_ACCOUNTS_MAX:
                    logger.warning(
                        "[EmailService] RESEND_ACCOUNTS has %d entries; using first %d",
                        len(resend_accounts),
                        RESEND_ACCOUNTS_MAX,
                    )
                resend_accounts = resend_accounts[:RESEND_ACCOUNTS_MAX]
                # 逐个校验并规范化账号：必须是含 api_key 的字典，缺失字段填默认值
                for acc in resend_accounts:
                    if isinstance(acc, dict) and acc.get("api_key"):
                        accounts.append(
                            {
                                "api_key": str(acc.get("api_key", "")),
                                "email_from": str(acc.get("email_from", "noreply@example.com")),
                                "email_from_name": str(acc.get("email_from_name", "LambChat")),
                            }
                        )
        # 配置格式错误时不抛出异常，仅告警并返回已解析部分，保证服务可降级
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("[EmailService] Failed to parse RESEND_ACCOUNTS: %s", e)

        return accounts

    async def _get_accounts(self) -> list[dict[str, str]]:
        """Get accounts with hot-reload support."""
        # 缓存命中且未超过 60 秒 TTL：直接返回缓存，避免频繁解析配置
        if self._accounts_cache is not None and time.time() - self._config_loaded_at < 60:
            return self._accounts_cache

        async with self._lock:
            # 加锁后再次检查（双重检查），防止并发下重复解析
            if self._accounts_cache is not None and time.time() - self._config_loaded_at < 60:
                return self._accounts_cache

            # 解析属于阻塞型 IO/CPU 操作，放入线程池执行避免阻塞事件循环
            self._accounts_cache = await run_blocking_io(self._parse_accounts)
            self._config_loaded_at = time.time()

            if self._accounts_cache:
                logger.info("[EmailService] Loaded %d Resend account(s)", len(self._accounts_cache))
            else:
                logger.warning("[EmailService] No accounts configured")

            return self._accounts_cache

    def _mask_api_key(self, key: str) -> str:
        """Mask API key for safe logging."""
        # 太短的 key 直接完全遮蔽，避免日志泄露密钥
        if not key or len(key) < 8:
            return "***"
        # 只保留首尾 4 位，中间用省略号替代，便于排障又不泄露
        return key[:4] + "..." + key[-4:]

    async def _get_next_account(self) -> Optional[dict[str, str]]:
        """Get next account using round-robin rotation."""
        accounts = await self._get_accounts()
        # 无可用账号时返回 None，由调用方决定如何降级
        if not accounts:
            return None
        async with self._lock:
            # 取当前下标账号后将下标向前推进并对账号数取模，实现循环轮询
            account = accounts[self._current_index]
            self._current_index = (self._current_index + 1) % len(accounts)
            # 返回副本，避免调用方修改污染缓存中的账号配置
            return account.copy()

    @classmethod
    async def get_instance(cls) -> EmailService:
        """Get singleton instance of EmailService."""
        # 双重检查锁定创建单例，保证并发下只初始化一次
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def is_enabled(self) -> bool:
        """Check if email service is enabled (config-level only).

        Note: account availability is checked separately in _get_next_account().
        """
        return self._enabled

    def _get_from_address(self, account: dict[str, str]) -> str:
        """Get formatted sender address from account."""
        # 将显示名与邮箱组合为「名称 <邮箱>」形式的发件人头
        return formataddr((account.get("email_from_name", ""), account.get("email_from", "")))

    def generate_token(self) -> str:
        """Generate a secure random token for password reset or email verification."""
        # 使用 secrets 生成 URL 安全的高熵随机串，适合放入链接
        return secrets.token_urlsafe(32)

    def get_token_expiry(self, hours: Optional[int] = None) -> datetime:
        """Get token expiry datetime."""
        # 未指定小时数则使用默认的密码重置有效期
        if hours is None:
            hours = self._reset_expire_hours
        # 以当前 UTC 时间为基准加上有效期得到过期时间点
        return utc_now() + timedelta(hours=hours)

    async def _send_email(
        self,
        account: dict[str, str],
        to_email: str,
        subject: str,
        html_content: str,
        text_content: str,
        max_retries: int = 3,
    ) -> bool:
        """Send email via Resend API using httpx with retry logic."""
        # 记录最后一次异常，供所有重试耗尽后统一打印
        last_error: Optional[Exception] = None

        # 最多重试 max_retries 次，配合指数退避处理限流与瞬时故障
        for attempt in range(max_retries):
            try:
                client = await self._get_http_client()
                # 调用 Resend 发信接口：Bearer 鉴权 + JSON body（含 html 与纯文本两种正文）
                response = await client.post(
                    RESEND_API_URL,
                    headers={
                        "Authorization": f"Bearer {account['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": self._get_from_address(account),
                        "to": [to_email],
                        "subject": subject,
                        "html": html_content,
                        "text": text_content,
                    },
                )

                # 200 表示发送成功，记录 Resend 返回的邮件 id 便于追踪
                if response.status_code == 200:
                    data = response.json()
                    masked_key = self._mask_api_key(account["api_key"])
                    logger.info(
                        "[EmailService] Email sent to %s via key %s, id=%s",
                        to_email,
                        masked_key,
                        data.get("id", "unknown"),
                    )
                    return True
                # 429 限流：优先遵循服务端 Retry-After，并与指数退避取较小值后等待重试
                elif response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    wait_time = min(retry_after, 2**attempt * 5)
                    logger.warning(
                        "[EmailService] Rate limited sending to %s, waiting %ds (attempt %d/%d)",
                        to_email,
                        wait_time,
                        attempt + 1,
                        max_retries,
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(wait_time)
                    continue
                # 5xx 服务端错误：属于可重试故障，指数退避后重试
                elif response.status_code >= 500:
                    wait_time = 2**attempt
                    logger.error(
                        "[EmailService] Server error (HTTP %d) sending to %s, retrying in %ds (attempt %d/%d): %s",
                        response.status_code,
                        to_email,
                        wait_time,
                        attempt + 1,
                        max_retries,
                        response.text[:200],
                    )
                    last_error = Exception(f"HTTP {response.status_code}: {response.text[:200]}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(wait_time)
                    continue
                # 其余 4xx 客户端错误（如鉴权失败、参数错误）不可重试，直接失败返回
                else:
                    logger.error(
                        "[EmailService] Failed to send email to %s: HTTP %d - %s",
                        to_email,
                        response.status_code,
                        response.text[:200],
                    )
                    return False

            # 超时属于瞬时故障，指数退避后重试
            except httpx.TimeoutException as e:
                wait_time = 2**attempt
                logger.warning(
                    "[EmailService] Timeout sending to %s, retrying in %ds (attempt %d/%d): %s",
                    to_email,
                    wait_time,
                    attempt + 1,
                    max_retries,
                    str(e),
                )
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait_time)
                continue

            # 网络错误（连接中断等）同样可重试
            except httpx.NetworkError as e:
                wait_time = 2**attempt
                logger.warning(
                    "[EmailService] Network error sending to %s, retrying in %ds (attempt %d/%d): %s",
                    to_email,
                    wait_time,
                    attempt + 1,
                    max_retries,
                    str(e),
                )
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait_time)
                continue

            # 未预期的异常：无法确定是否可重试，记录堆栈后直接跳出循环
            except Exception as e:
                logger.error(
                    "[EmailService] Unexpected error sending to %s: %s",
                    to_email,
                    e,
                    exc_info=True,
                )
                last_error = e
                break

        # 所有重试均失败，记录最终错误并返回 False
        logger.error(
            "[EmailService] Failed to send email to %s after %d attempts: %s",
            to_email,
            max_retries,
            last_error,
        )
        return False

    async def send_password_reset_email(
        self, to_email: str, username: str, reset_token: str, base_url: str, lang: str = "en"
    ) -> bool:
        """Send password reset email.

        Args:
            to_email: Recipient email address.
            username: User's username for personalization.
            reset_token: Password reset token.
            base_url: Base URL for constructing reset link.
            lang: 2-letter language code (en, zh, ja, ko, ru).

        Returns:
            True if email sent successfully, False otherwise.
        """
        # 服务未启用直接返回失败，避免无谓处理
        if not self.is_enabled():
            logger.warning("[EmailService] Cannot send email: service not enabled")
            return False

        # 轮询取一个可用发信账号；无账号则失败返回
        account = await self._get_next_account()
        if not account:
            logger.warning("[EmailService] No accounts available")
            return False

        # 拼接密码重置链接（去掉 base_url 结尾多余斜杠再拼路径，token 作为查询参数）
        reset_url = base_url.rstrip("/") + "/auth/reset-password?token=" + reset_token
        from_name = account.get("email_from_name", "LambChat")
        expire_hours = str(self._reset_expire_hours)
        icon_url = base_url.rstrip("/") + "/icons/icon.svg"
        # 对用户名做 HTML 转义，防止注入到 HTML 邮件正文造成 XSS
        safe_username = EmailTemplate._escape_html(username)

        # 按语言取密码重置邮件的文案模板
        texts = get_texts(lang, "password_reset")
        subject = texts["subject"].format(from_name=from_name)
        footer = (
            texts["footer"].format(from_name=from_name, hours=expire_hours)
            if texts["footer"]
            else None
        )

        # 渲染 HTML 版正文（含品牌图标、标题、按钮等）
        html_content = EmailTemplate.render(
            title=from_name,
            icon_url=icon_url,
            heading=texts["heading"],
            greeting=texts["greeting"].format(username=safe_username),
            content=texts["content"].format(from_name=from_name),
            button_url=reset_url,
            button_text=texts["button_text"],
            footer=footer,
        )

        # 生成纯文本版问候语：去掉 <strong> 标签并用原始用户名（纯文本无需转义）
        plain_greeting = (
            texts["greeting"]
            .replace("<strong>", "")
            .replace("</strong>", "")
            .format(username=username)
        )
        # 纯文本备用正文（供不支持 HTML 的客户端展示），<br> 还原为换行
        text_content = f"""{subject}

{plain_greeting}

{texts["content"].format(from_name=from_name)}

{reset_url}

{footer.replace("<br>", "\n") if footer else ""}"""

        return await self._send_email(account, to_email, subject, html_content, text_content)

    async def send_verification_email(
        self, to_email: str, username: str, verify_token: str, base_url: str, lang: str = "en"
    ) -> bool:
        """Send email verification email.

        Args:
            to_email: Recipient email address.
            username: User's username for personalization.
            verify_token: Email verification token.
            base_url: Base URL for constructing verify link.
            lang: 2-letter language code (en, zh, ja, ko, ru).

        Returns:
            True if email sent successfully, False otherwise.
        """
        # 服务未启用直接返回失败
        if not self.is_enabled():
            logger.warning("[EmailService] Cannot send email: service not enabled")
            return False

        # 轮询取一个可用发信账号
        account = await self._get_next_account()
        if not account:
            logger.warning("[EmailService] No accounts available")
            return False

        # 拼接邮箱验证链接：同时带上 token 与 email 两个查询参数供后端校验
        verify_url = (
            base_url.rstrip("/") + "/auth/verify-email?token=" + verify_token + "&email=" + to_email
        )
        from_name = account.get("email_from_name", "LambChat")
        icon_url = base_url.rstrip("/") + "/icons/icon.svg"
        # 用户名 HTML 转义，防止 XSS
        safe_username = EmailTemplate._escape_html(username)

        # 取邮箱验证邮件的本地化文案
        texts = get_texts(lang, "verify_email")
        subject = texts["subject"].format(from_name=from_name)
        footer = texts["footer"].format(from_name=from_name) if texts["footer"] else None

        # 渲染 HTML 正文
        html_content = EmailTemplate.render(
            title=from_name,
            icon_url=icon_url,
            heading=texts["heading"],
            greeting=texts["greeting"].format(username=safe_username),
            content=texts["content"].format(from_name=from_name),
            button_url=verify_url,
            button_text=texts["button_text"],
            footer=footer,
        )

        # 纯文本问候语（去标签，用原始用户名）
        plain_greeting = (
            texts["greeting"]
            .replace("<strong>", "")
            .replace("</strong>", "")
            .format(username=username)
        )
        # 纯文本备用正文
        text_content = f"""{subject}

{plain_greeting}

{texts["content"].format(from_name=from_name)}

{verify_url}

{footer.replace("<br>", "\n") if footer else ""}"""

        return await self._send_email(account, to_email, subject, html_content, text_content)

    async def send_welcome_email(
        self, to_email: str, username: str, base_url: str, lang: str = "en"
    ) -> bool:
        """Send welcome email after registration.

        Args:
            to_email: Recipient email address.
            username: User's username for personalization.
            base_url: Base URL for constructing login link.
            lang: 2-letter language code (en, zh, ja, ko, ru).

        Returns:
            True if email sent successfully, False otherwise.
        """
        # 服务未启用直接返回失败
        if not self.is_enabled():
            logger.warning("[EmailService] Cannot send email: service not enabled")
            return False

        # 轮询取一个可用发信账号
        account = await self._get_next_account()
        if not account:
            logger.warning("[EmailService] No accounts available")
            return False

        # 欢迎邮件引导用户前往登录页
        login_url = base_url.rstrip("/") + "/auth/login"
        from_name = account.get("email_from_name", "LambChat")
        icon_url = base_url.rstrip("/") + "/icons/icon.svg"
        # 用户名 HTML 转义，防止 XSS
        safe_username = EmailTemplate._escape_html(username)

        # 取欢迎邮件的本地化文案（欢迎邮件无 footer）
        texts = get_texts(lang, "welcome")
        subject = texts["subject"].format(from_name=from_name)

        # 渲染 HTML 正文
        html_content = EmailTemplate.render(
            title=from_name,
            icon_url=icon_url,
            heading=texts["heading"],
            greeting=texts["greeting"].format(username=safe_username),
            content=texts["content"].format(from_name=from_name),
            button_url=login_url,
            button_text=texts["button_text"],
        )

        # 纯文本问候语（去标签，用原始用户名）
        plain_greeting = (
            texts["greeting"]
            .replace("<strong>", "")
            .replace("</strong>", "")
            .format(username=username)
        )
        # 纯文本备用正文
        text_content = f"""{subject}

{plain_greeting}

{texts["content"].format(from_name=from_name)}

{login_url}
"""

        return await self._send_email(account, to_email, subject, html_content, text_content)

    async def close(self) -> None:
        """Close the HTTP client and cleanup resources."""
        # 关闭复用的 HTTP 客户端，释放连接池
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
            logger.info("[EmailService] HTTP client closed")
        # 若关闭的正是当前单例，则清空单例引用便于下次重新创建
        if EmailService._instance is self:
            EmailService._instance = None


async def get_email_service() -> EmailService:
    """Get the singleton EmailService instance."""
    return await EmailService.get_instance()


async def close_email_service() -> None:
    """Close the singleton EmailService without creating it during shutdown."""
    # 关闭时直接读取已有单例，避免在进程退出阶段又创建出新实例
    service = EmailService._instance
    if service is not None:
        await service.close()

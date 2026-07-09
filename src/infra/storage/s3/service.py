"""
S3 Storage Service - high-level interface for storage operations.

Supports multiple providers through configuration, with automatic backend selection.
Includes retry mechanism for transient upload failures.
"""

# ---------------------------------------------------------------------------
# 模块说明：S3 对象存储高层服务（多后端选择 + 上传重试 + 进程级单例）
#
# 本模块提供 S3StorageService，屏蔽不同存储后端的差异，对上层给出统一的
# 上传/下载/删除/签名 URL 等能力。三个难点：
#   1. 多后端选择（_get_backend）：按配置 provider 惰性选择并缓存后端——本地
#      文件系统 / 阿里云 OSS / 其余走 MinIO(标准 S3 协议)；阿里云 SDK(oss2) 缺失时
#      优雅降级到用 MinIO 客户端连接 OSS。configure() 换配置后会清缓存以便重建后端。
#   2. 瞬时错误重试（_retry_async）：区分「瞬时错误」（网络/超时/5xx）与「非瞬时
#      错误」（鉴权/校验）——只对前者做指数退避 + 随机抖动重试，后者立即抛出；
#      对第三方 SDK 异常则靠模块名 + 错误关键字启发式判断。每次重试前都把文件指针
#      seek 回起点，否则会从上次读到的位置继续读导致数据缺失。
#   3. 单例管理：类级 _instance 与模块级 _storage_service 配合；get_or_init_storage
#      按当前配置动态决定用对象存储还是本地存储，且仅当配置变化时才重建后端。
# 私有 bucket 上传后会补签一个预签名 URL（有效期不超过云厂商 7 天上限）。
# ---------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import io
import random
import re
import uuid
from collections.abc import Awaitable
from typing import Callable, Optional, TypeVar

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.storage.s3.backends import (
    AliyunOssBackend,
    LocalStorageBackend,
    MinioS3Backend,
)
from src.infra.storage.s3.base import BinaryReadFile, BinaryWriteFile, S3StorageBackend
from src.infra.storage.s3.types import S3Config, S3Provider, UploadResult
from src.infra.utils.datetime import utc_now

logger = get_logger(__name__)

T = TypeVar("T")

# Retry configuration
UPLOAD_MAX_RETRIES = 3
UPLOAD_RETRY_BACKOFF_BASE = 2  # seconds, exponential backoff base
UPLOAD_RETRY_BACKOFF_JITTER = 1  # seconds, random jitter


# S3 存储高层服务：持有 S3Config 与惰性创建的后端实例，所有 upload_* 最终都汇聚到
# upload_stream_to_key，统一走「大小校验 -> 选后端 -> 带重试上传 -> 私有桶补签名」流程
class S3StorageService:
    """
    S3 Storage Service

    Provides a high-level interface for S3-compatible storage operations.
    Supports multiple providers through configuration.
    """

    # 进程级单例句柄，通过 get_instance() 访问；与模块级的 _storage_service 单例配合使用
    # （get_instance 保证"只有一个类级别实例"，模块级函数则负责该实例的配置初始化/替换/关闭）。
    _instance: Optional["S3StorageService"] = None

    def __init__(self, config: Optional[S3Config] = None):
        # 具体后端延迟创建（首次调用 _get_backend 时才实例化），避免构造阶段就发起网络/文件系统操作。
        self._backend: Optional[S3StorageBackend] = None
        if config:
            self._config = config
        else:
            self._config = S3Config()

    @classmethod
    def get_instance(cls) -> "S3StorageService":
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def configure(self, config: S3Config) -> None:
        """Configure the storage service"""
        # 切换配置后必须清空已缓存的后端实例，下次访问时会按新配置重新选择/创建后端。
        self._config = config
        self._backend = None

    @property
    def is_local(self) -> bool:
        """Whether the storage backend is local filesystem."""
        return self._config.provider == S3Provider.LOCAL

    @staticmethod
    async def _retry_async(
        func: Callable[..., "Awaitable[T]"],
        max_retries: int = UPLOAD_MAX_RETRIES,
        label: str = "operation",
    ) -> T:
        """
        Execute an async function with exponential backoff retry on transient errors.

        Retries on network/timeout errors; does NOT retry on validation errors.
        """
        # 这是本文件的一个难点：需要区分"瞬时性错误"（网络抖动、超时、对象存储 SDK 报出的
        # 5xx/连接类错误）与"非瞬时性错误"（鉴权失败、参数校验错误等），只对前者做指数退避重试，
        # 后者应立即向上抛出，避免无意义的重试拖慢失败反馈。
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return await func()
            except (ConnectionError, TimeoutError, OSError) as e:
                # 标准库层面的网络异常，直接判定为可重试。
                last_exc = e
                if attempt < max_retries:
                    backoff = UPLOAD_RETRY_BACKOFF_BASE**attempt + random.uniform(
                        0, UPLOAD_RETRY_BACKOFF_JITTER
                    )
                    logger.warning(
                        f"Upload {label} failed (attempt {attempt}/{max_retries}), "
                        f"retrying in {backoff:.1f}s: {e}"
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(f"Upload {label} failed after {max_retries} attempts: {e}")
            except Exception as e:
                # Non-transient errors (e.g. auth, validation) — raise immediately
                # 对于第三方 SDK（阿里云 oss2 / MinIO）抛出的异常，无法直接按异常类型判断是否瞬时，
                # 只能通过检查异常所属模块 + 错误信息中的关键字（连接、超时、5xx 等）来启发式判断。
                if "oss2" in type(e).__module__ or "minio" in type(e).__module__:
                    # Check if it's a server/network error from the SDK
                    err_lower = str(e).lower()
                    if any(
                        kw in err_lower
                        for kw in (
                            "connection",
                            "timeout",
                            "timed out",
                            "network",
                            "temporary",
                            "service unavailable",
                            "internal server error",
                            "503",
                            "500",
                            "502",
                        )
                    ):
                        last_exc = e
                        if attempt < max_retries:
                            backoff = UPLOAD_RETRY_BACKOFF_BASE**attempt + random.uniform(
                                0, UPLOAD_RETRY_BACKOFF_JITTER
                            )
                            logger.warning(
                                f"Upload {label} failed (attempt {attempt}/{max_retries}), "
                                f"retrying in {backoff:.1f}s: {e}"
                            )
                            await asyncio.sleep(backoff)
                            continue
                        logger.error(f"Upload {label} failed after {max_retries} attempts: {e}")
                # 命中不了"疑似瞬时错误"特征的异常（包括鉴权/校验错误），直接重新抛出，不再重试。
                raise

        raise last_exc  # type: ignore[misc]

    def _get_backend(self) -> S3StorageBackend:
        """Get or create the storage backend"""
        # 按配置中的 provider 惰性选择并创建具体后端实现；创建后缓存复用，直到 configure() 重新配置。
        if self._backend is None:
            if self._config.provider == S3Provider.LOCAL:
                self._backend = LocalStorageBackend(self._config)
            elif self._config.provider == S3Provider.ALIYUN:
                try:
                    if AliyunOssBackend is None:
                        raise ImportError
                    self._backend = AliyunOssBackend(self._config)
                except ImportError:
                    # 阿里云 SDK（oss2）未安装时优雅降级：用兼容 S3 协议的 MinIO 客户端连接 OSS
                    # （阿里云 OSS 对 S3 协议有一定兼容性，但可能存在细节差异，因此仅作为兜底方案）。
                    logger.warning(
                        "Aliyun OSS SDK not available, falling back to minio "
                        "(may have compatibility issues)"
                    )
                    self._backend = MinioS3Backend(self._config)
            else:
                # AWS/腾讯云/MinIO/自定义兼容存储统一走 MinIO 客户端（标准 S3 协议实现）。
                self._backend = MinioS3Backend(self._config)

        return self._backend

    async def upload_file(
        self,
        file: BinaryReadFile,
        folder: str,
        filename: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
        *,
        skip_size_limit: bool = False,
    ) -> UploadResult:
        """Upload a file to storage with retry on transient failures."""
        # Check file size via current position
        # 通过 seek 到文件末尾再计算与起始位置的差值来获取文件大小，避免依赖调用方额外传入 size 参数；
        # 检查完毕后立即把指针复原到起始位置，保证后续真正上传时能从正确位置开始读取。
        start_pos = await run_blocking_io(file.tell)
        if not skip_size_limit:
            await run_blocking_io(file.seek, 0, 2)
            file_size = await run_blocking_io(file.tell) - start_pos
            await run_blocking_io(file.seek, start_pos)
            if file_size > self._config.internal_max_upload_size:
                max_mb = self._config.internal_max_upload_size / (1024 * 1024)
                raise ValueError(
                    f"File size ({file_size / (1024 * 1024):.1f}MB) exceeds "
                    f"internal upload limit ({max_mb:.0f}MB)"
                )

        # 生成带时间戳 + 随机后缀的唯一存储路径，既能按时间排序浏览，又能避免同名文件互相覆盖。
        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
        safe_filename = self._sanitize_filename(filename)
        unique_suffix = uuid.uuid4().hex[:8]
        key = f"{folder}/{timestamp}_{unique_suffix}_{safe_filename}"

        backend = self._get_backend()

        async def _upload_attempt() -> UploadResult:
            # 每次重试前都要把文件指针重新定位到起始位置，否则重试时会从上次读到的位置继续读，导致数据缺失。
            await run_blocking_io(file.seek, start_pos)
            return await backend.upload(file, key, content_type, metadata)

        return await self._retry_async(
            _upload_attempt,
            label=f"file://{key}",
        )

    async def upload_bytes(
        self,
        data: bytes,
        folder: str,
        filename: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
        *,
        skip_size_limit: bool = False,
    ) -> UploadResult:
        """Upload bytes to storage with retry on transient failures."""
        if not skip_size_limit and len(data) > self._config.internal_max_upload_size:
            max_mb = self._config.internal_max_upload_size / (1024 * 1024)
            raise ValueError(
                f"Data size ({len(data) / (1024 * 1024):.1f}MB) exceeds "
                f"internal upload limit ({max_mb:.0f}MB)"
            )

        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
        safe_filename = self._sanitize_filename(filename)
        unique_suffix = uuid.uuid4().hex[:8]
        key = f"{folder}/{timestamp}_{unique_suffix}_{safe_filename}"

        # 统一委托给 upload_stream_to_key：把 bytes 包装成内存流对象，复用同一套上传 + 重试 + 签名逻辑，
        # skip_size_limit=True 是因为上面已经检查过一次，避免重复校验。
        return await self.upload_stream_to_key(
            io.BytesIO(data),
            key,
            content_type,
            metadata,
            skip_size_limit=True,
        )

    async def upload_to_key(
        self,
        data: bytes,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
        *,
        skip_size_limit: bool = False,
    ) -> UploadResult:
        """Upload bytes to a specific key (caller controls the full key)."""
        # 与 upload_bytes 的区别：这里 key 完全由调用方指定（不做时间戳/随机后缀拼接），
        # 适用于需要精确控制存储路径的场景（如按固定规则覆盖写入）。
        if not skip_size_limit and len(data) > self._config.internal_max_upload_size:
            max_mb = self._config.internal_max_upload_size / (1024 * 1024)
            raise ValueError(
                f"Data size ({len(data) / (1024 * 1024):.1f}MB) exceeds "
                f"internal upload limit ({max_mb:.0f}MB)"
            )

        return await self.upload_stream_to_key(
            io.BytesIO(data),
            key,
            content_type,
            metadata,
            skip_size_limit=True,
        )

    async def upload_stream_to_key(
        self,
        file: BinaryReadFile,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
        *,
        skip_size_limit: bool = False,
    ) -> UploadResult:
        """Upload a file-like object to a specific key without materializing it as bytes."""
        # 本方法是各种 upload_* 便捷方法的最终落地实现：直接操作文件流而不强制转成 bytes，
        # 对大文件更省内存。
        start_pos = await run_blocking_io(file.tell)
        if not skip_size_limit:
            await run_blocking_io(file.seek, 0, 2)
            file_size = await run_blocking_io(file.tell) - start_pos
            await run_blocking_io(file.seek, start_pos)
            if file_size > self._config.internal_max_upload_size:
                max_mb = self._config.internal_max_upload_size / (1024 * 1024)
                raise ValueError(
                    f"File size ({file_size / (1024 * 1024):.1f}MB) exceeds "
                    f"internal upload limit ({max_mb:.0f}MB)"
                )

        backend = self._get_backend()

        async def _upload_attempt() -> UploadResult:
            await run_blocking_io(file.seek, start_pos)
            return await backend.upload(file, key, content_type, metadata)

        result = await self._retry_async(
            _upload_attempt,
            label=f"stream://{key}",
        )

        # 私有 bucket 场景下后端返回的 URL 可能不带签名参数，这里统一补签一个预签名 URL，
        # 且预签名有效期不超过 7 天（多数云厂商预签名 URL 的硬性上限），取配置值与该上限的较小者。
        if not self._config.public_bucket and "?" not in result.url:
            max_expires = 7 * 24 * 3600
            expires = min(self._config.presigned_url_expires, max_expires)
            result.url = await self._retry_async(
                lambda: backend.get_presigned_url(key, expires),
                label=f"presign://{key}",
            )

        return result

    async def upload_avatar(self, user_id: str, data: bytes, filename: str) -> UploadResult:
        """Upload user avatar"""
        return await self.upload_bytes(
            data=data,
            folder=f"avatars/{user_id}",
            filename=filename,
            content_type=self._get_image_content_type(filename),
        )

    async def delete_user_files(self, user_id: str) -> int:
        """Delete all files for a user. Returns number of files deleted."""
        deleted_count = 0
        backend = self._get_backend()

        # 优先尝试走底层客户端的批量删除接口（若后端暴露了 _client），比逐个调用 delete_file 更高效；
        # 分页处理：list_files 每次只取一批，删完再查下一批，直到某个前缀下没有剩余对象。
        if hasattr(backend, "_client"):
            try:
                client = backend._client
                bucket = self._config.bucket_name

                for prefix in (f"avatars/{user_id}", user_id):
                    while True:
                        objects = await self.list_files(prefix)
                        if not objects:
                            break

                        def _remove_objects():
                            for key in objects:
                                client.remove_object(bucket_name=bucket, object_name=key)

                        await run_blocking_io(_remove_objects)
                        deleted_count += len(objects)

                return deleted_count
            except Exception as e:
                logger.warning(f"Batch delete failed, falling back to individual deletes: {e}")

        # Fallback: individual deletes
        # 批量删除不可用或失败时，退化为逐个删除；额外加入"本轮未删除任何对象则停止"的保护，
        # 避免因某些对象反复删除失败而导致死循环。
        for prefix in (f"avatars/{user_id}", user_id):
            while True:
                objects = await self.list_files(prefix)
                if not objects:
                    break
                deleted_this_batch = 0
                for key in objects:
                    if await self.delete_file(key):
                        deleted_count += 1
                        deleted_this_batch += 1
                if deleted_this_batch == 0:
                    logger.warning(
                        "No progress deleting files for user=%s prefix=%s; stopping cleanup",
                        user_id,
                        prefix,
                    )
                    break

        return deleted_count

    async def delete_file(self, key: str) -> bool:
        """Delete a file"""
        return await self._get_backend().delete(key)

    async def download_file(self, key: str) -> bytes:
        """Download a file and return its content as bytes"""
        # 下载前先探测大小并校验上限，避免一次性把超大对象整体读入内存导致 OOM；
        # 真正需要处理大文件时应使用 download_stream / download_to_file 等流式接口。
        backend = self._get_backend()
        file_size = await backend.get_size(key)
        if file_size > self._config.internal_max_upload_size:
            max_mb = self._config.internal_max_upload_size / (1024 * 1024)
            raise ValueError(
                f"File size ({file_size / (1024 * 1024):.1f}MB) exceeds "
                f"internal download limit ({max_mb:.0f}MB)"
            )
        return await backend.download(key)

    async def download_to_file(
        self,
        key: str,
        file: BinaryWriteFile,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> int:
        """Download a file into a file-like sink without materializing bytes."""
        return await self._get_backend().download_to_file(key, file, chunk_size=chunk_size)

    async def download_stream(self, key: str, chunk_size: int = 1024 * 1024):
        """Stream a file without materializing the full object in memory."""
        async for chunk in self._get_backend().download_stream(key, chunk_size=chunk_size):
            yield chunk

    async def file_exists(self, key: str) -> bool:
        """Check if a file exists"""
        return await self._get_backend().exists(key)

    def get_file_path(self, key: str):
        """Get local filesystem path for a key (local backend only)."""
        # 仅本地存储后端才有真实文件系统路径的概念；其他后端（对象存储）没有本地路径,调用会直接报错。
        backend = self._get_backend()
        if not isinstance(backend, LocalStorageBackend):
            raise RuntimeError("get_file_path is only available for local storage")
        return backend._get_file_path(key)

    async def get_file_url(self, key: str) -> str:
        """Get public URL for a file"""
        return await self._get_backend().get_url(key)

    async def get_presigned_url(self, key: str, expires: int = 3600) -> str:
        """Get presigned URL for a file (for private buckets)"""
        return await self._get_backend().get_presigned_url(key, expires)

    async def list_files(self, folder: str) -> list[str]:
        """List files in a folder"""
        return await self._get_backend().list_objects(prefix=folder)

    async def close(self) -> None:
        """Close the storage service"""
        if self._backend:
            await self._backend.close()
            self._backend = None

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for safe storage"""
        # 把除字母数字、下划线、短横线、点以外的字符全部替换为下划线，避免路径注入/特殊字符问题；
        # 过长文件名做截断，同时尽量保留扩展名，避免影响内容类型识别。
        safe = re.sub(r"[^\w\-_\.]", "_", filename)
        if len(safe) > 200:
            name, ext = safe.rsplit(".", 1) if "." in safe else (safe, "")
            safe = name[: 200 - len(ext) - 1] + "." + ext if ext else name[:200]
        return safe

    def _get_image_content_type(self, filename: str) -> str:
        """Get content type for image files"""
        ext = filename.lower().split(".")[-1] if "." in filename else ""
        content_types = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
            "svg": "image/svg+xml",
            "bmp": "image/bmp",
            "ico": "image/x-icon",
        }
        return content_types.get(ext, "application/octet-stream")

    def validate_file(
        self,
        filename: str,
        file_size: int,
        allowed_extensions: Optional[list[str]] = None,
    ) -> tuple[bool, str]:
        """Validate file before upload. Returns (is_valid, error_message)."""
        # 面向"用户可见"的业务校验（对外文件大小限制、扩展名白名单），
        # 与前面 internal_max_upload_size 这种"内部硬性上限"是两层不同的校验目的。
        if file_size > self._config.max_file_size:
            max_mb = self._config.max_file_size / (1024 * 1024)
            return False, f"File size exceeds maximum of {max_mb:.1f}MB"

        ext = filename.lower().split(".")[-1] if "." in filename else ""
        extensions = allowed_extensions or self._config.allowed_extensions
        if ext not in extensions:
            return False, f"File type '.{ext}' is not allowed"

        return True, ""


# Global storage service instance
_storage_service: Optional[S3StorageService] = None


def get_storage_service() -> S3StorageService:
    """Get the global storage service instance"""
    global _storage_service
    if _storage_service is None:
        _storage_service = S3StorageService.get_instance()
    return _storage_service


async def init_storage(config: S3Config) -> None:
    """Initialize storage service with configuration"""
    global _storage_service
    _storage_service = S3StorageService(config)


async def close_storage() -> None:
    """Close storage service"""
    global _storage_service
    if _storage_service:
        await _storage_service.close()
    _storage_service = None
    S3StorageService._instance = None


def _parse_bool(value: object) -> bool:
    """Parse boolean value from various types."""
    # 兼容环境变量常见的多种"真值"写法（字符串 "true"/"1"/"yes"/"on" 等），统一转换为布尔值。
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


def get_s3_enabled() -> bool:
    """Get S3 enabled status from settings"""
    from src.kernel.config import settings

    return _parse_bool(settings.S3_ENABLED)


async def get_s3_config_from_settings() -> S3Config:
    """Build S3Config from cached application settings."""
    from src.kernel.config import settings

    # 未启用 S3 时直接使用应用配置里预置的（通常是本地存储）S3Config，不再走下面的厂商映射逻辑。
    if not get_s3_enabled():
        return settings.get_s3_config()

    provider_map = {
        "aws": S3Provider.AWS,
        "aliyun": S3Provider.ALIYUN,
        "tencent": S3Provider.TENCENT,
        "minio": S3Provider.MINIO,
        "custom": S3Provider.CUSTOM,
        "local": S3Provider.LOCAL,
    }

    storage_path = getattr(settings, "LOCAL_STORAGE_PATH", "./uploads") or "./uploads"

    return S3Config(
        provider=provider_map.get(str(settings.S3_PROVIDER).lower(), S3Provider.AWS),
        endpoint_url=settings.S3_ENDPOINT_URL if settings.S3_ENDPOINT_URL else None,
        access_key=str(settings.S3_ACCESS_KEY) if settings.S3_ACCESS_KEY else "",
        secret_key=str(settings.S3_SECRET_KEY) if settings.S3_SECRET_KEY else "",
        region=str(settings.S3_REGION) if settings.S3_REGION else "us-east-1",
        bucket_name=str(settings.S3_BUCKET_NAME) if settings.S3_BUCKET_NAME else "",
        custom_domain=settings.S3_CUSTOM_DOMAIN if settings.S3_CUSTOM_DOMAIN else None,
        path_style=_parse_bool(settings.S3_PATH_STYLE),
        public_bucket=_parse_bool(settings.S3_PUBLIC_BUCKET),
        max_file_size=(int(settings.S3_MAX_FILE_SIZE) if settings.S3_MAX_FILE_SIZE else 10485760),
        internal_max_upload_size=(
            int(settings.S3_INTERNAL_UPLOAD_MAX_SIZE)
            if settings.S3_INTERNAL_UPLOAD_MAX_SIZE
            else 50 * 1024 * 1024
        ),
        presigned_url_expires=(
            int(settings.S3_PRESIGNED_URL_EXPIRES)
            if settings.S3_PRESIGNED_URL_EXPIRES
            else 7 * 24 * 3600
        ),
        storage_path=storage_path,
    )


async def get_or_init_storage() -> S3StorageService:
    """Initialize (if needed) and return the global storage service.

    This is the single entry-point that infra-layer code should use
    instead of importing from API routes.
    """
    # 根据当前配置动态决定使用对象存储还是本地存储；每次调用都会重新计算期望配置，
    # 只有当期望配置与当前生效配置不一致时才重新 configure（并关闭旧后端），避免重复创建连接。
    if get_s3_enabled():
        config = await get_s3_config_from_settings()
    else:
        from src.kernel.config import settings

        storage_path = getattr(settings, "LOCAL_STORAGE_PATH", "./uploads") or "./uploads"
        config = S3Config(provider=S3Provider.LOCAL, storage_path=storage_path)

    svc = get_storage_service()
    if svc._config != config:
        await svc.close()
        svc.configure(config)
    return get_storage_service()

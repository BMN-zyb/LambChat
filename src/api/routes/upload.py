"""
File upload API routes

Provides endpoints for file uploads to S3-compatible storage.
"""

# 文件上传路由模块（挂载于 /api/upload）：负责 multipart 文件上传、头像上传/删除、
# 文件删除、存储配置查询，以及通过服务端动态代理访问已上传文件。
# 关键能力：
#   - 分类与权限：按扩展名/MIME 判定文件类别（见 file_type），并按类别校验上传权限；
#   - 大小限制：按用户角色解析各类别大小上限，边读边计数，超限立即中断；
#   - 内容去重：对文件内容做 SHA-256，命中已有记录则复用，避免重复存储（秒传）；
#   - 存储后端：本地磁盘或 S3 兼容对象存储（OSS），两者路径分别处理；
#   - 预签名直传相关路由由 upload_signed_urls 提供，并在文件末尾挂载进来。
import hashlib
import uuid
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from src.api.deps import get_current_user_required, require_permissions
from src.api.routes.file_type import (
    FILE_EXTENSIONS,
    FileCategory,
    get_file_category,
    get_permission_for_category,
)
from src.api.routes.upload_signed_urls import (
    SignedUrlItem,
    SignedUrlRequest,
    SignedUrlResponse,
    get_signed_urls,
    get_single_signed_url,
)
from src.api.routes.upload_signed_urls import (
    router as signed_url_router,
)
from src.infra.async_utils import run_blocking_io
from src.infra.async_utils.background_tasks import BestEffortTaskLimiter
from src.infra.auth.rbac import check_permission
from src.infra.logging import get_logger
from src.infra.storage.s3 import (
    S3Config,
    S3Provider,
)
from src.infra.storage.s3.base import BinaryReadFile
from src.infra.upload.file_record import FileRecordStorage
from src.kernel.config import settings
from src.kernel.schemas.user import TokenPayload

logger = get_logger(__name__)

__all__ = [
    "SignedUrlItem",
    "SignedUrlRequest",
    "SignedUrlResponse",
    "get_signed_urls",
    "get_single_signed_url",
]

# 文件记录存储：保存“内容哈希 → 存储 key/文件名/大小/类别/引用计数”的元数据，
# 用于秒传去重与删除时的引用计数保护
_file_record_storage = FileRecordStorage()
# 后台删除任务限流器：删除操作放到后台尽力执行，最多并发 8 个
_upload_delete_tasks = BestEffortTaskLimiter("upload delete", max_tasks=8)

# 读取上传文件时每次读取的块大小（1MB）
UPLOAD_READ_CHUNK_SIZE = 1024 * 1024
# SpooledTemporaryFile 的内存缓冲上限（2MB）：超过则自动落盘为临时文件，控制内存占用
UPLOAD_SPOOL_MEMORY_LIMIT = 2 * 1024 * 1024


# 等待所有后台删除任务执行完成（应用关闭时调用，避免任务被中断而丢失删除）
async def drain_upload_delete_tasks() -> None:
    await _upload_delete_tasks.drain()


# 释放本路由持有的资源：先排空后台删除任务，再关闭文件记录存储连接
async def close_upload_route_dependencies() -> None:
    await drain_upload_delete_tasks()
    await _file_record_storage.close()


def _parse_bool(value: Any) -> bool:
    """Parse boolean value from various types."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    # 字符串按常见“真值”词判定（true/1/yes/on 视为 True）
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


# 本模块路由统一挂载于 /api/upload 前缀下
router = APIRouter()


async def _get_live_record_by_hash(file_hash: str, storage=None) -> dict | None:
    """Return a dedupe record only if both metadata and the backing file still exist."""
    # 先查元数据记录；没有记录说明此前从未上传过该内容
    record = await _file_record_storage.find_by_hash(file_hash)
    if record is None:
        return None

    # 记录存在时还需确认底层文件仍在，避免返回指向已被删除对象的“僵尸”记录
    storage = storage or await get_or_init_storage()
    if await storage.file_exists(record["key"]):
        return record

    # 文件已丢失但记录残留：清理该陈旧记录并视为“不存在”，让调用方走正常上传
    logger.warning(
        "Found stale file record for hash %s pointing to missing key %s",
        file_hash,
        record["key"],
    )
    await _file_record_storage.delete_by_hash(file_hash)
    return None


def _get_base_url(request: Request) -> str:
    """获取 base_url，优先 APP_BASE_URL 环境变量，fallback 到 request.base_url"""
    app_base_url = getattr(settings, "APP_BASE_URL", "").rstrip("/")
    if app_base_url:
        return app_base_url
    base_url = str(request.base_url).rstrip("/")
    if base_url == "http://None":
        return ""
    return base_url


def _build_upload_response(
    request: Request,
    *,
    key: str,
    name: str,
    file_type: str,
    mime_type: str,
    size: int,
    exists: bool = False,
) -> dict:
    """Build a normalized upload response payload."""
    # 返回的 url 统一指向服务端代理接口 /api/upload/file/{key}，
    # 由该接口在访问时再决定是重定向到预签名 URL 还是直接读本地文件
    base_url = _get_base_url(request)
    proxy_url = f"{base_url}/api/upload/file/{key}"
    payload = {
        "key": key,
        "url": proxy_url,
        "name": name,
        "type": file_type,
        "mime_type": mime_type,
        "size": size,
    }
    # exists=True 表示命中去重（复用已有文件，本次并未真正上传新内容）
    if exists:
        payload["exists"] = True
    return payload


# 从头像 URL 反解出对象存储 key，且仅当该 key 属于当前用户（前缀 avatars/{user_id}/）
# 时才返回，否则返回 None。用于删除旧头像时的归属校验，防止误删他人对象。
def _avatar_object_key_from_url(avatar_url: str | None, user_id: str) -> str | None:
    if not avatar_url:
        return None

    # 解析 URL 并做百分号解码，兼容“代理 URL”和“裸 key”两种形式
    parsed = urlsplit(avatar_url)
    path = unquote(parsed.path or avatar_url)
    proxy_prefix = "/api/upload/file/"
    if proxy_prefix in path:
        # 形如 .../api/upload/file/<key>，截取代理前缀之后的部分作为 key
        key = path.split(proxy_prefix, 1)[1]
    else:
        # 否则将路径去掉开头斜杠直接当作 key
        key = path.lstrip("/")

    # 归属校验：只有前缀为本用户头像目录的 key 才允许被后续删除
    owned_prefix = f"avatars/{user_id}/"
    if key.startswith(owned_prefix):
        return key
    return None


# 删除用户旧头像对象，但仅在该对象确属此用户时执行；keep_key 用于跳过刚上传的新头像。
# 删除失败只记录告警、不抛出（旧头像残留不影响主流程）。
async def _delete_avatar_object_if_owned(
    storage: Any,
    user_id: str,
    avatar_url: str | None,
    *,
    keep_key: str | None = None,
) -> None:
    # 反解并校验归属；非本人对象或正是要保留的新头像则直接跳过
    key = _avatar_object_key_from_url(avatar_url, user_id)
    if key is None or key == keep_key:
        return

    try:
        await storage.delete_file(key)
    except Exception as e:
        logger.warning("Failed to delete previous avatar object %s: %s", key, e, exc_info=True)


# 判断本地文件路径是否存在（抽成函数便于用 run_blocking_io 放到线程池执行此阻塞 IO）
def _path_exists(file_path) -> bool:
    return file_path.exists()


# 为文件下载/代理响应准备元数据：返回 (用于 Content-Disposition 的原始文件名, MIME 类型)。
# 优先取文件记录中的名称与 mime_type，缺失时用 mimetypes 猜测，最后兜底为二进制流。
async def _get_file_response_metadata(key: str) -> tuple[str | None, str]:
    record = await _file_record_storage.find_by_key(key)
    filename_for_disposition = record["name"] if record else None
    content_type = record["mime_type"] if record and record.get("mime_type") else None

    # 记录中没有 MIME 时，按文件名/key 的扩展名猜测
    if not content_type:
        import mimetypes

        content_type, _ = mimetypes.guess_type(key)
        # 仍猜不出则使用通用二进制类型
        if not content_type:
            content_type = "application/octet-stream"

    return filename_for_disposition, content_type


async def _read_upload_file_limited(
    file: Any,
    *,
    max_size_bytes: int,
    max_size_mb: int,
    purpose: str = "File",
    chunk_size: int = UPLOAD_READ_CHUNK_SIZE,
) -> bytes:
    """Read an UploadFile in chunks and stop as soon as the configured limit is exceeded."""
    data = bytearray()
    total_size = 0

    # 分块读取并累计大小，一旦超过上限立即抛 400，避免把超大文件整体读进内存
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break

        total_size += len(chunk)
        if total_size > max_size_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"{purpose} size exceeds maximum of {max_size_mb}MB",
            )
        data.extend(chunk)

    return bytes(data)


# 上传临时文件的结构化协议：既可二进制读取（BinaryReadFile），又可关闭
class UploadSpool(BinaryReadFile, Protocol):
    def close(self) -> None: ...


# 已缓冲的上传文件：封装临时文件对象、内容 SHA-256 十六进制串与总字节数
@dataclass
class SpooledUpload:
    file: UploadSpool
    sha256_hex: str
    size: int

    def close(self) -> None:
        self.file.close()


async def _spool_upload_file_limited(
    file: Any,
    *,
    max_size_bytes: int,
    max_size_mb: int,
    purpose: str = "File",
    chunk_size: int = UPLOAD_READ_CHUNK_SIZE,
) -> SpooledUpload:
    """Stream an UploadFile into a bounded spool while hashing and enforcing size limits."""
    # 一边流式读取，一边增量计算 SHA-256，一边写入内存/磁盘混合的临时文件
    digest = hashlib.sha256()
    total_size = 0
    spooled = SpooledTemporaryFile(max_size=UPLOAD_SPOOL_MEMORY_LIMIT, mode="w+b")

    try:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break

            total_size += len(chunk)
            # 超过大小上限立即中断（此时临时文件会在下方 except 中被清理）
            if total_size > max_size_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"{purpose} size exceeds maximum of {max_size_mb}MB",
                )
            digest.update(chunk)
            # 写盘属阻塞 IO，交给线程池执行，避免阻塞事件循环
            await run_blocking_io(spooled.write, chunk)

        # 读取完成后把游标移回开头，供后续上传/读取复用
        await run_blocking_io(spooled.seek, 0)
        return SpooledUpload(file=spooled, sha256_hex=digest.hexdigest(), size=total_size)
    except Exception:
        # 任何异常都要关闭并释放临时文件，防止句柄/磁盘泄漏
        spooled.close()
        raise


def get_s3_enabled() -> bool:
    """Get S3 enabled status from cached settings"""
    return _parse_bool(settings.S3_ENABLED)


async def get_s3_config_from_settings() -> S3Config:
    """Get S3 configuration from cached settings"""
    # 未启用 S3 时，直接返回配置对象里默认的（本地）存储配置
    if not get_s3_enabled():
        return settings.get_s3_config()

    # 存储服务商标识 → 内部枚举的映射（支持 AWS/阿里云/腾讯云/MinIO/自定义/本地）
    provider_map = {
        "aws": S3Provider.AWS,
        "aliyun": S3Provider.ALIYUN,
        "tencent": S3Provider.TENCENT,
        "minio": S3Provider.MINIO,
        "custom": S3Provider.CUSTOM,
        "local": S3Provider.LOCAL,
    }

    # 本地存储根目录（provider=local 或作为回退时使用）
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
        # 对外允许的单文件最大大小（默认 10MB）
        max_file_size=(int(settings.S3_MAX_FILE_SIZE) if settings.S3_MAX_FILE_SIZE else 10485760),
        # 服务端内部上传（如流式转存）允许的最大大小（默认 50MB）
        internal_max_upload_size=(
            int(settings.S3_INTERNAL_UPLOAD_MAX_SIZE)
            if settings.S3_INTERNAL_UPLOAD_MAX_SIZE
            else 50 * 1024 * 1024
        ),
        # 预签名 URL 的默认过期时间（默认 7 天）
        presigned_url_expires=(
            int(settings.S3_PRESIGNED_URL_EXPIRES)
            if settings.S3_PRESIGNED_URL_EXPIRES
            else 7 * 24 * 3600
        ),
        storage_path=storage_path,
    )


async def get_or_init_storage():
    """Initialize and get storage service (re-exported from infra layer)"""
    from src.infra.storage.s3.service import get_or_init_storage as _get_or_init

    return await _get_or_init()


async def resolve_upload_limits(user_roles: list[str]) -> dict:
    """Resolve effective upload limits for a user based on their roles.

    Most permissive value across roles wins. Falls back to global settings.
    """
    from src.infra.role.storage import RoleStorage

    # 全局默认上限（各类别大小 MB 与最大文件数），作为无角色覆盖时的兜底
    defaults = {
        "image": settings.FILE_UPLOAD_MAX_SIZE_IMAGE,
        "video": settings.FILE_UPLOAD_MAX_SIZE_VIDEO,
        "audio": settings.FILE_UPLOAD_MAX_SIZE_AUDIO,
        "document": settings.FILE_UPLOAD_MAX_SIZE_DOCUMENT,
        "maxFiles": settings.FILE_UPLOAD_MAX_FILES,
    }

    # 返回字段名 → 角色 limits 对象上对应属性名的映射
    field_map = {
        "image": "max_file_size_image",
        "video": "max_file_size_video",
        "audio": "max_file_size_audio",
        "document": "max_file_size_document",
        "maxFiles": "max_files",
    }

    resolved = dict(defaults)
    role_overrides: dict[str, int] = {}

    try:
        role_storage = RoleStorage()
        # 遍历用户所有角色，对每个字段取“最宽松（最大）”的值作为该用户的上限
        for role_name in user_roles:
            role = await role_storage.get_by_name(role_name)
            if role and role.limits:
                for key, field_name in field_map.items():
                    value = getattr(role.limits, field_name, None)
                    if value is not None:
                        role_overrides[key] = max(role_overrides.get(key, value), value)

        # Only apply role overrides for fields where at least one role set a value
        # 仅覆盖那些至少有一个角色显式设置了值的字段，其余保持全局默认
        resolved.update(role_overrides)
    except Exception as e:
        # 角色上限解析失败不应阻断上传，降级为全局默认值
        logger.warning(f"Failed to resolve role upload limits, using defaults: {e}")

    return resolved


# /check 秒传探测的请求体：前端先在本地算出文件的 SHA-256，再来询问服务端是否已存在
class FileCheckRequest(BaseModel):
    # 文件内容的 SHA-256 十六进制摘要（固定 64 个字符）
    hash: str = Field(..., min_length=64, max_length=64, description="SHA-256 hex digest")
    # 文件大小（字节，必须大于 0）
    size: int = Field(..., gt=0, description="File size in bytes")
    # 原始文件名
    name: str = Field(..., description="Original filename")
    # MIME 类型
    mime_type: str = Field(..., description="MIME type")


# POST /check：按内容哈希探测文件是否已存在（“秒传”）。需要登录。
# 命中则返回已有文件的 key/url/元数据，前端可跳过实际上传直接复用。
@router.post("/check")
async def check_file_exists(
    request: Request,
    body: FileCheckRequest,
    current_user: TokenPayload = Depends(get_current_user_required),
) -> dict:
    storage = await get_or_init_storage()
    # 校验哈希对应的记录且底层文件确实存在，否则视为未命中
    record = await _get_live_record_by_hash(body.hash, storage)
    if record is None:
        return {"exists": False}
    base_url = _get_base_url(request)
    return {
        "exists": True,
        "key": record["key"],
        "url": f"{base_url}/api/upload/file/{record['key']}",
        "name": record["name"],
        "type": record["category"],
        "mime_type": record["mime_type"],
        "size": record["size"],
    }


@router.post("/file")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    current_user: TokenPayload = Depends(get_current_user_required),
) -> dict:
    """
    Upload a file to S3

    Requires: file:upload:{type} permission based on file type
    Files are stored in folders organized by user_id.

    Args:
        request: FastAPI request object (for base_url)
        file: File to upload
        current_user: Current authenticated user

    Returns:
        Upload result with URL and metadata
    """
    storage = await get_or_init_storage()

    # Determine file category from filename and content_type (no need to read content)
    # 仅凭文件名与 content_type 判定类别，无需先把文件内容读进来
    category = get_file_category(file.filename or "", file.content_type)
    permission = get_permission_for_category(category)

    # Check permission
    # 权限校验：拥有该类别专属权限（如 file:upload:image）或通用 file:upload 之一即可
    has_specific = False
    has_general = False

    if permission:
        has_specific = check_permission(current_user.permissions, permission)
    has_general = check_permission(current_user.permissions, "file:upload")

    if not (has_specific or has_general):
        category_label = category.value if category != FileCategory.UNKNOWN else "未知"
        raise HTTPException(
            status_code=403,
            detail=f"No permission to upload {category_label} files",
        )

    # Resolve per-role upload limits
    # 按用户角色解析各类别大小上限，并组装成“类别 → 上限(MB)”的查表
    upload_limits = await resolve_upload_limits(current_user.roles)
    size_limits = {
        FileCategory.IMAGE: upload_limits["image"],
        FileCategory.VIDEO: upload_limits["video"],
        FileCategory.AUDIO: upload_limits["audio"],
        FileCategory.DOCUMENT: upload_limits["document"],
        FileCategory.UNKNOWN: 10,
    }
    max_size_mb = size_limits.get(category, 10)
    max_size_bytes = max_size_mb * 1024 * 1024

    # 先用 Content-Length 头做一次快速预检，尽早拒绝明显超限的请求
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_size_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"File size exceeds maximum of {max_size_mb}MB",
                )
        except ValueError:
            # Content-Length 非法（无法转 int）时忽略，交由后续逐块读取时严格校验
            pass

    # Validate file extension
    # 扩展名白名单校验：已识别类别时，扩展名必须在该类别允许集合内
    ext = (file.filename or "").lower().split(".")[-1]
    allowed_exts = FILE_EXTENSIONS.get(category, set())
    if category != FileCategory.UNKNOWN and ext not in allowed_exts:
        raise HTTPException(
            status_code=400,
            detail=f"File extension '.{ext}' is not allowed for {category.value} files",
        )

    spooled_upload: SpooledUpload | None = None
    storage_key = ""
    file_hash = ""
    try:
        # 流式缓冲到临时文件，同时算出内容哈希并强制大小上限
        spooled_upload = await _spool_upload_file_limited(
            file,
            max_size_bytes=max_size_bytes,
            max_size_mb=max_size_mb,
        )
        file_hash = spooled_upload.sha256_hex

        # Check if hash already exists (race condition guard)
        # 内容去重：哈希已存在则直接复用已有文件，返回 exists=True，不重复存储
        existing = await _get_live_record_by_hash(file_hash, storage)
        if existing:
            return _build_upload_response(
                request,
                key=existing["key"],
                name=existing["name"],
                file_type=existing["category"],
                mime_type=existing["mime_type"],
                size=existing["size"],
                exists=True,
            )

        # Upload with short key organized by category and user
        # 生成存储 key：按“类别/用户ID/随机短ID.扩展名”组织，避免命名冲突并便于归属管理
        short_id = uuid.uuid4().hex[:16]
        ext = (file.filename or "").rsplit(".", 1)[-1] if "." in (file.filename or "") else ""
        storage_key = (
            f"{category.value}/{current_user.sub}/{short_id}.{ext}"
            if ext
            else f"{category.value}/{current_user.sub}/{short_id}"
        )
        # 以流式方式把临时文件写入目标 key；大小已在前面校验，故跳过存储层的大小限制
        upload_result = await storage.upload_stream_to_key(
            file=spooled_upload.file,
            key=storage_key,
            content_type=file.content_type,
            metadata={"uploaded_by": current_user.sub, "content_hash": file_hash},
            skip_size_limit=True,
        )
        storage_key = upload_result.key

        # Write file record
        # 写入文件元数据记录（含内容哈希），供后续秒传去重与引用计数使用
        await _file_record_storage.create(
            file_hash=file_hash,
            key=storage_key,
            name=file.filename or "unknown",
            mime_type=file.content_type or "application/octet-stream",
            size=spooled_upload.size,
            category=category.value,
            uploaded_by=current_user.sub,
        )

        return _build_upload_response(
            request,
            key=storage_key,
            name=file.filename or "unknown",
            file_type=category.value,
            mime_type=file.content_type or "application/octet-stream",
            size=spooled_upload.size,
        )
    except DuplicateKeyError:
        # 并发竞态：另一个请求已抢先写入同哈希记录（触发唯一索引冲突）
        logger.info("Duplicate upload detected for hash %s, reusing existing file", file_hash)

        existing = await _get_live_record_by_hash(file_hash, storage)
        if existing:
            # 本次刚上传的对象成了多余副本，尽力删除以免留下孤儿对象
            try:
                await storage.delete_file(storage_key)
            except Exception as cleanup_error:
                logger.warning(
                    "Failed to delete duplicate uploaded object %s after dedupe race: %s",
                    storage_key,
                    cleanup_error,
                )

            return _build_upload_response(
                request,
                key=existing["key"],
                name=existing["name"],
                file_type=existing["category"],
                mime_type=existing["mime_type"],
                size=existing["size"],
                exists=True,
            )

        raise HTTPException(status_code=500, detail="Upload failed: duplicate record conflict")
    except HTTPException:
        # 业务校验类异常（如超限/权限不足）原样抛出，保留其状态码
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")
    finally:
        # 无论成功失败都关闭临时文件，释放内存/磁盘
        if spooled_upload is not None:
            spooled_upload.close()


def _get_image_content_type(data: bytes) -> str:
    """Detect image content type from binary data using magic bytes"""
    # Check magic bytes to detect image type
    # Safety check: ensure data is long enough for magic byte detection
    # 通过文件头“魔术字节”识别真实图片类型，比信任客户端上报的 content_type 更安全
    if len(data) < 2:
        return "image/png"  # Default for empty/very small data

    # PNG 固定 8 字节文件头
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    # JPEG 以 FF D8 开头
    elif data[:2] == b"\xff\xd8":
        return "image/jpeg"
    # GIF：GIF87a / GIF89a
    elif len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    # WebP：RIFF....WEBP（第 0-3 字节为 RIFF，第 8-11 字节为 WEBP）
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # BMP：BM / BA
    elif data[:2] in (b"BM", b"BA"):
        return "image/bmp"
    else:
        return "image/png"  # Default to PNG


@router.post("/avatar", dependencies=[Depends(require_permissions("avatar:upload"))])
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: TokenPayload = Depends(get_current_user_required),
) -> dict:
    """
    Upload user avatar

    Avatar is stored in object storage and referenced by URL.

    Requires: file:upload permission

    Args:
        file: Avatar image file
        current_user: Current authenticated user

    Returns:
        Avatar data URI
    """
    # Validate file type
    # 头像仅允许常见图片扩展名
    allowed_image_extensions = ["jpg", "jpeg", "png", "gif", "webp"]
    ext = (
        (file.filename or "avatar.png").lower().split(".")[-1]
        if "." in (file.filename or "")
        else ""
    )
    if ext not in allowed_image_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"File type '.{ext}' is not allowed. Allowed types: {', '.join(allowed_image_extensions)}",
        )

    # Validate file size (max 2MB for avatar)
    # 头像大小上限固定 2MB（区别于普通上传的按角色上限）
    max_size = 2 * 1024 * 1024  # 2MB
    spooled_upload = await _spool_upload_file_limited(
        file,
        max_size_bytes=max_size,
        max_size_mb=2,
        purpose="Avatar file",
    )

    try:
        # 读取前 12 字节用魔术字节判定真实图片类型，再把游标重置回开头供上传
        header = await run_blocking_io(spooled_upload.file.read, 12)
        content_type = _get_image_content_type(header)
        await run_blocking_io(spooled_upload.file.seek, 0)
    except Exception:
        spooled_upload.close()
        raise

    try:
        from src.infra.user.storage import UserStorage
        from src.kernel.schemas.user import UserUpdate

        # 上传到 avatars/{用户ID} 目录；大小已在前面校验，跳过存储层的再次限制
        storage = await get_or_init_storage()
        upload_result = await storage.upload_file(
            file=spooled_upload.file,
            folder=f"avatars/{current_user.sub}",
            filename=file.filename or "avatar.png",
            content_type=content_type,
            skip_size_limit=True,
        )
        # 优先用存储返回的 URL，否则回退到服务端代理 URL
        avatar_url = upload_result.url or f"/api/upload/file/{upload_result.key}"

        logger.info(f"Uploading avatar for user: {current_user.sub}, filename: {file.filename}")
        # 先记下旧头像 URL，把用户资料更新为新头像后，再清理旧头像对象
        user_storage = UserStorage()
        previous_user = await user_storage.get_by_id(current_user.sub)
        previous_avatar_url = getattr(previous_user, "avatar_url", None)
        await user_storage.update(
            current_user.sub,
            UserUpdate(avatar_url=avatar_url),
        )
        # 删除旧头像对象（仅当其确属本人；keep_key 确保不会误删刚上传的新头像）
        await _delete_avatar_object_if_owned(
            storage,
            current_user.sub,
            previous_avatar_url,
            keep_key=upload_result.key,
        )
        logger.info(f"Avatar uploaded successfully for user: {current_user.sub}")

        return {
            "url": avatar_url,
            "size": spooled_upload.size,
            "content_type": content_type,
        }
    except Exception as e:
        logger.exception("Avatar upload failed")
        raise HTTPException(status_code=500, detail=f"Avatar upload failed: {str(e)}")
    finally:
        spooled_upload.close()


@router.delete("/avatar", dependencies=[Depends(require_permissions("avatar:upload"))])
async def delete_avatar(
    current_user: TokenPayload = Depends(get_current_user_required),
) -> dict:
    """
    Delete user avatar

    Removes the avatar_url from the user's profile.
    Requires: avatar:upload permission

    Args:
        current_user: Current authenticated user

    Returns:
        Deletion status
    """
    try:
        from src.infra.user.storage import UserStorage
        from src.kernel.schemas.user import UserUpdate

        # 先取旧头像 URL，将用户资料的 avatar_url 置空，再清理其对应的对象
        logger.info(f"Deleting avatar for user: {current_user.sub}")
        user_storage = UserStorage()
        previous_user = await user_storage.get_by_id(current_user.sub)
        previous_avatar_url = getattr(previous_user, "avatar_url", None)
        await user_storage.update(
            current_user.sub,
            UserUpdate(avatar_url=None),
        )
        # 删除对象存储中的旧头像（仅当其确属本人）
        object_storage = await get_or_init_storage()
        await _delete_avatar_object_if_owned(
            object_storage,
            current_user.sub,
            previous_avatar_url,
        )
        logger.info(f"Avatar deleted successfully for user: {current_user.sub}")

        return {"deleted": True}
    except Exception as e:
        logger.exception("Avatar deletion failed")
        raise HTTPException(status_code=500, detail=f"Avatar deletion failed: {str(e)}")


@router.delete("/{key:path}", dependencies=[Depends(require_permissions("file:upload"))])
async def delete_file(
    key: str,
    current_user: TokenPayload = Depends(get_current_user_required),
) -> dict:
    """
    Delete a file from S3

    Requires: file:upload permission

    Args:
        key: File key to delete
        current_user: Current authenticated user

    Returns:
        Deletion status
    """
    storage = await get_or_init_storage()

    # 有元数据记录时，按引用计数决定是否真的删除（去重可能导致多处引用同一文件）
    record = await _file_record_storage.find_by_key(key)
    if record is not None:
        # 引用计数 <= 0：无人引用，物理删除文件与记录
        if record.get("reference_count", 0) <= 0:
            await storage.delete_file(key)
            await _file_record_storage.delete_by_key(key)
            logger.info("Deleted unreferenced file %s", key)
            return {"deleted": True, "key": key, "status": "deleted"}

        # 仍被引用：保留文件以免破坏其他引用者，返回 preserved
        logger.info(
            "Preserving tracked file %s during delete request to avoid breaking deduplicated references",
            key,
        )
        return {"deleted": False, "key": key, "status": "preserved"}

    # Async delete - return immediately, delete in background
    # 无记录（未纳入去重管理）的文件：放到后台删除并立即返回 deleting
    async def background_delete():
        try:
            await storage.delete_file(key)
            await _file_record_storage.delete_by_key(key)
            logger.info(f"Background delete completed for key: {key}")
        except Exception as e:
            logger.error(f"Background delete failed for key {key}: {e}")

    _upload_delete_tasks.create_task(background_delete())
    return {"deleted": True, "key": key, "status": "deleting"}


@router.get("/config")
async def get_storage_config(
    current_user: TokenPayload = Depends(get_current_user_required),
) -> dict:
    """
    Get storage configuration status and file upload limits

    Returns effective upload limits for the current user based on their roles.
    Falls back to global settings if no role-specific limits are configured.

    Returns:
        Storage configuration and upload limits
    """
    s3_enabled = get_s3_enabled()

    # Resolve per-role upload limits for current user
    # 解析当前用户按角色生效的上传上限，返回给前端用于校验与展示
    upload_limits = await resolve_upload_limits(current_user.roles)

    # 未启用 S3 时 provider 返回 "local"；enabled 恒为 True（本地存储兜底）
    return {
        "enabled": True,  # Always enabled (local storage as fallback)
        "provider": settings.S3_PROVIDER if s3_enabled else "local",
        "uploadLimits": {
            "image": upload_limits["image"],
            "video": upload_limits["video"],
            "audio": upload_limits["audio"],
            "document": upload_limits["document"],
            "maxFiles": upload_limits["maxFiles"],
        },
    }


# 挂载预签名 URL 子路由（/signed-urls、/signed-url），前缀继承 /api/upload
router.include_router(signed_url_router)


@router.get("/file/{key:path}")
async def get_file_proxy(
    key: str,
    request: Request,
    direct: bool = False,
    proxy: bool = False,
) -> Response:
    """
    Dynamic proxy endpoint for file access

    For S3 storage: generates a short-lived presigned URL and redirects.
    For local storage: serves the file directly.
    No authentication required.

    Query params:
        direct: If true, return the URL as JSON instead of redirecting.
        proxy: If true, stream non-local storage through the app instead of redirecting.
    """
    from fastapi.responses import JSONResponse

    storage = await get_or_init_storage()

    base_url = _get_base_url(request)
    proxy_url = f"{base_url}/api/upload/file/{key}"

    # Local storage: serve file directly with FileResponse (native Range/sendfile support)
    # 本地存储：用 FileResponse 直接返回文件（原生支持 Range 断点续传/sendfile）
    if storage.is_local:
        # direct=true 时只返回该文件的 URL（JSON），不返回文件内容本身
        if direct:
            return JSONResponse({"url": proxy_url})
        try:
            file_path = storage.get_file_path(key)
            # 文件不存在返回 404（路径存在性检查为阻塞 IO，放线程池执行）
            if not await run_blocking_io(_path_exists, file_path):
                raise HTTPException(status_code=404, detail="File not found")

            filename_for_disposition, content_type = await _get_file_response_metadata(key)

            # inline 让浏览器尽量内联预览；本地文件缓存 1 天
            return FileResponse(
                path=str(file_path),
                media_type=content_type,
                filename=filename_for_disposition,
                content_disposition_type="inline",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to serve local file {key}: {e}")
            raise HTTPException(status_code=500, detail="Failed to read file")

    # S3 storage: redirect to presigned URL
    # 对象存储：默认重定向到预签名 URL，让客户端直连对象存储下载
    try:
        exists = await storage.file_exists(key)
        if not exists:
            raise HTTPException(status_code=404, detail="File not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to check file existence for {key}: {e}")

    # proxy=true：由服务端流式转发对象内容（适用于不希望暴露对象存储直链的场景）
    if proxy:
        filename_for_disposition, content_type = await _get_file_response_metadata(key)
        headers = {"Cache-Control": "public, max-age=300"}
        if filename_for_disposition:
            headers["Content-Disposition"] = f'inline; filename="{filename_for_disposition}"'

        return StreamingResponse(
            storage.download_stream(key),
            media_type=content_type,
            headers=headers,
        )

    try:
        # 公有桶直接取公开 URL；私有桶生成 300 秒有效的短时预签名 URL
        if storage._config.public_bucket:
            url = await storage.get_file_url(key)
        else:
            url = await storage.get_presigned_url(key, 300)
    except Exception as e:
        logger.error(f"Failed to generate presigned URL for {key}: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate file URL")

    # direct=true 只回传 URL（JSON）；否则用 302 重定向到该 URL
    if direct:
        return JSONResponse({"url": url})

    return Response(
        status_code=302,
        headers={
            "Location": url,
            "Cache-Control": "public, max-age=300",
        },
    )

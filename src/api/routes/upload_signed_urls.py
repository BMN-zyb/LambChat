"""
Signed URL API routes for upload-backed files.
"""

# 预签名 URL 路由模块：为对象存储中的私有文件批量/单个生成带有效期的访问 URL，
# 供前端直接读取文件，无需经服务端中转下载。
# 本 router 会被 upload.py 以 include_router 挂载到 /api/upload 前缀下。
# 三种存储形态区别对待：本地存储 → 返回服务端代理 URL；公有桶 → 返回公开 URL；
# 私有桶 → 生成带过期时间的预签名 URL。
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.deps import get_current_user_required, require_permissions
from src.infra.logging import get_logger
from src.kernel.schemas.user import TokenPayload

logger = get_logger(__name__)

# 单次批量请求允许的最大对象 key 数量（防止一次请求生成过多签名 URL）
SIGNED_URL_KEYS_MAX = 100

router = APIRouter()


class SignedUrlRequest(BaseModel):
    """Request model for getting signed URLs"""

    # 需要生成签名 URL 的对象 key 列表，长度限制 1..SIGNED_URL_KEYS_MAX
    keys: list[str] = Field(
        ...,
        min_length=1,
        max_length=SIGNED_URL_KEYS_MAX,
        description="List of S3 object keys to get signed URLs for",
    )
    # URL 过期时间（秒），默认 1 小时，取值范围 60 秒..24 小时
    expires: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="URL expiration time in seconds (default 1 hour, max 24 hours)",
    )


class SignedUrlItem(BaseModel):
    """Single signed URL result"""

    # 对象 key（原样回显，便于前端与请求项对应）
    key: str
    # 成功时的可访问 URL；失败时为 None
    url: str | None = None
    # 该 key 处理失败时的错误信息（如文件不存在）；成功时为 None
    error: str | None = None


class SignedUrlResponse(BaseModel):
    """Response model for signed URLs"""

    # 每个 key 对应的结果列表（逐项返回成功或失败，单个失败不导致整体报错）
    urls: list[SignedUrlItem]
    # 本批 URL 的有效期（秒）；本地存储与公有桶不适用，返回 0
    expires_in: int


# POST /signed-urls：批量为多个对象 key 生成访问/预签名 URL。
# 权限要求：file:upload。请求体 SignedUrlRequest，响应 SignedUrlResponse。
@router.post(
    "/signed-urls",
    response_model=SignedUrlResponse,
    dependencies=[Depends(require_permissions("file:upload"))],
)
async def get_signed_urls(
    body: SignedUrlRequest,
    req: Request,
    current_user: TokenPayload = Depends(get_current_user_required),
) -> SignedUrlResponse:
    """
    Get presigned URLs for private S3 objects.
    """
    # 权限已由依赖校验，此处不需要用户本体，显式丢弃以避免未使用告警
    del current_user
    # 延迟导入 upload 模块，复用其存储初始化与 base_url 计算，规避循环导入
    from src.api.routes import upload as upload_route

    storage = await upload_route.get_or_init_storage()
    base_url = upload_route._get_base_url(req)

    # 本地存储：无预签名概念，逐个校验文件是否存在并返回服务端代理 URL
    if storage.is_local:
        urls = []
        for key in body.keys:
            try:
                exists = await storage.file_exists(key)
                if exists:
                    urls.append(SignedUrlItem(key=key, url=f"{base_url}/api/upload/file/{key}"))
                else:
                    urls.append(SignedUrlItem(key=key, error="File not found"))
            except Exception as e:
                urls.append(SignedUrlItem(key=key, error=str(e)))
        return SignedUrlResponse(urls=urls, expires_in=0)

    # 公有桶：对象本身可公开访问，直接返回公开 URL，无需签名与过期时间
    if storage._config.public_bucket:
        urls = []
        for key in body.keys:
            try:
                url = await storage.get_file_url(key)
                urls.append(SignedUrlItem(key=key, url=url))
            except Exception as e:
                urls.append(SignedUrlItem(key=key, error=str(e)))
        return SignedUrlResponse(urls=urls, expires_in=0)

    # 私有桶：为每个 key 生成带过期时间的预签名 URL；单个失败不影响其余 key
    urls = []
    for key in body.keys:
        try:
            url = await storage.get_presigned_url(key, body.expires)
            urls.append(SignedUrlItem(key=key, url=url))
        except Exception as e:
            logger.warning("Failed to generate signed URL for %s: %s", key, e)
            urls.append(SignedUrlItem(key=key, error=str(e)))

    return SignedUrlResponse(urls=urls, expires_in=body.expires)


# GET /signed-url：为单个对象 key 生成访问/预签名 URL（query 参数 key、expires）。
# 权限要求：file:upload。响应 SignedUrlItem。
@router.get(
    "/signed-url",
    response_model=SignedUrlItem,
    dependencies=[Depends(require_permissions("file:upload"))],
)
async def get_single_signed_url(
    key: str,
    request: Request,
    expires: int = 3600,
    current_user: TokenPayload = Depends(get_current_user_required),
) -> SignedUrlItem:
    """
    Get a single presigned URL for a private S3 object.
    """
    # 权限已由依赖校验，丢弃未使用的 current_user
    del current_user
    # 手动校验过期时间范围（GET query 参数无法用 Field 约束）：60 秒..24 小时
    if expires < 60 or expires > 86400:
        raise HTTPException(
            status_code=400,
            detail="expires must be between 60 and 86400 seconds",
        )

    # 延迟导入 upload 模块，复用存储初始化与 base_url 计算
    from src.api.routes import upload as upload_route

    storage = await upload_route.get_or_init_storage()
    base_url = upload_route._get_base_url(request)

    try:
        # 本地存储：校验存在后返回代理 URL；公有桶返回公开 URL；私有桶返回预签名 URL
        if storage.is_local:
            exists = await storage.file_exists(key)
            if not exists:
                return SignedUrlItem(key=key, error="File not found")
            return SignedUrlItem(key=key, url=f"{base_url}/api/upload/file/{key}")
        if storage._config.public_bucket:
            url = await storage.get_file_url(key)
        else:
            url = await storage.get_presigned_url(key, expires)
        return SignedUrlItem(key=key, url=url)
    except Exception as e:
        logger.warning("Failed to generate signed URL for %s: %s", key, e)
        return SignedUrlItem(key=key, error=str(e))

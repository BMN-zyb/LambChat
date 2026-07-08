"""
Reveal File 工具

让 Agent 可以向用户展示/推荐文件，前端会自动展开文件树并可以点击查看内容。
文件会自动从 backend 下载并上传到 S3，返回 S3 URL。

统一通过 download_files 获取原始文件内容（沙箱/非沙箱均适用）。
非沙箱模式下，若 backend 下载失败，会回退到直接读取本地文件系统。

返回格式与前端 UploadResult 一致：
{
    "key": "...",
    "url": "...",
    "name": "...",
    "type": "image" | "video" | "audio" | "document",
    "mime_type": "...",
    "size": ...
}

分布式安全设计：
- 不依赖 ContextVar（无法跨进程/Worker 工作）
- 通过 ToolRuntime 注入 backend
- 使用 asyncio.Lock 防止并发初始化
"""

import inspect
import json
import mimetypes
import os
import re
from tempfile import SpooledTemporaryFile
from typing import Annotated, Any, Literal, Optional
from urllib.parse import unquote, urlparse

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.logging.context import TraceContext
from src.infra.revealed_file.storage import get_revealed_file_storage
from src.infra.tool.backend_utils import (
    get_backend_from_runtime,
    get_base_url_from_runtime,
    get_delivery_source_from_runtime,
    get_session_id_from_runtime,
    get_trace_id_from_runtime,
    get_user_id_from_runtime,
)
from src.kernel.config import settings

logger = get_logger(__name__)


async def _json_dumps_result(data: dict[str, Any]) -> str:
    # JSON 序列化放到线程池，避免阻塞事件循环
    return await run_blocking_io(json.dumps, data, ensure_ascii=False)


# 上传临时文件的内存阈值：小于则内存中处理，超过才落盘
_UPLOAD_SPOOL_MEMORY_LIMIT = 2 * 1024 * 1024
# 本地引用替换时，仅对不超过该大小的小文本文件做解析（避免读大文件）
_LOCAL_REF_RESOLUTION_MAX_BYTES = 2 * 1024 * 1024
# 单个文件内最多上传的本地资源引用数，防止一个文件引发过多上传
_LOCAL_REF_UPLOAD_LIMIT = 20
# reveal 文件上传的默认大小上限（可被 S3_INTERNAL_UPLOAD_MAX_SIZE 覆盖）
_DEFAULT_REVEAL_FILE_UPLOAD_MAX_BYTES = 50 * 1024 * 1024

# 文件类型分类
FileCategory = Literal["image", "video", "audio", "document"]

# MIME 类型到文件类别的映射
# 显式映射常见类型，未命中时再按前缀兜底（见 get_file_category）
MIME_TYPE_CATEGORIES: dict[str, FileCategory] = {
    # 图片
    "image/jpeg": "image",
    "image/png": "image",
    "image/gif": "image",
    "image/webp": "image",
    "image/svg+xml": "image",
    "image/bmp": "image",
    "image/x-icon": "image",
    # 视频
    "video/mp4": "video",
    "video/mpeg": "video",
    "video/webm": "video",
    "video/quicktime": "video",
    "video/x-msvideo": "video",
    "video/x-ms-wmv": "video",
    # 音频
    "audio/mpeg": "audio",
    "audio/wav": "audio",
    "audio/ogg": "audio",
    "audio/aac": "audio",
    "audio/flac": "audio",
    "audio/x-m4a": "audio",
}


def get_file_category(mime_type: str) -> FileCategory:
    """根据 MIME 类型获取文件类别"""
    # 先查精确映射表
    if mime_type in MIME_TYPE_CATEGORIES:
        return MIME_TYPE_CATEGORIES[mime_type]

    # 未命中则按 MIME 大类前缀归类
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"

    # 其余一律视为文档
    return "document"


def get_mime_type(filename: str) -> str:
    """根据文件名获取 MIME 类型"""
    # 猜不到类型时回退为通用二进制流
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"


def _is_sandbox_backend(backend: Any) -> bool:
    """判断 backend 是否为沙箱类型（支持 shell 命令执行）"""
    # 具备 execute/aexecute 能力的即视为沙箱后端；据此决定是否允许本地文件系统兜底
    return hasattr(backend, "execute") or hasattr(backend, "aexecute")


def _local_filesystem_fallback_enabled() -> bool:
    """Whether non-sandbox reveal flows may read from the process filesystem."""
    # 是否允许非沙箱模式下回退读取本机文件系统（受配置开关控制）
    return bool(getattr(settings, "ENABLE_LOCAL_FILESYSTEM_FALLBACK", True))


def _can_resolve_local_filesystem_refs(file_path: str) -> bool:
    """Only materialize small local text files for best-effort reference rewriting."""
    # 仅对小文件做本地引用解析；取大小失败（不存在等）视为不可解析
    try:
        return os.path.getsize(file_path) <= _LOCAL_REF_RESOLUTION_MAX_BYTES
    except OSError:
        return False


def _get_local_ref_upload_limit() -> int:
    # 本地引用上传上限，至少为 1
    return max(int(_LOCAL_REF_UPLOAD_LIMIT), 1)


def _get_reveal_file_upload_max_bytes() -> int:
    # 解析 reveal 上传大小上限：优先配置项，回退默认，至少 1
    configured = getattr(
        settings,
        "S3_INTERNAL_UPLOAD_MAX_SIZE",
        _DEFAULT_REVEAL_FILE_UPLOAD_MAX_BYTES,
    )
    return max(int(configured or _DEFAULT_REVEAL_FILE_UPLOAD_MAX_BYTES), 1)


def _coerce_file_size(value: Any) -> int | None:
    # 把后端返回的大小值安全转为非负 int；bool/None/非法值一律返回 None
    if isinstance(value, bool) or value is None:
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


async def _lookup_session_project_id(session_id: str | None) -> str | None:
    # 由 session_id 查出其所属 project_id（用于 revealed 文件索引归属）；失败静默返回 None
    if not session_id:
        return None
    try:
        from src.infra.storage.mongodb import get_mongo_client
        from src.kernel.config import settings

        mongo_client = get_mongo_client()
        db = mongo_client[settings.MONGODB_DB]
        # 只投影 metadata.project_id 字段，减少数据传输
        session_doc = await db[settings.MONGODB_SESSIONS_COLLECTION].find_one(
            {"session_id": session_id}, {"metadata.project_id": 1}
        )
        if session_doc:
            return (session_doc.get("metadata") or {}).get("project_id")
    except Exception:
        pass
    return None


async def _index_revealed_file(
    *,
    runtime: ToolRuntime | None,
    file_name: str,
    file_category: FileCategory,
    mime_type: str,
    file_size: int,
    url: str,
    file_key: str,
    description: str,
    original_path: str,
) -> None:
    # 把本次 reveal 的文件登记到"已展示文件"索引，供后续按用户/会话检索；失败不影响主流程
    try:
        # 优先用请求上下文中的用户/会话/trace 信息，缺失时回退到 runtime 注入值
        req_ctx = TraceContext.get_request_context()
        user_id = req_ctx.user_id or get_user_id_from_runtime(runtime)
        if not user_id:
            logger.info("[reveal_file] Skipping revealed file index: no user_id available")
            return

        session_id = req_ctx.session_id or get_session_id_from_runtime(runtime)
        trace_id = (
            req_ctx.trace_id
            or TraceContext.get().trace_id
            or get_trace_id_from_runtime(runtime)
            or ""
        )
        session_project_id = await _lookup_session_project_id(session_id)
        delivery_source = get_delivery_source_from_runtime(runtime)
        data: dict[str, Any] = {
            "file_type": file_category,
            "mime_type": mime_type,
            "file_size": file_size,
            "url": url,
            "session_id": session_id,
            "project_id": session_project_id,
            "description": description,
            "original_path": original_path,
        }
        if delivery_source:
            data["delivery_source"] = delivery_source

        # 以 (user_id, file_name) 维度 upsert，避免同名文件重复入库
        storage_index = get_revealed_file_storage()
        await storage_index.upsert_by_name(
            user_id=user_id,
            file_name=file_name,
            source="reveal_file",
            file_key=file_key,
            trace_id=trace_id,
            data=data,
        )
    except Exception as idx_err:
        logger.warning(f"[reveal_file] Failed to index revealed file: {idx_err}")


async def _get_backend_file_size(backend: Any, file_path: str) -> int | None:
    # 逐一尝试后端可能提供的取大小方法：异步 aget_file_size -> 同步 get_file_size -> 私有 _file_size
    # 用于在下载前就拦截超大文件
    async_method = getattr(backend, "aget_file_size", None)
    if callable(async_method):
        try:
            size = async_method(file_path)
            if inspect.isawaitable(size):
                size = await size
            return _coerce_file_size(size)
        except Exception as e:
            logger.debug(f"[reveal_file] aget_file_size failed for {file_path}: {e}")

    sync_method = getattr(backend, "get_file_size", None)
    if callable(sync_method):
        try:
            return _coerce_file_size(await run_blocking_io(sync_method, file_path))
        except Exception as e:
            logger.debug(f"[reveal_file] get_file_size failed for {file_path}: {e}")

    private_method = getattr(backend, "_file_size", None)
    if callable(private_method):
        try:
            return _coerce_file_size(await run_blocking_io(private_method, file_path))
        except Exception as e:
            logger.debug(f"[reveal_file] _file_size failed for {file_path}: {e}")

    return None


async def _get_storage():
    """获取已初始化的 storage 服务（复用 upload 模块的初始化逻辑）"""
    from src.infra.storage.s3.service import get_or_init_storage

    return await get_or_init_storage()


async def _download_file_from_backend(backend: Any, file_path: str) -> Optional[bytes]:
    """
    通过 download_files 从 backend 获取原始文件内容。

    沙箱（DaytonaBackend）和非沙箱（StateBackend/StoreBackend）均支持 download_files，
    返回原始字节，不包含行号等格式化内容。
    """
    logger.info(f"[reveal_file] Attempting to download: {file_path}")

    # 优先异步下载接口
    if hasattr(backend, "adownload_files"):
        try:
            responses = await backend.adownload_files([file_path])
            if responses:
                resp = responses[0]
                logger.info(
                    f"[reveal_file] adownload_files response: path={resp.path}, error={resp.error}, content_len={len(resp.content) if resp.content else 0}"
                )
                if resp.content:
                    return resp.content
                elif resp.error:
                    logger.warning(f"[reveal_file] Download error: {resp.error}")
        except Exception as e:
            logger.warning(f"[reveal_file] adownload_files failed for {file_path}: {e}")

    # 回退同步下载接口（放到线程池执行）
    if hasattr(backend, "download_files"):
        try:
            responses = await run_blocking_io(backend.download_files, [file_path])
            if responses:
                resp = responses[0]
                logger.info(
                    f"[reveal_file] download_files response: path={resp.path}, error={resp.error}, content_len={len(resp.content) if resp.content else 0}"
                )
                if resp.content:
                    return resp.content
                elif resp.error:
                    logger.warning(f"[reveal_file] Download error: {resp.error}")
        except Exception as e:
            logger.warning(f"[reveal_file] download_files failed for {file_path}: {e}")

    return None


async def _read_file_from_filesystem(file_path: str) -> Optional[bytes]:
    """非沙箱模式下的兜底：直接从本地文件系统读取文件内容"""
    try:

        # 仅读取存在且不超限的小文件
        def _read_small_file() -> Optional[bytes]:
            if not os.path.isfile(file_path):
                return None
            if os.path.getsize(file_path) > _LOCAL_REF_RESOLUTION_MAX_BYTES:
                logger.warning(
                    "[reveal_file] Skipping filesystem fallback read for large file: %s",
                    file_path,
                )
                return None
            with open(file_path, "rb") as file:
                return file.read()

        # 阻塞读放到线程池
        content = await run_blocking_io(_read_small_file)
        if content is not None:
            return content
        logger.debug(f"[reveal_file] File not found on filesystem: {file_path}")
    except Exception as e:
        logger.warning(f"[reveal_file] Failed to read from filesystem: {file_path}: {e}")
    return None


def _is_file_path(file_path: str) -> bool:
    # 判断路径是否为本机存在的普通文件
    return os.path.isfile(file_path)


async def _upload_filesystem_file(
    file_path: str,
    storage: Any,
    filename: str,
    mime_type: str,
):
    """Upload a local file handle directly without materializing it as bytes."""

    # 直接以文件句柄流式上传，避免把整个文件读进内存
    def _open_file():
        return open(file_path, "rb")

    file = await run_blocking_io(_open_file)
    try:
        return await storage.upload_file(
            file=file,
            folder="revealed_files",
            filename=filename,
            content_type=mime_type,
            # skip_size_limit：此处已在上游做过大小校验，跳过存储层的二次限制
            skip_size_limit=True,
        )
    finally:
        # 确保文件句柄关闭
        await run_blocking_io(file.close)


# ---------------------------------------------------------------------------
# 本地资源引用检测与替换
# 背景：Markdown/HTML/SVG 等文件可能引用本地图片/音视频；直接展示会因用户无法访问
# 隔离环境而失效。此处作为兜底，把这些本地引用上传到 S3 并替换为可访问 URL。
# ---------------------------------------------------------------------------

# 需要处理的文件扩展名（这些文件类型可能引用本地资源）
_RESOLVABLE_EXTENSIONS = {".md", ".markdown", ".html", ".htm", ".svg", ".xhtml"}

# 可上传的资源扩展名（图片、视频、音频）
_UPLOADABLE_EXTENSIONS = {
    # 图片
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".bmp",
    ".ico",
    ".avif",
    # 视频
    ".mp4",
    ".webm",
    ".mov",
    ".avi",
    ".wmv",
    ".mkv",
    ".ogv",
    # 音频
    ".mp3",
    ".wav",
    ".ogg",
    ".aac",
    ".flac",
    ".m4a",
    ".opus",
}

# 正则模式
# 分别匹配 Markdown 图片、HTML 媒体标签、CSS url()、SVG <image> 中的资源路径
_RE_MD_LINK = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")  # ![alt](path)
_RE_HTML_SRC = re.compile(
    r'<(img|video|audio|source|iframe)\b[^>]*(?:src|href)=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_RE_CSS_URL = re.compile(r'url\(["\']?([^)"\']+)["\']?\)')  # CSS url()
_RE_SVG_IMAGE = re.compile(r'<image\b[^>]*href=["\']([^"\']+)["\']', re.IGNORECASE)


def _is_local_path(path: str) -> bool:
    """判断路径是否为本地文件路径（非 http/https/data URL）"""
    # 排除各种远程/内联/锚点协议，剩下的才需要上传替换
    stripped = path.strip()
    return (
        not stripped.startswith("http://")
        and not stripped.startswith("https://")
        and not stripped.startswith("data:")
        and not stripped.startswith("#")
        and not stripped.startswith("blob:")
        and not stripped.startswith("mailto:")
    )


def _is_remote_url(path: str) -> bool:
    """判断路径是否为可直接返回的远程 URL"""
    # 需同时具备 http/https scheme 与主机名
    parsed = urlparse(path.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _get_filename_from_path(path: str) -> str:
    """从本地路径或 URL 中提取文件名。"""
    # 远程 URL：取其路径末段（解码 % 编码）
    if _is_remote_url(path):
        parsed = urlparse(path.strip())
        candidate = os.path.basename(unquote(parsed.path))
        if candidate:
            return candidate

    # 本地路径：取末段；兜底返回原始路径
    candidate = os.path.basename(path.rstrip("/"))
    return candidate or path


def _is_uploadable_resource(path: str) -> bool:
    """判断路径是否指向可上传的资源文件"""
    # 去掉 query string / fragment
    clean = path.split("?")[0].split("#")[0]
    ext = os.path.splitext(clean)[1].lower()
    return ext in _UPLOADABLE_EXTENSIONS


def _needs_local_ref_resolution(filename: str, mime_type: str) -> bool:
    """判断文件是否需要做本地引用替换"""
    # 依据扩展名或 MIME 判断该文件是否可能内嵌本地资源引用
    ext = os.path.splitext(filename)[1].lower()
    if ext in _RESOLVABLE_EXTENSIONS:
        return True
    if mime_type in ("text/markdown", "text/x-markdown", "text/html", "image/svg+xml"):
        return True
    return False


async def _upload_local_resource(
    local_path: str,
    file_dir: str,
    backend: Any,
    storage: Any,
    base_url: str,
) -> Optional[str]:
    """
    尝试下载并上传一个本地资源文件到 S3，返回 proxy URL。
    失败时返回 None。
    """
    try:
        # 相对路径以宿主文件所在目录为基准解析为绝对路径
        if os.path.isabs(local_path):
            abs_path = local_path
        else:
            abs_path = os.path.normpath(os.path.join(file_dir, local_path))

        content = await _download_file_from_backend(backend, abs_path)
        # 非沙箱且允许本地兜底：后端下载失败时直接以文件句柄流式上传
        if (
            content is None
            and not _is_sandbox_backend(backend)
            and _local_filesystem_fallback_enabled()
        ):
            if not await run_blocking_io(_is_file_path, abs_path):
                return None
            res_filename = os.path.basename(abs_path)
            res_mime = get_mime_type(res_filename)
            upload_result = await _upload_filesystem_file(
                abs_path,
                storage,
                res_filename,
                res_mime,
            )
            url = f"{base_url}/api/upload/file/{upload_result.key}"
            logger.info(f"[reveal_file] Uploaded local resource {local_path} -> {url}")
            return url
        if content is None:
            return None

        # 已拿到字节：写入 spooled 临时文件后上传
        res_filename = os.path.basename(abs_path)
        res_mime = get_mime_type(res_filename)
        with SpooledTemporaryFile(
            max_size=_UPLOAD_SPOOL_MEMORY_LIMIT,
            mode="w+b",
        ) as spooled:
            await run_blocking_io(spooled.write, content)
            # 及时释放大 bytes 引用，降低内存占用
            del content
            await run_blocking_io(spooled.seek, 0)
            upload_result = await storage.upload_file(
                file=spooled,
                folder="revealed_files",
                filename=res_filename,
                content_type=res_mime,
                skip_size_limit=True,
            )
        url = f"{base_url}/api/upload/file/{upload_result.key}"
        logger.info(f"[reveal_file] Uploaded local resource {local_path} -> {url}")
        return url
    except Exception as e:
        # 单个资源失败不影响其余资源，返回 None 让调用方保留原始引用
        logger.warning(f"[reveal_file] Failed to upload local resource {local_path}: {e}")
        return None


async def _resolve_local_references(
    content: bytes,
    file_dir: str,
    backend: Any,
    storage: Any,
    base_url: str,
) -> bytes:
    """
    检测并替换文本内容中的本地资源引用（图片、视频、音频）为 S3 URL。
    支持 Markdown、HTML、SVG、CSS 等文件类型。

    作为兜底机制：agent 提示词已要求它主动上传资源并使用 URL，
    此函数用于捕获遗漏的本地引用。
    """
    # 非 UTF-8 文本无法解析，直接原样返回
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content

    # 收集所有需要上传的本地资源路径（保持原始大小写用于替换，但去重时不区分）
    # seen_normalized 以规范化路径去重，unique_paths 保留原始写法用于精确替换
    seen_normalized = set()
    unique_paths: list[str] = []

    for pattern in (_RE_MD_LINK, _RE_HTML_SRC, _RE_SVG_IMAGE, _RE_CSS_URL):
        for match in pattern.finditer(text):
            # 不同 pattern 的路径在不同 group
            # 有第 2 组的（MD/HTML）取第 2 组，否则（CSS/SVG）取第 1 组
            path = (
                match.group(2).strip()
                if match.lastindex and match.lastindex >= 2
                else match.group(1).strip()
            )
            # 仅处理"本地路径 + 可上传资源"的引用
            if _is_local_path(path) and _is_uploadable_resource(path):
                normalized = os.path.normpath(path)
                if normalized not in seen_normalized:
                    seen_normalized.add(normalized)
                    unique_paths.append(path)

    if not unique_paths:
        return content
    # 数量上限保护：超出只处理前 N 个
    upload_limit = _get_local_ref_upload_limit()
    if len(unique_paths) > upload_limit:
        logger.warning(
            "[reveal_file] Found %s local resource references; only uploading first %s",
            len(unique_paths),
            upload_limit,
        )
        unique_paths = unique_paths[:upload_limit]

    logger.info(
        f"[reveal_file] Found {len(unique_paths)} local resource reference(s), "
        f"uploading to S3 as fallback"
    )

    # 批量上传
    # 先建立"原始路径 -> S3 URL"映射，再统一替换文本
    path_to_url: dict[str, str] = {}
    for ref_path in unique_paths:
        url = await _upload_local_resource(ref_path, file_dir, backend, storage, base_url)
        if url:
            path_to_url[ref_path] = url

    if not path_to_url:
        return content

    # 替换所有匹配到的本地路径
    # 只替换匹配串内的路径部分，尽量不破坏其余语法结构
    def _replacer(match: re.Match) -> str:
        original = match.group(0)
        for group_idx in (1, 2):
            if match.lastindex is not None and group_idx <= match.lastindex:
                path = (
                    match.group(group_idx).strip()
                    if match.lastindex and match.lastindex >= group_idx
                    else ""
                )
                if path in path_to_url:
                    return original.replace(path, path_to_url[path], 1)
        return original

    for pattern in (_RE_MD_LINK, _RE_HTML_SRC, _RE_SVG_IMAGE, _RE_CSS_URL):
        text = pattern.sub(_replacer, text)

    return text.encode("utf-8")


@tool
async def reveal_file(
    file_path: Annotated[
        str, "要展示的文件路径（本地绝对路径、相对路径，或可直接访问的 http(s) URL）"
    ],
    description: Annotated[
        Optional[str], "对文件内容的简要描述，帮助用户理解为什么要查看这个文件"
    ] = None,
    runtime: ToolRuntime = None,  # type: ignore[assignment]
) -> str:
    """
    向用户展示/推荐一个文件

    用户要求查看、打开、显示文件时，必须调用此工具。
    只回复文件路径或文件名是不够的。
    用户无法直接访问隔离环境中的文件系统，`reveal_file` 才会把文件真正暴露给前端界面。

    当你想让用户查看某个文件时，使用此工具。
    前端自动给用户显示可点击的文件。

    Args:
        file_path: 要展示的文件路径（本地绝对路径、相对路径，或可直接访问的 http(s) URL）
        description: 对文件内容的简要描述，帮助用户理解为什么要查看这个文件（可选）

    Returns:
        JSON 格式的结果，包含文件信息
    """
    # 情形一：本身就是可直接访问的远程 URL，无需下载/上传，直接登记并返回
    if _is_remote_url(file_path):
        filename = _get_filename_from_path(file_path)
        mime_type = get_mime_type(filename)
        file_category = get_file_category(mime_type)
        remote_result = {
            "key": file_path,
            "url": file_path,
            "name": filename,
            "type": file_category,
            "mime_type": mime_type,
            "size": 0,
            "_meta": {
                "path": file_path,
                "description": description or "",
                "source": "remote_url",
            },
        }
        await _index_revealed_file(
            runtime=runtime,
            file_name=filename,
            file_category=file_category,
            mime_type=mime_type,
            file_size=0,
            url=file_path,
            file_key=file_path,
            description=description or "",
            original_path=file_path,
        )
        return await _json_dumps_result(remote_result)

    storage = await _get_storage()

    # 从运行时注入获取后端；跨进程安全，不依赖 ContextVar
    backend = get_backend_from_runtime(runtime)

    if backend is None:
        # 无后端可用：降级为只回传原始路径（前端无法真正打开文件）
        logger.warning("Backend not available from runtime, returning raw path")
        backend_unavailable_result: dict[str, Any] = {
            "type": "file_reveal",
            "file": {
                "path": file_path,
                "description": description or "",
            },
        }
        return await _json_dumps_result(backend_unavailable_result)

    try:
        # 下载前先按已知大小拦截超大文件，省去无谓下载
        known_size = await _get_backend_file_size(backend, file_path)
        max_upload_bytes = _get_reveal_file_upload_max_bytes()
        if known_size is not None and known_size > max_upload_bytes:
            logger.warning(
                "[reveal_file] Refusing oversized backend file before download: %s size=%s max=%s",
                file_path,
                known_size,
                max_upload_bytes,
            )
            too_large_result = {
                "type": "file_reveal",
                "file": {
                    "path": file_path,
                    "description": description or "",
                    "error": "file_too_large",
                    "size": known_size,
                    "max_size": max_upload_bytes,
                },
            }
            return await _json_dumps_result(too_large_result)

        file_content = await _download_file_from_backend(backend, file_path)
        use_filesystem_stream = False
        # 下载后二次校验：无 Content-Length 场景下按实际字节数再拦一次
        if file_content is not None and len(file_content) > max_upload_bytes:
            content_size = len(file_content)
            del file_content
            logger.warning(
                "[reveal_file] Refusing oversized backend file after download: %s size=%s max=%s",
                file_path,
                content_size,
                max_upload_bytes,
            )
            too_large_result = {
                "type": "file_reveal",
                "file": {
                    "path": file_path,
                    "description": description or "",
                    "error": "file_too_large",
                    "size": content_size,
                    "max_size": max_upload_bytes,
                },
            }
            return await _json_dumps_result(too_large_result)

        # 非沙箱模式兜底：backend 下载失败时尝试直接读取本地文件系统
        # 标记 use_filesystem_stream，后续以文件句柄流式上传（避免读入内存）
        if (
            file_content is None
            and not _is_sandbox_backend(backend)
            and _local_filesystem_fallback_enabled()
        ):
            logger.info(
                f"[reveal_file] Backend download failed, trying filesystem fallback for {file_path}"
            )
            use_filesystem_stream = await run_blocking_io(_is_file_path, file_path)

        # 既拿不到内容也无法走文件系统流：判定文件缺失
        if file_content is None and not use_filesystem_stream:
            logger.error(f"Failed to read file {file_path} from backend")
            missing_file_result = {
                "type": "file_reveal",
                "file": {
                    "path": file_path,
                    "description": description or "",
                    "error": "file_not_found_or_empty",
                },
            }
            return await _json_dumps_result(missing_file_result)

        filename = _get_filename_from_path(file_path)
        mime_type = get_mime_type(filename)

        # 对可包含本地资源引用的文件（Markdown、HTML、SVG 等），兜底替换本地路径
        base_url = get_base_url_from_runtime(runtime)
        if not base_url:
            logger.warning("[reveal_file] base_url is empty, URL may be incomplete")

        # 需要引用替换时：若此前走的是文件系统流，则在大小允许时读入内容再替换
        if _needs_local_ref_resolution(filename, mime_type) and (
            file_content is not None
            or not use_filesystem_stream
            or _can_resolve_local_filesystem_refs(file_path)
        ):
            if file_content is None and use_filesystem_stream:
                file_content = await _read_file_from_filesystem(file_path)
            if file_content is None:
                raise ValueError(f"Unable to read file content for {file_path}")
            file_dir = os.path.dirname(file_path)
            file_content = await _resolve_local_references(
                file_content, file_dir, backend, storage, base_url
            )
            # 已读入并可能改写内容，改走内存上传分支
            use_filesystem_stream = False

        # 上传：文件系统流式上传 或 内存 spooled 上传
        if use_filesystem_stream:
            upload_result = await _upload_filesystem_file(file_path, storage, filename, mime_type)
        else:
            with SpooledTemporaryFile(
                max_size=_UPLOAD_SPOOL_MEMORY_LIMIT,
                mode="w+b",
            ) as spooled:
                await run_blocking_io(spooled.write, file_content)
                # 及时释放大 bytes 引用
                del file_content
                await run_blocking_io(spooled.seek, 0)
                upload_result = await storage.upload_file(
                    file=spooled,
                    folder="revealed_files",
                    filename=filename,
                    content_type=mime_type,
                    skip_size_limit=True,
                )

        # 以上传返回的实际 content_type 归类（更准确）
        file_category = get_file_category(upload_result.content_type or mime_type)

        # 生成经后端代理的可访问 URL
        proxy_url = f"{base_url}/api/upload/file/{upload_result.key}"

        # 组装与前端 UploadResult 一致的结果结构
        reveal_result = {
            "key": upload_result.key,
            "url": proxy_url,
            "name": filename,
            "type": file_category,
            "mime_type": upload_result.content_type or mime_type,
            "size": upload_result.size,
            "_meta": {
                "path": file_path,
                "description": description or "",
            },
        }
        logger.info(f"Successfully uploaded {file_path} to S3: {upload_result.url}")

        # 登记到已展示文件索引
        await _index_revealed_file(
            runtime=runtime,
            file_name=filename,
            file_category=file_category,
            mime_type=upload_result.content_type or mime_type,
            file_size=upload_result.size,
            url=proxy_url,
            file_key=upload_result.key,
            description=description or "",
            original_path=file_path,
        )

        return await _json_dumps_result(reveal_result)

    except Exception as e:
        # 兜底：任何异常都返回结构化错误结果，不抛出以免中断 Agent
        logger.error(f"Error processing file {file_path}: {e}")
        error_result = {
            "type": "file_reveal",
            "file": {
                "path": file_path,
                "description": description or "",
                "error": str(e),
            },
        }
        return await _json_dumps_result(error_result)


def get_reveal_file_tool() -> BaseTool:
    """获取 reveal_file 工具实例"""
    return reveal_file

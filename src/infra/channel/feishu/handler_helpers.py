"""Helper functions and constants for Feishu message handling."""

import inspect
import mimetypes
import time
from typing import Any
from urllib.parse import quote, unquote, urlparse

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)

# Redis key prefix for Feishu chat session mapping
# 飞书 chat_id -> 会话 ID 映射在 Redis 中的键前缀。
FEISHU_SESSION_KEY_PREFIX = "feishu:session:"

# 事件类型定义
EVENT_MESSAGE_CHUNK = "message:chunk"
EVENT_THINKING = "thinking"
EVENT_TOOL_START = "tool:start"
EVENT_TOOL_RESULT = "tool:result"
EVENT_DONE = "done"
# 流式卡片更新的防抖间隔（秒）与"首帧"字符数，用于平衡实时性与飞书更新频率。
FEISHU_STREAM_UPDATE_DEBOUNCE_SECONDS = 0.12
FEISHU_STREAM_FIRST_PAINT_CHARS = 12
# 代理上传 URL 中用于识别存储 key 的路径标记；卡片末尾"查看会话"链接文案。
_UPLOAD_FILE_PATH_MARKER = "/api/upload/file/"
_SESSION_LINK_TEXT = "查看这条消息"
# reveal 下载：分块大小、流式下载上限、旧版整块下载上限（超限即拒绝，防内存打爆）。
FEISHU_REVEAL_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
FEISHU_REVEAL_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
FEISHU_REVEAL_LEGACY_DOWNLOAD_MAX_BYTES = 2 * 1024 * 1024


async def _get_backend_object_size(backend: Any, key: str) -> int | None:
    # 预检对象大小：若后端支持 get_size 则返回其字节数（兼容同步/异步实现），
    # 供下载前的超限判断使用；不支持或出错则返回 None。
    method = getattr(backend, "get_size", None)
    if not callable(method):
        return None
    try:
        size = method(key)
        if inspect.isawaitable(size):
            size = await size
        # 显式排除 bool（isinstance(True, int) 为真）等异常返回。
        if isinstance(size, bool) or size is None:
            return None
        value = int(size)
        return value if value >= 0 else None
    except Exception as e:
        logger.debug("[Feishu] Failed to preflight storage object size for %s: %s", key, e)
        return None


def _raise_if_storage_object_too_large(size: int, key: str) -> None:
    # 超过流式下载上限则抛错，避免下载超大对象。
    if size > FEISHU_REVEAL_DOWNLOAD_MAX_BYTES:
        raise ValueError(
            f"Storage object too large for Feishu reveal download: {key} "
            f"size={size} bytes (max {FEISHU_REVEAL_DOWNLOAD_MAX_BYTES})"
        )


async def _download_storage_object_to_file(
    backend: Any,
    key: str,
    file: Any,
    *,
    chunk_size: int = FEISHU_REVEAL_DOWNLOAD_CHUNK_SIZE,
) -> int:
    """Download storage object into a file sink, preferring streaming APIs."""
    # 先按可得的大小做预检超限判断。
    size = await _get_backend_object_size(backend, key)
    if size is not None:
        _raise_if_storage_object_too_large(size, key)

    # 优先用后端原生的 download_to_file（最省内存）。
    if hasattr(backend, "download_to_file"):
        return int(await backend.download_to_file(key, file, chunk_size=chunk_size))

    # 其次流式分块下载，边写边累加并在累计超限时中止。
    if hasattr(backend, "download_stream"):
        total_size = 0
        async for chunk in backend.download_stream(key, chunk_size=chunk_size):
            if total_size + len(chunk) > FEISHU_REVEAL_DOWNLOAD_MAX_BYTES:
                raise ValueError(
                    f"Storage object too large for Feishu reveal download: {key} "
                    f"size>{FEISHU_REVEAL_DOWNLOAD_MAX_BYTES} bytes"
                )
            # 文件写入是阻塞 IO，放到线程池执行以免阻塞事件循环。
            await run_blocking_io(file.write, chunk)
            total_size += len(chunk)
        await run_blocking_io(file.seek, 0)
        return total_size

    # 最后兜底：一次性 download（受更小的旧版上限约束）。
    data = await backend.download(key)
    if not data:
        return 0
    size = len(data)
    if size > FEISHU_REVEAL_LEGACY_DOWNLOAD_MAX_BYTES:
        raise ValueError(
            f"Storage object too large for legacy bytes download: {size} bytes "
            f"(max {FEISHU_REVEAL_LEGACY_DOWNLOAD_MAX_BYTES})"
        )
    await run_blocking_io(file.write, data)
    await run_blocking_io(file.seek, 0)
    return size


async def _get_feishu_session_id(chat_id: str) -> str:
    """获取飞书聊天对应的当前 session ID，如果不存在则创建默认的"""
    from src.infra.storage.redis import RedisStorage

    storage = RedisStorage()
    key = f"{FEISHU_SESSION_KEY_PREFIX}{chat_id}"
    session_id = await storage.get(key)

    if session_id is None:
        # 默认使用 chat_id 作为 session ID（兼容旧数据）
        session_id = f"feishu_{chat_id}"
        await storage.set(key, session_id)

    return session_id


async def _create_new_feishu_session(chat_id: str) -> str:
    """为飞书聊天创建新的 session ID"""
    from src.infra.storage.redis import RedisStorage

    storage = RedisStorage()
    key = f"{FEISHU_SESSION_KEY_PREFIX}{chat_id}"

    # 使用时间戳生成唯一的 session ID
    timestamp = int(time.time())
    session_id = f"feishu_{chat_id}_{timestamp}"

    # 存储到 Redis，不设置过期时间
    # 覆盖该 chat 的会话映射，使后续消息进入新会话（对应 /new 命令）。
    await storage.set(key, session_id)

    logger.info(f"[Feishu] Created new session for chat {chat_id}: {session_id}")
    return session_id


def _storage_key_from_upload_url(url: str) -> str | None:
    """Extract the LambChat storage key from a proxied upload URL."""
    # 从形如 .../api/upload/file/<key> 的代理 URL 中还原出存储 key。
    if not url:
        return None
    try:
        path = urlparse(url).path
    except Exception:
        path = url

    if _UPLOAD_FILE_PATH_MARKER not in path:
        return None
    key = path.split(_UPLOAD_FILE_PATH_MARKER, 1)[1]
    return unquote(key).lstrip("/") or None


def _media_name_from_entry(entry: dict[str, Any], key: str | None, url: str, index: int) -> str:
    # 推断媒体文件名：优先取条目里的显式名字段，否则从 key/url 末段解析，最后用兜底名。
    for field in ("name", "file_name", "filename"):
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()

    source = key or urlparse(url).path or url
    name = unquote(source.rstrip("/").rsplit("/", 1)[-1])
    return name or f"attachment-{index + 1}.bin"


def _media_mime_type(entry: dict[str, Any], name: str, url: str) -> str:
    # 推断 MIME：优先取条目里的显式类型字段，否则按文件名/URL 猜测，兜底为二进制流。
    for field in ("mime_type", "mimeType", "content_type", "contentType"):
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return mimetypes.guess_type(name or url)[0] or "application/octet-stream"


def _media_attachment_type(media_type: str, mime_type: str) -> str:
    # 归一化为飞书附件类型：image/audio/video，其余归为 document。
    if media_type == "image" or mime_type.startswith("image/"):
        return "image"
    if media_type == "audio" or mime_type.startswith("audio/"):
        return "audio"
    if media_type == "video" or mime_type.startswith("video/"):
        return "video"
    return "document"


def _media_file_info_from_entry(entry: dict[str, Any], index: int) -> dict[str, Any] | None:
    """Normalize tool media entries into FeishuResponseCollector file metadata."""
    # 把工具产出的媒体条目规整成收集器可用的文件元数据；无法定位 key 则丢弃。
    media_type = str(entry.get("type") or "").lower()
    if media_type not in {"image", "file", "audio", "video", ""}:
        return None

    url = entry.get("url")
    url = url.strip() if isinstance(url, str) else ""
    key = entry.get("key")
    key = key.strip() if isinstance(key, str) else None
    # 没给 key 时尝试从上传 URL 反解。
    if not key and url:
        key = _storage_key_from_upload_url(url)
    if not key:
        return None

    name = _media_name_from_entry(entry, key, url, index)
    mime_type = _media_mime_type(entry, name, url)
    return {
        "key": key,
        "name": name,
        "type": _media_attachment_type(media_type, mime_type),
        "mime_type": mime_type,
        "url": url,
    }


def _extract_tool_media_files(result: Any) -> list[dict[str, Any]]:
    """Extract app-storage-backed image/file outputs from tool results."""
    if not isinstance(result, dict):
        return []

    # 从工具结果的 images 列表与 blocks 列表中收集候选媒体条目。
    candidates: list[dict[str, Any]] = []

    images = result.get("images")
    if isinstance(images, list):
        candidates.extend(item for item in images if isinstance(item, dict))

    blocks = result.get("blocks")
    if isinstance(blocks, list):
        candidates.extend(
            item
            for item in blocks
            if isinstance(item, dict) and item.get("type") in {"image", "file", "audio", "video"}
        )

    # 规整并按存储 key 去重，得到最终文件列表。
    file_infos: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, entry in enumerate(candidates):
        file_info = _media_file_info_from_entry(entry, index)
        if not file_info or file_info["key"] in seen_keys:
            continue
        seen_keys.add(file_info["key"])
        file_infos.append(file_info)
    return file_infos


def _build_session_run_url(session_id: str, run_id: str | None = None) -> str:
    # 构建"查看会话"深链：/chat/<session>[?run_id=<run>]，有配置 APP_BASE_URL 则拼成绝对地址。
    path = f"/chat/{quote(session_id, safe='')}"
    if run_id:
        path = f"{path}?run_id={quote(run_id, safe='')}"

    base_url = getattr(settings, "APP_BASE_URL", "").rstrip("/")
    return f"{base_url}{path}" if base_url else path

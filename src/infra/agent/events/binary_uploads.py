"""Upload binary MCP result blocks and replace inline base64 with URLs."""

import base64
import mimetypes
import uuid
from tempfile import SpooledTemporaryFile

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger

logger = get_logger(__name__)

# SpooledTemporaryFile 内存阈值：超过 2MB 才落盘，小文件全程留在内存
_SPOOL_MAX_MEMORY_BYTES = 2 * 1024 * 1024
# 单个二进制块允许上传的最大字节数（50MB）
_BINARY_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
# 单次结果中所有二进制块的总字节上限（50MB）
_BINARY_UPLOAD_TOTAL_MAX_BYTES = 50 * 1024 * 1024
# 单次结果最多上传的二进制块数量，超出的块会被脱敏丢弃
_BINARY_UPLOAD_MAX_BLOCKS = 4
# 流式解码 base64 时每次处理的字符数（4MB），避免一次性构造超大 bytes
_BASE64_CHUNK_CHARS = 4 * 1024 * 1024


def _redact_failed_binary_upload(block: dict) -> None:
    # 上传失败：移除 base64 原文并标记错误原因，避免把大体积 base64 回传前端
    block.pop("base64", None)
    block["upload_error"] = "binary_upload_failed"


def _redact_oversized_binary_upload(block: dict) -> None:
    # 体积超限：移除 base64 原文并标记"过大"
    block.pop("base64", None)
    block["upload_error"] = "binary_upload_too_large"


def _redact_excess_binary_upload(block: dict) -> None:
    # 块数超限：移除 base64 原文并标记"块数过多"
    block.pop("base64", None)
    block["upload_error"] = "binary_upload_too_many_blocks"


def _redact_all_base64_blocks(blocks: list) -> None:
    # 批量脱敏：存储初始化失败等场景下，把所有 base64 块统一标记为失败
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("base64"), str):
            _redact_failed_binary_upload(block)


def _estimated_base64_decoded_size(b64_data: str) -> int:
    # 由 base64 字符串长度估算解码后字节数：去掉尾部 '=' 填充后按 4 字符 -> 3 字节换算
    stripped = b64_data.rstrip("=")
    return (len(stripped) * 3) // 4


def _decode_base64_to_file(b64_data: str, file, *, max_bytes: int) -> int:
    """Decode base64 data into a file-like object without building one large bytes."""
    # total 记录已写入字节数，carry 保存上一块无法凑成 4 的倍数的残余字符
    total = 0
    carry = ""

    # 按 _BASE64_CHUNK_CHARS 分块解码，逐块写入文件，避免一次性占用大量内存
    for start in range(0, len(b64_data), _BASE64_CHUNK_CHARS):
        # 把上次残余字符拼到本块头部
        chunk = carry + b64_data[start : start + _BASE64_CHUNK_CHARS]
        # base64 必须按 4 字符对齐解码，取当前可整除的长度
        decode_len = (len(chunk) // 4) * 4
        if decode_len == 0:
            carry = chunk
            continue

        data = base64.b64decode(chunk[:decode_len])
        file.write(data)
        total += len(data)
        # 边解码边检查体积，超限立即抛出（后续被识别为 too_large）
        if total > max_bytes:
            raise ValueError("binary_upload_too_large")
        # 未对齐的尾部留到下一轮
        carry = chunk[decode_len:]

    # 处理最后残余（对齐后的尾块）
    if carry:
        data = base64.b64decode(carry)
        file.write(data)
        total += len(data)
        if total > max_bytes:
            raise ValueError("binary_upload_too_large")

    # 重置文件游标到开头，供后续上传读取
    file.seek(0)
    return total


async def upload_binary_blocks(result: dict, base_url: str) -> None:
    """Upload base64 blocks in-place, replacing each `base64` payload with a URL."""
    # result.blocks 必须是列表，否则无二进制块可处理，直接返回
    blocks = result.get("blocks")
    if not isinstance(blocks, list):
        return

    # storage 延迟初始化（首个有效块出现时才连接对象存储）
    storage = None
    # 已成功上传的块计数与累计估算字节数，用于限流
    uploaded_block_count = 0
    estimated_total_bytes = 0

    for block in blocks:
        if not isinstance(block, dict):
            continue

        # 仅处理带 base64 字符串负载的块
        b64_data = block.get("base64")
        if not b64_data or not isinstance(b64_data, str):
            continue

        # 上传前先按估算体积/块数/总量做三重限流，命中即脱敏跳过
        estimated_bytes = _estimated_base64_decoded_size(b64_data)
        if estimated_bytes > _BINARY_UPLOAD_MAX_BYTES:
            _redact_oversized_binary_upload(block)
            continue
        if uploaded_block_count >= _BINARY_UPLOAD_MAX_BLOCKS:
            _redact_excess_binary_upload(block)
            continue
        if estimated_total_bytes + estimated_bytes > _BINARY_UPLOAD_TOTAL_MAX_BYTES:
            _redact_oversized_binary_upload(block)
            continue

        # 首次需要上传时才初始化存储；失败则把所有块脱敏并整体放弃
        if storage is None:
            try:
                from src.infra.storage.s3.service import get_or_init_storage

                storage = await get_or_init_storage()
            except Exception as exc:
                logger.warning("Failed to initialize storage for binary upload: %s", exc)
                _redact_all_base64_blocks(blocks)
                return

        try:
            # 依据 mime_type 推断文件扩展名，生成随机文件名
            mime_type = block.get("mime_type", "application/octet-stream")
            ext = (mimetypes.guess_extension(mime_type) or ".bin").lstrip(".")
            filename = f"binary_{uuid.uuid4().hex[:8]}.{ext}"

            # 用 spooled 临时文件承接解码结果：小文件在内存、大文件自动落盘
            with SpooledTemporaryFile(
                max_size=_SPOOL_MAX_MEMORY_BYTES,
                mode="w+b",
            ) as spooled:
                # 解码属阻塞 IO，放到线程池执行避免阻塞事件循环
                size = await run_blocking_io(
                    _decode_base64_to_file,
                    b64_data,
                    spooled,
                    max_bytes=_BINARY_UPLOAD_MAX_BYTES,
                )
                # 上传到对象存储的 tool_binaries 目录（体积已自校验，跳过存储层限制）
                upload_result = await storage.upload_file(
                    file=spooled,
                    folder="tool_binaries",
                    filename=filename,
                    content_type=mime_type,
                    skip_size_limit=True,
                )

            # 生成经后端代理的下载 URL（base_url 缺省则用相对路径）
            proxy_url = (
                f"{base_url}/api/upload/file/{upload_result.key}"
                if base_url
                else f"/api/upload/file/{upload_result.key}"
            )
            # 用 URL 就地替换 base64，显著缩小回传给前端的体积
            block.pop("base64", None)
            block["url"] = proxy_url
            uploaded_block_count += 1
            estimated_total_bytes += estimated_bytes
            logger.info(
                "Uploaded binary block to storage: %s (%d bytes)",
                upload_result.key,
                size,
            )
        except ValueError as exc:
            # 解码阶段抛出的体积超限，按"过大"脱敏
            if str(exc) == "binary_upload_too_large":
                _redact_oversized_binary_upload(block)
                continue
            logger.warning("Failed to upload binary block: %s", exc)
            _redact_failed_binary_upload(block)
        except Exception as exc:
            # 其余异常统一按上传失败脱敏，保证不把 base64 泄漏到前端
            logger.warning("Failed to upload binary block: %s", exc)
            _redact_failed_binary_upload(block)

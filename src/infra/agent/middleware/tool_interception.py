"""Tool call interception middleware — MCP quota, deferred tool search, binary upload."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 本文件是三个互相独立、各自解决一类问题的 LangChain Agent 中间件的集合，
# 都通过 awrap_tool_call / awrap_model_call 钩子拦截 Agent 的工具调用生命周期：
#
#   1. MCPQuotaMiddleware：沙箱里的 execute 工具可以通过 `mcporter call ...`
#      间接调用 MCP 服务，这类"绕道"调用不会经过 MCP 工具本身的配额检查，
#      于是这里专门识别并拦截这种 shell 命令，补上配额扣减/拒绝逻辑；
#   2. ToolResultBinaryMiddleware：MCP 工具结果、read_file 读到的二进制文件，
#      都不适合把原始 base64 数据直接丢给 LLM（浪费海量 token、各家模型
#      对内联媒体块的处理方式还不统一），统一先上传到对象存储换成 URL；
#   3. ToolSearchMiddleware：配合"延迟工具加载"机制——大量 MCP 工具不会
#      一开始就注册进 LLM 的工具列表（会占用巨大的 prompt 空间），而是
#      按需通过 search_tools 工具搜索、发现后再动态注入，本中间件负责
#      在每次模型调用前同步"已发现工具"状态，以及在工具调用时拦截执行
#      这些尚未注册进 ToolNode 的动态工具。
#
# 三者的共同风格：任何拦截/增强逻辑失败都尽量优雅降级（吞异常、记警告、
# 返回 None 或原始结果），绝不能因为这层"锦上添花"的处理而搞垮正常的
# 工具调用主链路。
# ============================================================================

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import shlex
import uuid
from collections.abc import Awaitable, Callable
from tempfile import SpooledTemporaryFile
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

if TYPE_CHECKING:
    from src.infra.tool.deferred_manager import DeferredToolManager

from src.infra.agent.middleware._helpers import (
    _append_system_text_blocks,
    _normalize_prompt_text,
    _system_message_to_blocks,
    _tool_sort_key,
)
from src.infra.async_utils import run_blocking_io
from src.infra.tool.deferred_manager import DEFERRED_TOOL_SEARCH_GUIDE
from src.kernel.config import settings

logger = logging.getLogger(__name__)

_PROMPT_CACHE_VOLATILE_TOOL_EXTRA = "_lambchat_prompt_cache_volatile"
# 单个二进制文件在写入前先落到内存里的最大字节数，超出则自动落盘（临时文件），
# 避免超大文件长期占用进程内存
_BINARY_UPLOAD_SPOOL_MEMORY_LIMIT = 2 * 1024 * 1024
# 单个 MCP 返回的 base64 二进制块允许上传的最大字节数
_BINARY_BLOCK_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
# 同一次工具结果里，所有二进制块加起来允许上传的总字节数上限
_BINARY_BLOCK_UPLOAD_TOTAL_MAX_BYTES = 50 * 1024 * 1024
# 同一次工具结果里最多处理这么多个二进制块，防止一次结果里塞几十个媒体块拖垮性能
_BINARY_BLOCK_UPLOAD_MAX_BLOCKS = 4
# read_file 读到二进制文件时，允许自动上传的最大文件字节数
_READ_FILE_BINARY_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
# 流式 base64 解码时，每次处理的原始字符数（约 4MB 字符，解码后约 3MB 字节）
_BASE64_DECODE_CHUNK_CHARS = 4 * 1024 * 1024


# MCP content block types that may carry binary data
_BINARY_BLOCK_TYPES = frozenset(("image", "file"))

# Binary file extensions — read_file should upload these to S3 instead of returning garbled text
_BINARY_EXTENSIONS = frozenset(
    (
        # Images
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".svg",
        ".avif",
        ".tiff",
        ".tif",
        # Videos
        ".mp4",
        ".webm",
        ".mov",
        ".avi",
        ".wmv",
        ".mkv",
        ".ogv",
        # Audio
        ".mp3",
        ".wav",
        ".ogg",
        ".aac",
        ".flac",
        ".m4a",
        ".opus",
        # Documents
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
    )
)


def _redact_failed_binary_block(block: dict[str, Any]) -> dict[str, Any]:
    # 上传失败：去掉 base64 原始数据（绝不能让超大/无用的数据流入 LLM 上下文），
    # 换成一个错误标记，让 LLM 至少知道"这里本来有个文件，但没能处理成功"
    redacted = {k: v for k, v in block.items() if k != "base64"}
    redacted["upload_error"] = "binary_upload_failed"
    return redacted


def _redact_oversized_binary_block(block: dict[str, Any]) -> dict[str, Any]:
    # 超出单次结果总字节数上限，主动放弃上传（而不是费力上传了却告知超限）
    redacted = {k: v for k, v in block.items() if k != "base64"}
    redacted["upload_error"] = "binary_upload_too_large"
    return redacted


def _redact_excess_binary_block(block: dict[str, Any]) -> dict[str, Any]:
    # 超出单次结果允许处理的块数上限，后面多出来的块直接跳过上传
    redacted = {k: v for k, v in block.items() if k != "base64"}
    redacted["upload_error"] = "binary_upload_too_many_blocks"
    return redacted


def _estimated_base64_decoded_size(b64_data: str) -> int:
    # 不做真正解码，只靠字符数估算解码后的字节数（base64 每 4 字符编码 3 字节），
    # 先去掉末尾的 '=' 填充字符再计算，用作上传前的低成本预检查
    stripped = b64_data.rstrip("=")
    return (len(stripped) * 3) // 4


def _decode_base64_to_file(b64_data: str, file, *, max_bytes: int) -> int:
    # 流式解码：避免把整段 base64 字符串一次性解码进内存（可能是几十 MB），
    # 而是分块处理并直接写入目标文件对象
    total = 0
    # base64 必须按 4 字符为单位解码，carry 用来暂存上一块末尾凑不满 4 的余数字符，
    # 拼到下一块开头再继续解码，保证解码边界始终对齐
    carry = ""
    for start in range(0, len(b64_data), _BASE64_DECODE_CHUNK_CHARS):
        chunk = carry + b64_data[start : start + _BASE64_DECODE_CHUNK_CHARS]
        decode_len = (len(chunk) // 4) * 4
        if decode_len == 0:
            carry = chunk
            continue
        decoded = base64.b64decode(chunk[:decode_len])
        file.write(decoded)
        total += len(decoded)
        # 解码过程中随时检查是否超限，不必等整段解码完才发现太大——
        # 这是比 _estimated_base64_decoded_size 更权威的"实际大小"校验
        if total > max_bytes:
            raise ValueError("binary_upload_too_large")
        carry = chunk[decode_len:]
    if carry:
        decoded = base64.b64decode(carry)
        file.write(decoded)
        total += len(decoded)
        if total > max_bytes:
            raise ValueError("binary_upload_too_large")
    file.seek(0)
    return total


def _write_bytes_to_file(data: bytes, file) -> int:
    # 非 base64 场景（如 read_file 直接下载到的原始字节）写入文件的简单封装
    file.write(data)
    size = len(data)
    file.seek(0)
    return size


def _coerce_file_size(value: Any) -> int | None:
    # 显式排除 bool（避免 True/False 被当成 int 误用），并拒绝负数大小
    if isinstance(value, bool) or value is None:
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


async def _get_backend_file_size(backend: Any, file_path: str) -> int | None:
    # 兼容不同沙箱后端可能暴露的三种取文件大小的方法名/同步异步形式，
    # 依次尝试：异步 aget_file_size -> 同步 get_file_size -> 内部私有 _file_size；
    # 任何一种方式报错都只记调试日志继续尝试下一种，全部失败则返回 None
    # （上层据此放弃"预检查"，退化为下载后再校验实际大小）
    async_method = getattr(backend, "aget_file_size", None)
    if callable(async_method):
        try:
            return _coerce_file_size(await async_method(file_path))
        except Exception as exc:
            logger.debug("aget_file_size failed for %s: %s", file_path, exc)

    sync_method = getattr(backend, "get_file_size", None)
    if callable(sync_method):
        try:
            return _coerce_file_size(await run_blocking_io(sync_method, file_path))
        except Exception as exc:
            logger.debug("get_file_size failed for %s: %s", file_path, exc)

    private_method = getattr(backend, "_file_size", None)
    if callable(private_method):
        try:
            return _coerce_file_size(await run_blocking_io(private_method, file_path))
        except Exception as exc:
            logger.debug("_file_size failed for %s: %s", file_path, exc)

    return None


async def _json_dumps_for_tool_message(value: Any) -> str:
    # JSON 序列化丢线程池执行；default=str 兜底处理不可直接序列化的对象（如自定义类型），
    # 保证工具结果转文本这一步不会因为个别字段序列化失败而整体报错
    return await run_blocking_io(
        json.dumps,
        value,
        ensure_ascii=False,
        default=str,
    )


# ---------------------------------------------------------------------------
# MCP Quota Middleware
# ---------------------------------------------------------------------------


def _extract_mcporter_call_target(command: str) -> str | None:
    """Extract the target from a mcporter call command."""
    # 解析 shell 命令字符串，找出 "mcporter call <target>" 这种调用模式里的 target。
    # shlex.split 按 shell 语义正确处理引号/转义；命令本身写得不规范导致
    # shlex 解析失败时，退化为最朴素的空白切分，尽量还是能抓到关键 token
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for index, token in enumerate(tokens):
        if token == "mcporter" and index + 2 < len(tokens) and tokens[index + 1] == "call":
            return tokens[index + 2]
        # 命令可能是嵌套/组合形式（如整个 mcporter 调用被作为一个 token 塞进了
        # 更外层的命令里，比如通过 bash -c "..."），此时递归再解析一层
        if "mcporter call " in token:
            nested = _extract_mcporter_call_target(token)
            if nested:
                return nested
    return None


def _server_from_mcporter_target(target: str) -> str | None:
    # target 可能是 "server.tool" 或 "server:tool" 两种写法，取分隔符前的部分
    # 作为 server 名（配额是按 server 维度统计的，不区分具体调用了哪个 tool）
    for separator in (".", ":"):
        if separator in target:
            server = target.split(separator, 1)[0]
            return server or None
    return target or None


class MCPQuotaMiddleware(AgentMiddleware):
    """Enforce quotas for sandbox MCP calls routed through execute/mcporter."""

    def __init__(self, *, user_id: str | None) -> None:
        super().__init__()
        self._user_id = user_id

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        # 只关心 execute 工具调用，且入参里要有合法的 command 字符串，
        # 其它情况都不是本中间件要处理的场景，直接放行
        tool_name = request.tool_call.get("name", "")
        tool_args = request.tool_call.get("args", {})
        if tool_name != "execute" or not isinstance(tool_args, dict):
            return await handler(request)

        command = tool_args.get("command")
        if not isinstance(command, str):
            return await handler(request)

        # 识别不出 mcporter 调用目标，说明这只是一次普通 shell 命令，不需要配额检查
        target = _extract_mcporter_call_target(command)
        server_name = _server_from_mcporter_target(target) if target else None
        if not server_name:
            return await handler(request)

        from src.infra.mcp.quota import (
            check_and_consume_system_mcp_quota,
            quota_error_json,
        )

        # 检查并原子性地扣减该用户对这个 MCP server 的配额
        quota_result = await check_and_consume_system_mcp_quota(
            user_id=self._user_id,
            server_name=server_name,
        )
        if quota_result.allowed:
            return await handler(request)

        # 配额已用尽：直接短路返回错误结果，真正的 shell 命令根本不会被执行，
        # 避免绕过配额限制白白消耗 MCP 服务资源
        return ToolMessage(
            content=quota_error_json(server_name, quota_result),
            tool_call_id=request.tool_call.get("id", ""),
            name=tool_name,
        )


# ---------------------------------------------------------------------------
# Tool Result Binary Middleware
# ---------------------------------------------------------------------------


class ToolResultBinaryMiddleware(AgentMiddleware):
    """Upload base64 binary data and replace with URL before sending ToolMessage to LLM.

    Handles two scenarios:
    1. MCP tools returning image/file type base64 data → upload and replace with URL
    2. read_file tool reading binary files → download and upload to S3, return file link
    """

    def __init__(self, *, base_url: str = "") -> None:
        super().__init__()
        self._base_url = base_url

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        tool_name = request.tool_call.get("name", "")
        tool_args = request.tool_call.get("args", {})

        # --- read_file binary interception ---
        # 在真正调用 read_file 之前就判断目标文件是不是二进制类型：如果是，
        # 走专门的下载+上传流程直接短路返回，根本不让原始 read_file 尝试
        # 把二进制内容当文本读（那样只会读出乱码）
        if tool_name == "read_file":
            file_path = tool_args.get("file_path", "") if isinstance(tool_args, dict) else ""
            if file_path and self._is_binary_file(file_path):
                uploaded = await self._handle_read_file_binary(request, file_path)
                if uploaded is not None:
                    return uploaded

        result = await handler(request)

        # Only process ToolMessage results
        if not isinstance(result, ToolMessage):
            return result

        content = result.content
        if not isinstance(content, list):
            return result

        # Quick check: any base64 blocks?
        # 先做一次廉价的存在性检查，绝大多数工具结果里不会有 base64 块，
        # 这样可以快速跳过，避免每次都构建新列表的开销
        if not any(
            isinstance(b, dict) and b.get("base64") and b.get("type") in _BINARY_BLOCK_TYPES
            for b in content
        ):
            return result

        # Upload and replace base64 with URL. Return JSON text instead of a raw
        # content-block list so model providers do not parse MCP media blocks as
        # provider-native image/file blocks on the next LLM call.
        new_blocks: list[str | dict[str, Any]] = []
        uploaded_block_count = 0
        estimated_total_bytes = 0
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("base64")
                and block.get("type") in _BINARY_BLOCK_TYPES
            ):
                b64_data = block.get("base64")
                estimated_bytes = (
                    _estimated_base64_decoded_size(b64_data) if isinstance(b64_data, str) else 0
                )
                # 块数或累计字节数任一超限，就不再尝试上传，直接打上对应的
                # redact 标记，保留块本身（去掉 base64）但不再消耗上传配额
                if uploaded_block_count >= _BINARY_BLOCK_UPLOAD_MAX_BLOCKS:
                    new_blocks.append(_redact_excess_binary_block(block))
                    continue
                if estimated_total_bytes + estimated_bytes > _BINARY_BLOCK_UPLOAD_TOTAL_MAX_BYTES:
                    new_blocks.append(_redact_oversized_binary_block(block))
                    continue
                url = await self._upload_block(block)
                if url:
                    # Keep original structure, replace base64 with url
                    new_block = {k: v for k, v in block.items() if k != "base64"}
                    new_block["url"] = url
                    new_blocks.append(new_block)
                    uploaded_block_count += 1
                    estimated_total_bytes += estimated_bytes
                else:
                    new_blocks.append(_redact_failed_binary_block(block))
            else:
                new_blocks.append(block)

        return ToolMessage(
            content=await self._format_uploaded_blocks_for_llm(new_blocks),
            tool_call_id=result.tool_call_id,
            name=getattr(result, "name", None),
            status=getattr(result, "status", None),
            artifact=getattr(result, "artifact", None),
        )

    @staticmethod
    async def _format_uploaded_blocks_for_llm(blocks: list[str | dict[str, Any]]) -> str:
        # 把"文本片段 + 已上传媒体块/失败标记块"这样混杂的列表，统一整理成
        # 一个 JSON 对象：所有文本拼成一个 text 字段，非文本块收进 blocks 数组
        # （没有非文本块时干脆不带这个字段），保证最终喂给 LLM 的是结构简单、
        # 可预期的纯文本内容，而不是原始的多态内容块列表
        text_parts: list[str] = []
        media_blocks: list[dict[str, Any]] = []

        for block in blocks:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                text_parts.append(str(block))
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if text is not None:
                    text_parts.append(str(text))
                continue
            media_blocks.append(block)

        payload: dict[str, Any] = {"text": "".join(text_parts)}
        if media_blocks:
            payload["blocks"] = media_blocks
        return await _json_dumps_for_tool_message(payload)

    @staticmethod
    def _is_binary_file(file_path: str) -> bool:
        """Check if a file path has a binary extension."""
        ext = os.path.splitext(file_path)[1].lower()
        return ext in _BINARY_EXTENSIONS

    async def _handle_read_file_binary(self, request: Any, file_path: str) -> ToolMessage | None:
        """Download a binary file from the sandbox, upload to S3, return URL info."""
        # 整个方法用一个大 try/except 兜底：任何环节失败都返回 None，
        # 调用方据此退回执行原本的 read_file 逻辑，本方法纯粹是"增强"而非必需
        try:
            from src.infra.storage.s3.service import get_or_init_storage
            from src.infra.tool.backend_utils import get_backend_from_runtime

            backend = get_backend_from_runtime(request.runtime)
            if backend is None:
                return None

            # 下载前先尽量拿到文件大小做预检查，避免白白下载一个注定会
            # 因为太大而被拒绝上传的文件，节省沙箱到本进程的下载带宽
            known_size = await _get_backend_file_size(backend, file_path)
            if known_size is not None and known_size > _READ_FILE_BINARY_UPLOAD_MAX_BYTES:
                logger.warning(
                    "read_file binary upload refused oversized file before download: "
                    "%s size=%s max=%s",
                    file_path,
                    known_size,
                    _READ_FILE_BINARY_UPLOAD_MAX_BYTES,
                )
                return None

            # Download from sandbox backend
            # 同样是"异步优先、同步兜底"的后端兼容性探测，两种方式都失败则放弃
            file_bytes: bytes | None = None
            if hasattr(backend, "adownload_files"):
                try:
                    responses = await backend.adownload_files([file_path])
                    if responses and responses[0].content:
                        file_bytes = responses[0].content
                    del responses
                except Exception:
                    pass

            if file_bytes is None and hasattr(backend, "download_files"):
                try:
                    responses = await run_blocking_io(backend.download_files, [file_path])
                    if responses and responses[0].content:
                        file_bytes = responses[0].content
                    del responses
                except Exception:
                    pass

            if file_bytes is None:
                return None

            filename = file_path.rsplit("/", 1)[-1]
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            file_size = len(file_bytes)
            # 前面基于后端上报大小的预检查未必总能拿到准确值，这里用真正下载
            # 到的字节数做一次权威复核；即便已经产生了下载开销，也好过继续
            # 上传一个超限文件浪费存储资源
            if file_size > _READ_FILE_BINARY_UPLOAD_MAX_BYTES:
                logger.warning(
                    "read_file binary upload refused oversized file: %s size=%s max=%s",
                    file_path,
                    file_size,
                    _READ_FILE_BINARY_UPLOAD_MAX_BYTES,
                )
                return None

            # Upload to storage
            # SpooledTemporaryFile：小文件留在内存，超过阈值自动落盘，
            # 兼顾大文件时的内存安全与小文件时的性能
            storage = await get_or_init_storage()
            with SpooledTemporaryFile(
                max_size=_BINARY_UPLOAD_SPOOL_MEMORY_LIMIT,
                mode="w+b",
            ) as spooled:
                file_size = await run_blocking_io(_write_bytes_to_file, file_bytes, spooled)
                # 已经拷入 spooled 文件，主动释放这份可能很大的内存副本引用
                del file_bytes
                upload_result = await storage.upload_file(
                    file=spooled,
                    folder="revealed_files",
                    filename=filename,
                    content_type=mime_type,
                    # 前面已经按本场景的专属限制做过校验，跳过存储层自身的通用大小限制
                    skip_size_limit=True,
                )

            # 对外暴露的是本服务自己的下载中转接口，而不是存储后端的原始地址，
            # 便于统一做访问控制、并屏蔽底层具体用的是哪种对象存储
            base_url = self._base_url or getattr(settings, "APP_BASE_URL", "").rstrip("/")
            proxy_url = (
                f"{base_url}/api/upload/file/{upload_result.key}"
                if base_url
                else f"/api/upload/file/{upload_result.key}"
            )

            result_data = await _json_dumps_for_tool_message(
                {
                    "key": upload_result.key,
                    "url": proxy_url,
                    "name": filename,
                    "mime_type": upload_result.content_type or mime_type,
                    "size": file_size,
                    "_meta": {
                        "path": file_path,
                        "source": "read_file_binary_upload",
                    },
                },
            )

            logger.info(
                "read_file binary upload: %s → %s (%d bytes)",
                file_path,
                upload_result.key,
                file_size,
            )

            # 伪装成一次正常的 read_file 调用结果返回，LLM 侧感知不到中间发生了
            # 额外的下载/上传过程，只是看到 read_file 返回了文件的链接信息
            return ToolMessage(
                content=result_data,
                tool_call_id=request.tool_call.get("id", ""),
                name="read_file",
            )
        except Exception as e:
            logger.warning("read_file binary upload failed: %s", e)
            return None

    async def _upload_block(self, block: dict) -> str | None:
        """Upload a single binary block to storage, return the access URL."""
        b64_data = block.get("base64")
        if not b64_data or not isinstance(b64_data, str):
            return None

        # 用估算值先做一轮廉价的超限拒绝，避免为一个明显过大的块启动存储客户端
        if _estimated_base64_decoded_size(b64_data) > _BINARY_BLOCK_UPLOAD_MAX_BYTES:
            logger.warning(
                "Refusing oversized binary block upload: estimated=%s max=%s",
                _estimated_base64_decoded_size(b64_data),
                _BINARY_BLOCK_UPLOAD_MAX_BYTES,
            )
            return None

        try:
            from src.infra.storage.s3.service import get_or_init_storage

            storage = await get_or_init_storage()
        except Exception as e:
            # 存储服务初始化失败，这个块就没法上传，但不影响其它块/主流程
            logger.warning("Failed to initialize storage for binary upload: %s", e)
            return None

        try:
            # MCP 返回的内联二进制块没有"原始文件名"，只能根据 mime_type 猜一个
            # 扩展名，再拼上随机短 id 生成一个唯一文件名
            mime_type = block.get("mime_type", "application/octet-stream")
            ext = mimetypes.guess_extension(mime_type) or ".bin"
            ext = ext.lstrip(".")
            filename = f"binary_{uuid.uuid4().hex[:8]}.{ext}"

            with SpooledTemporaryFile(
                max_size=_BINARY_UPLOAD_SPOOL_MEMORY_LIMIT,
                mode="w+b",
            ) as spooled:
                # 真正解码时再次强制校验字节上限（比前面的估算值更权威），
                # 双重防线避免估算偏差导致漏判
                size = await run_blocking_io(
                    _decode_base64_to_file,
                    b64_data,
                    spooled,
                    max_bytes=_BINARY_BLOCK_UPLOAD_MAX_BYTES,
                )
                upload_result = await storage.upload_file(
                    file=spooled,
                    folder="tool_binaries",
                    filename=filename,
                    content_type=mime_type,
                    skip_size_limit=True,
                )

            base_url = self._base_url
            if not base_url:
                base_url = getattr(settings, "APP_BASE_URL", "").rstrip("/")

            url = (
                f"{base_url}/api/upload/file/{upload_result.key}"
                if base_url
                else f"/api/upload/file/{upload_result.key}"
            )
            logger.info("Middleware uploaded binary block: %s (%d bytes)", upload_result.key, size)
            return url
        except ValueError as e:
            # _decode_base64_to_file 解码过程中检测到真实超限时抛的正是这个异常，
            # 单独识别出来打一条更精确的日志；其它 ValueError 走通用失败分支
            if str(e) == "binary_upload_too_large":
                logger.warning(
                    "Refusing oversized binary block upload after decode exceeded %s bytes",
                    _BINARY_BLOCK_UPLOAD_MAX_BYTES,
                )
                return None
            logger.warning("Failed to upload binary block in middleware: %s", e)
            return None
        except Exception as e:
            logger.warning("Failed to upload binary block in middleware: %s", e)
            return None


# ---------------------------------------------------------------------------
# Deferred Tool Search Middleware
# ---------------------------------------------------------------------------


class ToolSearchMiddleware(AgentMiddleware):
    """Deferred tool loading middleware — manages on-demand MCP tool discovery and dynamic injection.

    Two core hooks:

    * ``awrap_model_call`` — before each LLM call:
      1. Injects the undiscovered deferred tool name list into the system prompt tail
      2. Injects ``search_tools`` tool + discovered tool schemas into ``request.tools``

    * ``awrap_tool_call`` — during tool execution:
      If the tool name is in the discovered set but not in the ToolNode registry,
      execute directly and return ToolMessage (factory skips validation for these tools).
    """

    def __init__(
        self,
        *,
        deferred_manager: "DeferredToolManager",
        search_limit: int = 10,
    ) -> None:
        super().__init__()
        self._deferred_manager = deferred_manager
        self._search_limit = search_limit

        # Lazy init for search_tools (avoid importing potentially missing modules in __init__)
        self._search_tool: "BaseTool | None" = None

    def _get_search_tool(self) -> "BaseTool":
        """Lazily create search_tools tool instance."""
        if self._search_tool is None:
            from src.infra.tool.tool_search_tool import ToolSearchTool

            self._search_tool = ToolSearchTool(
                manager=self._deferred_manager,
                search_limit=self._search_limit,
            )
        return self._search_tool

    @staticmethod
    def _system_message_contains_search_guide(system_message: Any) -> bool:
        # 判断系统提示里是否已经包含过"如何使用 search_tools"的引导文本，
        # 避免每一轮都重复注入同一段说明；只比较 text 类型的块，并做空白归一化
        # 处理，防止因格式细节差异（多余空格/换行）导致误判为"未包含"
        guide = _normalize_prompt_text(DEFERRED_TOOL_SEARCH_GUIDE)
        if not guide:
            return False

        text_parts: list[str] = []
        for block in _system_message_to_blocks(system_message):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text", "")
            if isinstance(text, str):
                text_parts.append(_normalize_prompt_text(text))
        return guide in "\n\n".join(text_parts)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Inject deferred tool prompt and dynamic tool schemas."""
        # 1. Inject deferred tool name list + discovered tool state (uses manager's dirty flag cache)
        # 每次调用模型前都刷新"未发现工具"清单；如果引导说明已经存在于系统提示里
        # （典型是上一轮已经注入过），这次就只追加清单部分（去掉第一段引导文本），
        # 避免同一段引导语在多轮对话里重复堆积
        prompt_sections = self._deferred_manager.get_deferred_prompt_blocks()
        if prompt_sections and self._system_message_contains_search_guide(request.system_message):
            prompt_sections = prompt_sections[1:]
        if prompt_sections:
            new_system_message = _append_system_text_blocks(request.system_message, prompt_sections)
            request = request.override(system_message=new_system_message)

        # 2. Inject search_tools itself and discovered tools (ensures sub-agents share the same dynamic loading path)
        # search_tools 本身、以及目前已经被发现的 MCP 工具，都要确保出现在
        # 本次请求的工具列表里；子 agent 会各自 fork 一份 deferred_manager，
        # 必须在每次模型调用时重新注入，不能假设它们已经全局注册好了
        search_tool = self._get_search_tool()
        discovered = self._deferred_manager.get_discovered_tools()
        existing_names = {
            t.name if hasattr(t, "name") else t.get("name", "") for t in request.tools
        }
        new_tools = []
        if search_tool.name not in existing_names:
            new_tools.append(search_tool)
        new_tools.extend(t for t in discovered if t.name not in existing_names)
        if new_tools:
            # 新工具统一排序后追加，保持工具列表顺序稳定，减少因顺序抖动导致
            # 的 prompt cache 失效
            combined = list(request.tools) + sorted(new_tools, key=_tool_sort_key)
            request = request.override(tools=combined)

        return await handler(request)

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Intercept deferred tool and search_tools calls, execute directly.

        Handles two tool types:
        1. search_tools — search and discover deferred tools (may not be registered in ToolNode)
        2. Discovered deferred MCP tools — execute directly and return ToolMessage
        """
        tool_name = request.tool_call.get("name", "")

        # Handle search_tools through this middleware even when ToolNode has a
        # registered search_tools instance. Sub-agents use forked managers, and
        # executing the registered parent tool would make the search invisible
        # to the sub-agent's next model call.
        # 始终用本中间件自己持有的 search_tool 实例执行，而不是可能存在于
        # ToolNode 里的那个（父 agent 的）实例——否则子 agent 调用 search_tools
        # 时，"发现"的结果会写进父 agent 的 manager，子 agent 自己接下来的
        # 模型调用完全看不到这次搜索的效果
        search_tool = self._get_search_tool()
        if tool_name == search_tool.name:
            try:
                args = request.tool_call.get("args", {})
                result = await search_tool.ainvoke(args)
                content = (
                    result
                    if isinstance(result, str)
                    else await _json_dumps_for_tool_message(result)
                )
                return ToolMessage(
                    content=content,
                    tool_call_id=request.tool_call.get("id", ""),
                    name=tool_name,
                )
            except Exception as e:
                # search_tools 本身只是辅助发现工具，执行失败不应该中断整个 Agent
                # 运行，转换成 error 状态的 ToolMessage 让模型知道这次搜索没成功
                logger.warning(
                    "[ToolSearchMiddleware] Error executing search_tools: %s", e, exc_info=True
                )
                return ToolMessage(
                    content=f"Error executing tool {tool_name}: {e}",
                    tool_call_id=request.tool_call.get("id", ""),
                    name=tool_name,
                    status="error",
                )

        # Check if it's a discovered deferred tool
        # request.tool is None 意味着这个工具名没有在 ToolNode 里静态注册
        # （否则框架早就把对应的 tool 对象填进 request.tool 了），说明它是一个
        # 通过 search_tools 动态发现、需要本中间件亲自执行的延迟加载工具
        if self._deferred_manager.is_discovered(tool_name) and request.tool is None:
            tool = self._deferred_manager.get_tool(tool_name)
            if tool is not None:
                try:
                    args = request.tool_call.get("args", {})
                    result = await tool.ainvoke(args)

                    # MCP tools with response_format="content_and_artifact"
                    # ainvoke() returns tuple (content, artifact), need to unpack
                    if isinstance(result, tuple) and len(result) == 2:
                        result = result[0]

                    # MCP content blocks ([{"type":"text","text":"..."}]) passed directly as list,
                    # preserving ToolMessage.content str | list[dict] format
                    if isinstance(result, list):
                        msg_content: str | list[Any] = result
                    elif isinstance(result, str):
                        msg_content = result
                    elif isinstance(result, dict):
                        msg_content = await _json_dumps_for_tool_message(result)
                    elif result is not None:
                        msg_content = str(result)
                    else:
                        msg_content = ""

                    return ToolMessage(
                        content=msg_content,
                        tool_call_id=request.tool_call.get("id", ""),
                        name=tool_name,
                    )
                except Exception as e:
                    logger.warning(
                        "[ToolSearchMiddleware] Error executing discovered tool %s: %s",
                        tool_name,
                        e,
                        exc_info=True,
                    )
                    return ToolMessage(
                        content=f"Error executing tool {tool_name}: {e}",
                        tool_call_id=request.tool_call.get("id", ""),
                        name=tool_name,
                        status="error",
                    )

        # Non-deferred tool, pass through to original handler
        # 既不是 search_tools 也不是已发现的延迟工具，说明是普通的静态注册工具，
        # 交还给正常的处理链路
        return await handler(request)

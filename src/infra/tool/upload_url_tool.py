"""
URL 文件上传到沙箱工具

下载指定 URL 的文件内容，上传到沙箱文件系统的指定路径。
仅在沙箱模式下加载。

通过 ToolRuntime 注入 backend，复用 backend_utils 获取沙箱后端。
"""
# 中文补充说明：本工具优先让"下载"这一步发生在沙箱内部（生成一段 python 脚本，
# 通过 backend.aexecute 在沙箱里直接执行 urllib 下载），这样大文件不需要先拉回
# API 进程再转发一次，节省带宽和内存。只有当 backend 不支持 execute（旧版 backend）
# 时，才降级为"API 进程下载 + aupload_files 上传"的兼容路径，且该兼容路径对文件
# 大小做了更严格的限制（_FALLBACK_UPLOAD_MAX_BYTES）。

import json
import shlex
from tempfile import SpooledTemporaryFile
from typing import Annotated, Any

import httpx
from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool, InjectedToolArg

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.tool.backend_utils import get_backend_from_runtime, get_base_url_from_runtime

logger = get_logger(__name__)

# 下载超时（秒）
_DOWNLOAD_TIMEOUT = 60

# 最大文件大小（50MB，与 S3_INTERNAL_UPLOAD_MAX_SIZE 保持一致）
_MAX_FILE_SIZE = 50 * 1024 * 1024

# Keep small downloads in memory, spill larger ones to disk while enforcing _MAX_FILE_SIZE.
# 中文：小文件保留在内存中，超过该阈值后 SpooledTemporaryFile 会自动落盘，
# 避免大文件把 API 进程内存占满
_SPOOL_MAX_MEMORY_BYTES = 2 * 1024 * 1024

# Legacy fallback backends only accept bytes via aupload_files(); keep that path small.
# 中文：走兼容路径（API 侧下载后再整体上传）时，文件必须整个读进内存再一次性上传，
# 因此这里设置一个更小的上限，避免超大文件走这条路径拖垮 API 进程
_FALLBACK_UPLOAD_MAX_BYTES = 2 * 1024 * 1024


async def _json_dumps_result(data: dict[str, Any]) -> str:
    # 统一以 JSON 字符串形式返回工具结果给 LLM
    return await run_blocking_io(json.dumps, data, ensure_ascii=False)


def _sandbox_download_command(url: str, file_path: str) -> str:
    # 中文：构造一段在沙箱内执行的 python3 -c 脚本，直接用标准库 urllib 流式下载，
    # 不依赖沙箱内是否装了 requests/httpx 等第三方库；
    # 关键点：
    #   1）边下载边累加 total 字节数，超过 max_size 时主动抛异常中止，防止把沙箱磁盘写满；
    #   2）先写入临时文件 tmp_path，成功后再用 os.replace 原子改名为目标路径，
    #      避免下载中途失败时留下一个不完整的目标文件；
    #   3）用 shlex.quote 对整段脚本做 shell 转义后拼进 python3 -c 命令，
    #      防止 url/file_path 中的特殊字符破坏命令行结构。
    script = f"""
import os
import urllib.request

url = {url!r}
file_path = {file_path!r}
max_size = {_MAX_FILE_SIZE!r}
chunk_size = 1024 * 1024

parent = os.path.dirname(file_path)
if parent:
    os.makedirs(parent, exist_ok=True)

tmp_path = file_path + ".download_tmp"
total = 0
try:
    with urllib.request.urlopen(url, timeout={_DOWNLOAD_TIMEOUT!r}) as response:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    raise RuntimeError(f"File too large: {{total}} bytes (max {{max_size}})")
                out.write(chunk)
    os.replace(tmp_path, file_path)
    print(total)
except Exception:
    try:
        os.remove(tmp_path)
    except FileNotFoundError:
        pass
    raise
"""
    return f"python3 -c {shlex.quote(script)}"


async def _execute_sandbox_download(backend, url: str, file_path: str) -> tuple[bool, str]:
    # 中文：兼容两种 backend 接口形态——优先使用异步 aexecute，
    # 没有的话降级为同步 execute（通过 run_blocking_io 放入线程池执行，避免阻塞事件循环）；
    # 两者都不支持则说明该 backend 完全不能在沙箱内执行命令，只能走 API 侧下载兼容路径
    command = _sandbox_download_command(url, file_path)
    if hasattr(backend, "aexecute"):
        result = await backend.aexecute(command)
    elif hasattr(backend, "execute"):
        result = await run_blocking_io(backend.execute, command)
    else:
        return False, "backend does not support execute"

    exit_code = getattr(result, "exit_code", 0)
    output = getattr(result, "output", "")
    if exit_code == 0:
        return True, str(output or "")
    return False, str(output or f"exit_code={exit_code}")


# 中文：这是唯一对外暴露的 LLM 工具；runtime 通过 InjectedToolArg 注入，
# 用于取出沙箱 backend 与 base_url，对 LLM 不可见
@tool
async def upload_url_to_sandbox(
    url: Annotated[str, "要下载的文件 URL"],
    file_path: Annotated[str, "沙箱内的目标文件路径（绝对路径）"],
    runtime: Annotated[ToolRuntime, InjectedToolArg],
) -> str:
    """Download a file from a URL and upload it to the sandbox filesystem.

    Use this tool to transfer external files (user uploads, web resources) into the sandbox
    so they can be accessed by shell commands and scripts.
    """
    # 目标路径必须是绝对路径，否则沙箱内脚本的 os.makedirs/os.replace 语义会不明确
    if not file_path.startswith("/"):
        return await _json_dumps_result(
            {"success": False, "error": "file_path must be an absolute path"}
        )

    # 获取 backend
    backend = get_backend_from_runtime(runtime)
    if backend is None:
        return await _json_dumps_result({"success": False, "error": "No sandbox backend available"})

    # 如果 url 是相对路径，拼接 base_url
    if url.startswith("/"):
        base_url = get_base_url_from_runtime(runtime)
        if base_url:
            url = f"{base_url}{url}"
        else:
            logger.warning("[upload_url_to_sandbox] url is relative but base_url is empty: %s", url)

    # 优先走"沙箱内直接下载"路径：省去 API 进程中转，且不受 _MAX_FILE_SIZE 之外
    # 更严格的兼容上限约束；只有该路径抛异常或返回失败时才继续走下面的兼容分支
    if hasattr(backend, "aexecute") or hasattr(backend, "execute"):
        try:
            ok, output = await _execute_sandbox_download(backend, url, file_path)
            if ok:
                logger.info(
                    "[upload_url_to_sandbox] Sandbox downloaded %s -> %s (%s)",
                    url,
                    file_path,
                    output.strip(),
                )
                return await _json_dumps_result(
                    {"success": True, "path": file_path, "source": "sandbox"}
                )
            logger.warning("[upload_url_to_sandbox] Sandbox download failed: %s", output)
        except Exception as e:
            logger.warning("[upload_url_to_sandbox] Sandbox download failed: %s", e)

    # 下载文件
    # 中文：兼容路径——在 API 进程内用 httpx 流式下载到 SpooledTemporaryFile
    # （小文件留内存、大文件自动落盘），边下载边检查两级大小上限
    content: bytes
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT) as client:
            with SpooledTemporaryFile(max_size=_SPOOL_MAX_MEMORY_BYTES, mode="w+b") as spooled:
                total_size = 0
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        total_size += len(chunk)
                        # 超过硬性总大小上限：无论如何都拒绝，防止无限下载耗尽内存/磁盘
                        if total_size > _MAX_FILE_SIZE:
                            return await _json_dumps_result(
                                {
                                    "success": False,
                                    "error": (
                                        f"File too large: {total_size} bytes (max {_MAX_FILE_SIZE})"
                                    ),
                                }
                            )
                        # 超过兼容路径专用的更小上限：提示应改用支持沙箱内下载的 backend
                        if total_size > _FALLBACK_UPLOAD_MAX_BYTES:
                            return await _json_dumps_result(
                                {
                                    "success": False,
                                    "error": (
                                        "File too large for API-side fallback upload; "
                                        "use a backend with sandbox-side download support"
                                    ),
                                }
                            )
                        await run_blocking_io(spooled.write, chunk)
                await run_blocking_io(spooled.seek, 0)
                content = await run_blocking_io(spooled.read)
    except httpx.HTTPStatusError as e:
        logger.warning(f"[upload_url_to_sandbox] HTTP error downloading {url}: {e}")
        return await _json_dumps_result(
            {
                "success": False,
                "error": f"Download failed: HTTP {e.response.status_code}",
            }
        )
    except Exception as e:
        logger.warning(f"[upload_url_to_sandbox] Failed to download {url}: {e}")
        return await _json_dumps_result({"success": False, "error": f"Download failed: {e}"})

    # 上传到沙箱
    # 中文：兼容路径下载完成后，通过 backend.aupload_files 把整块字节内容一次性上传，
    # 这是传统 backend 都支持的最基础接口
    try:
        results = await backend.aupload_files([(file_path, content)])
        result = results[0]
        if result.error:
            return await _json_dumps_result(
                {
                    "success": False,
                    "error": f"Upload failed: {result.error}",
                    "path": file_path,
                }
            )
        logger.info(f"[upload_url_to_sandbox] Uploaded {url} -> {file_path} ({len(content)} bytes)")
        return await _json_dumps_result({"success": True, "path": file_path, "size": len(content)})
    except Exception as e:
        logger.error(f"[upload_url_to_sandbox] Failed to upload to {file_path}: {e}")
        return await _json_dumps_result({"success": False, "error": f"Upload failed: {e}"})


def get_upload_url_tool() -> BaseTool:
    """获取 upload_url_to_sandbox 工具实例"""
    # 中文：供 agent 构建工具集时按需注册（仅在沙箱模式下加载本工具）
    return upload_url_to_sandbox

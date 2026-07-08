"""
Transfer File / Transfer Path 工具

在不同 backend 之间双向转移文本文件（sandbox、skills store、memory store 等）。
仅支持文本文件，不支持二进制文件。
通过 CompositeBackend 的路径前缀路由自动选择源/目标 backend：
  /skills/*  → SkillsStoreBackend (MongoDB)
  /memories/* → StoreBackend (DB)
  其他       → Sandbox (Daytona/E2B) 或 StoreBackend

支持双向传输：
  - sandbox → /skills/、/memories/ 等
  - /skills/ → sandbox
  - 任意两个不同 backend 之间

安全措施：
- 路径穿越防护（.. 规范化检查）
- 文件类型限制（扩展名黑名单 + null 字节检测）
- 文件大小限制（单文件 1MB，批量 10MB）
- 目录深度/文件数限制（深度 5 层，1000 文件）
"""
# 中文补充说明：本模块的核心难点在于"跨 backend 抽象"——沙箱 backend、
# 技能存储 backend、记忆存储 backend 的底层实现完全不同（有的是文件系统，
# 有的是 MongoDB），却要用同一套 transfer_file/transfer_path 逻辑操作，
# 因此大量使用 hasattr() 探测 + 多种方法名回退（aupload_files/upload_files、
# aget_file_size/get_file_size/_file_size 等）来兼容不同 backend 的接口形态。
# 具体的源/目标 backend 由 CompositeBackend 依据路径前缀自动路由决定，
# 本模块不关心具体路由到了哪个 backend，只通过统一接口读写。

import inspect
import json
import os
from typing import Annotated, Any, Optional

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.tool.backend_utils import get_backend_from_runtime

# 二进制文件扩展名黑名单
# 中文：采用黑名单而非白名单——本工具面向"任意文本文件"（代码、配置、文档等
# 扩展名多种多样，无法枚举），因此改为枚举明确的二进制类型加以排除，
# 配合 _is_text_content 的 null 字节检测做双重兜底
BINARY_EXTENSIONS = frozenset(
    {
        # 图片
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".svg",
        ".tiff",
        ".avif",
        # 视频
        ".mp4",
        ".avi",
        ".mov",
        ".mkv",
        ".webm",
        ".flv",
        ".wmv",
        ".m4v",
        # 音频
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".aac",
        ".m4a",
        ".wma",
        # 压缩包
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".tgz",
        # 二进制/可执行
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".wasm",
        ".o",
        ".a",
        ".lib",
        # 文档二进制
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        # 数据库
        ".db",
        ".sqlite",
        ".sqlite3",
        # 字体
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".eot",
        # 其他
        ".pyc",
        ".pyo",
        ".class",
        ".jar",
        ".parquet",
        ".arrow",
        ".feather",
    }
)

logger = get_logger(__name__)


async def _json_dumps_result(data: dict[str, Any]) -> str:
    # 统一以 JSON 字符串形式返回工具结果给 LLM
    return await run_blocking_io(json.dumps, data, ensure_ascii=False)


# ==========================================
# 安全常量
# ==========================================

# 单文件大小上限 (10MB)
MAX_FILE_SIZE = 10 * 1024 * 1024
# 批量传输总大小上限 (100MB)
MAX_BATCH_SIZE = 100 * 1024 * 1024
# 目录递归最大深度
MAX_RECURSION_DEPTH = 5
# 批量传输最大文件数
MAX_BATCH_FILES = 500
# 工具响应中最多返回的逐文件明细数，避免大批量传输把 LLM 消息体撑爆。
TRANSFER_PATH_RESULT_FILE_LIMIT = 100


# ==========================================
# 安全工具函数
# ==========================================


def _is_binary_file(filename: str) -> bool:
    """根据扩展名判断是否为二进制文件"""
    _, ext = os.path.splitext(filename.lower())
    return ext in BINARY_EXTENSIONS


def _is_text_content(data: bytes) -> bool:
    """检测内容是否为文本（检查前 8KB 是否包含 null 字节）"""
    chunk = data[:8192]
    return b"\x00" not in chunk


def _check_path_traversal(path: str) -> Optional[str]:
    """检查路径是否存在穿越攻击（.. 组件）。

    Returns:
        错误信息字符串，或 None（路径安全）
    """
    # 规范化路径
    normalized = os.path.normpath(path)
    # 规范化后的路径不应包含 .. 段（normpath 会解析 .. 但保留开头 ../）
    if ".." in normalized.split(os.sep):
        return f"path traversal detected: {path}"
    return None


def _check_file_size(content: bytes, filename: str) -> Optional[str]:
    """检查文件大小是否超限。

    Returns:
        错误信息字符串，或 None（大小合法）
    """
    if len(content) > MAX_FILE_SIZE:
        return f"file too large: {filename} ({len(content)} bytes, limit {MAX_FILE_SIZE} bytes)"
    return None


def _check_known_file_size(size: int | None, filename: str) -> Optional[str]:
    """检查已知文件大小是否超限，避免先下载大文件再拒绝。"""
    if size is not None and size > MAX_FILE_SIZE:
        return f"file too large: {filename} ({size} bytes, limit {MAX_FILE_SIZE} bytes)"
    return None


def _validate_text_file(filename: str, content: bytes) -> Optional[str]:
    """综合校验文件类型和内容。

    Returns:
        错误信息字符串，或 None（校验通过）
    """
    if _is_binary_file(filename):
        return f"binary files are not supported: {filename}"
    if not _is_text_content(content):
        return f"file appears to be binary (contains null bytes): {filename}"
    size_err = _check_file_size(content, filename)
    if size_err:
        return size_err
    return None


def _append_transfer_result(
    results: list[dict[str, Any]],
    result: dict[str, Any],
    omitted_count: int,
) -> int:
    # 中文：批量传输时逐文件明细可能非常多，超过 TRANSFER_PATH_RESULT_FILE_LIMIT
    # 后不再追加进 results（避免把 LLM 消息体撑爆），只累加 omitted_count 计数，
    # 让调用方能在汇总里看到"还省略了多少条明细"
    if len(results) < TRANSFER_PATH_RESULT_FILE_LIMIT:
        results.append(result)
        return omitted_count
    return omitted_count + 1


def _entry_path(entry: Any) -> str | None:
    # 中文：ls 返回的 entry 可能是 dict，也可能是某个 SDK 的对象实例，
    # 这里统一按两种形态尝试取 path 字段，兼容不同 backend 的返回类型
    if isinstance(entry, dict):
        path = entry.get("path")
    else:
        path = getattr(entry, "path", None)
    return path if isinstance(path, str) else None


def _entry_is_dir(entry: Any) -> bool:
    # 同 _entry_path，兼容 dict / 对象两种 entry 形态
    if isinstance(entry, dict):
        return bool(entry.get("is_dir"))
    return bool(getattr(entry, "is_dir", False))


def _entry_size(entry: Any) -> int | None:
    # 同上，且对无法转换为 int 的脏数据做兜底，返回 None 表示"大小未知"
    value = entry.get("size") if isinstance(entry, dict) else getattr(entry, "size", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _get_backend_file_size(backend: Any, file_path: str) -> int | None:
    """Best-effort size preflight across sandbox/store backends."""
    # 中文：不同 backend 暴露的"取文件大小"接口名称不统一，
    # 依次尝试 aget_file_size（异步）、get_file_size（同步，需线程池执行）、
    # _file_size（内部方法兜底）；任意一种失败都只记录 debug 日志、继续尝试下一种，
    # 全部失败则返回 None（调用方会按"大小未知"处理，不会因此中断传输）
    async_method = getattr(backend, "aget_file_size", None)
    if callable(async_method):
        try:
            size = async_method(file_path)
            if inspect.isawaitable(size):
                size = await size
            return int(size) if size is not None else None
        except Exception as e:
            logger.debug(f"[transfer_file] aget_file_size failed for {file_path}: {e}")

    sync_method = getattr(backend, "get_file_size", None)
    if callable(sync_method):
        try:
            size = await run_blocking_io(sync_method, file_path)
            return int(size) if size is not None else None
        except Exception as e:
            logger.debug(f"[transfer_file] get_file_size failed for {file_path}: {e}")

    private_method = getattr(backend, "_file_size", None)
    if callable(private_method):
        try:
            size = await run_blocking_io(private_method, file_path)
            return int(size) if size is not None else None
        except Exception as e:
            logger.debug(f"[transfer_file] _file_size failed for {file_path}: {e}")

    return None


async def _download_from_backend(backend: Any, file_path: str) -> Optional[bytes]:
    """从 backend 下载文件内容"""
    # 中文：优先尝试异步接口 adownload_files，不存在或调用失败再尝试同步接口
    # download_files（通过线程池执行，避免阻塞事件循环）；两者都失败/都没有
    # 则返回 None，交由调用方判定为"文件不存在或为空"
    if hasattr(backend, "adownload_files"):
        try:
            responses = await backend.adownload_files([file_path])
            if responses:
                resp = responses[0]
                if resp.content:
                    return resp.content
                if resp.error:
                    logger.warning(f"[transfer_file] Download error for {file_path}: {resp.error}")
        except Exception as e:
            logger.warning(f"[transfer_file] adownload_files failed for {file_path}: {e}")

    if hasattr(backend, "download_files"):
        try:
            responses = await run_blocking_io(backend.download_files, [file_path])
            if responses:
                resp = responses[0]
                if resp.content:
                    return resp.content
                if resp.error:
                    logger.warning(f"[transfer_file] Download error for {file_path}: {resp.error}")
        except Exception as e:
            logger.warning(f"[transfer_file] download_files failed for {file_path}: {e}")

    return None


async def _upload_to_backend(backend: Any, target_path: str, content: bytes) -> Optional[str]:
    """上传文件到 backend，返回错误信息或 None"""
    # 中文：与下载逻辑对称，优先异步 aupload_files，回退同步 upload_files；
    # 返回值语义是"错误信息字符串"或 None（表示成功），而不是布尔值，
    # 便于调用方直接把具体错误原因回传给 LLM
    if hasattr(backend, "aupload_files"):
        try:
            responses = await backend.aupload_files([(target_path, content)])
            if responses:
                resp = responses[0]
                if resp.error:
                    return str(resp.error)
                return None
        except Exception as e:
            return str(e)

    if hasattr(backend, "upload_files"):
        try:
            responses = await run_blocking_io(backend.upload_files, [(target_path, content)])
            if responses:
                resp = responses[0]
                if resp.error:
                    return str(resp.error)
                return None
        except Exception as e:
            return str(e)

    return "backend does not support upload_files"


@tool
async def transfer_file(
    source_path: Annotated[
        str,
        "源文件路径。路径前缀决定源 backend：/skills/* → 技能存储, 其他 → 沙箱/工作区。跨会话语义记忆请使用 memory_* 工具。",
    ],
    target_path: Annotated[
        str,
        "目标文件路径。路径前缀决定目标 backend：/skills/* → 技能存储, 其他 → 沙箱/工作区。跨会话语义记忆请使用 memory_* 工具。",
    ],
    runtime: ToolRuntime = None,  # type: ignore[assignment]
) -> str:
    """
    在不同 backend 之间转移文本文件

    仅支持文本文件（代码、配置、Markdown 等），不支持二进制文件（图片、视频、压缩包等）。
    通过路径前缀自动路由到对应的存储后端：
    - /skills/* 路由到技能存储 (MongoDB)
    - 其他路径路由到沙箱 (Daytona/E2B) 或持久化存储

    常见用途：
    - 从沙箱转移生成的代码到技能目录
    - 从技能目录复制文件到沙箱工作区

    Args:
        source_path: 源文件路径（路径前缀决定源 backend）
        target_path: 目标文件路径（路径前缀决定目标 backend）

    Returns:
        JSON 格式的操作结果
    """
    backend = get_backend_from_runtime(runtime)

    if backend is None:
        return await _json_dumps_result({"success": False, "error": "backend not available"})

    # 1. 路径安全检查
    for label, path in [("source", source_path), ("target", target_path)]:
        traversal_err = _check_path_traversal(path)
        if traversal_err:
            return await _json_dumps_result({"success": False, "error": f"{label} {traversal_err}"})

    # 2. 下载
    filename = source_path.split("/")[-1]
    # 先做一次"预检查"：如果 backend 能提前告知文件大小，就在真正下载前拦截超大文件，
    # 避免浪费带宽/内存去下载一个注定会被拒绝的文件
    known_size = await _get_backend_file_size(backend, source_path)
    size_err = _check_known_file_size(known_size, filename)
    if size_err:
        return await _json_dumps_result(
            {
                "success": False,
                "error": size_err,
                "source": source_path,
            }
        )

    content = await _download_from_backend(backend, source_path)
    if content is None:
        return await _json_dumps_result(
            {
                "success": False,
                "error": f"file not found or empty: {source_path}",
                "source": source_path,
            }
        )

    # 3. 文件类型 + 大小校验
    # 拿到真实内容后再做一次完整校验（二进制扩展名/null 字节/真实大小），
    # 因为预检查阶段的 known_size 可能不可用或不准确
    validation_err = _validate_text_file(filename, content)
    if validation_err:
        return await _json_dumps_result(
            {
                "success": False,
                "error": validation_err,
                "source": source_path,
            }
        )

    # 4. 上传
    upload_error = await _upload_to_backend(backend, target_path, content)
    if upload_error:
        return await _json_dumps_result(
            {
                "success": False,
                "error": upload_error,
                "source": source_path,
                "target": target_path,
            }
        )

    logger.info(
        f"[transfer_file] Transferred {source_path} -> {target_path} ({len(content)} bytes)"
    )

    return await _json_dumps_result(
        {
            "success": True,
            "source": source_path,
            "target": target_path,
            "size": len(content),
        }
    )


def get_transfer_file_tool() -> BaseTool:
    """获取 transfer_file 工具实例"""
    return transfer_file


# ==========================================
# Transfer Path — 批量目录传输
# ==========================================


async def _list_dir_files(
    backend: Any,
    dir_path: str,
    *,
    limit: int | None = None,
) -> list[tuple[str, int | None]]:
    """列出目录下所有文件路径（通过 ls 递归）。

    Returns:
        文件路径列表（相对/绝对路径，取决于 backend 返回格式）
    """
    all_files: list[tuple[str, int | None]] = []
    visited_dirs: set[str] = set()

    async def _recurse(current_dir: str, depth: int) -> None:
        if limit is not None and len(all_files) > limit:
            return
        if depth > MAX_RECURSION_DEPTH:
            return
        # 中文：用 visited_dirs 记录已访问过的目录，防止软链接等造成的循环引用
        # 导致无限递归
        if current_dir in visited_dirs:
            return
        visited_dirs.add(current_dir)

        try:
            # 同样兼容异步/同步两种 ls 接口
            if hasattr(backend, "als"):
                result = await backend.als(current_dir)
                entries = result.entries or []
            elif hasattr(backend, "ls"):
                result = await run_blocking_io(backend.ls, current_dir)
                entries = result.entries or []
            else:
                return
        except Exception as e:
            logger.warning(f"[transfer_path] ls failed for {current_dir}: {e}")
            return

        for entry in entries:
            if limit is not None and len(all_files) > limit:
                return
            path = _entry_path(entry)
            if path is None:
                continue
            if _entry_is_dir(entry):
                # 子目录递归遍历，深度 +1
                await _recurse(path, depth + 1)
            else:
                all_files.append((path, _entry_size(entry)))
                if limit is not None and len(all_files) > limit:
                    return

    await _recurse(dir_path, 0)
    return all_files


@tool
async def transfer_path(
    source_dir: Annotated[
        str,
        "源目录路径（如当前 session 工作区下的 my-project/ 或 /skills/MySkill/）。路径前缀决定源 backend：/skills/* → 技能存储, 其他 → 沙箱。",
    ],
    target_prefix: Annotated[
        str,
        "目标路径前缀。默认 /skills/，会将源目录下所有文件传输到 skills 数据库，目录名作为 skill 名称。也可指定其他沙箱/工作区路径。",
    ] = "/skills/",
    runtime: ToolRuntime = None,  # type: ignore[assignment]
) -> str:
    """
    批量传输目录下所有文本文件到目标 backend（双向）

    在任意两个 backend 之间批量传输目录文件：
    - 沙箱 → /skills/ (批量创建 skill)
    - /skills/ → 沙箱 (将 skill 文件复制到工作区)

    目录名自动作为目标子路径名称（如 /skills/Foo/ → 当前 session 工作区下的 Foo/）。

    安全限制：
    - 仅支持文本文件，不支持二进制文件
    - 单文件上限 1MB，总大小上限 10MB
    - 递归深度最大 5 层，最多 500 个文件
    - 禁止路径穿越（..）

    常见用途：
    - 从沙箱目录批量创建 skill（如当前 session 工作区下的 my-skill/ → /skills/my-skill/）
    - 将 skill 文件批量复制到沙箱工作区（如 /skills/MySkill/ → 当前 session 工作区下的 MySkill/）

    Args:
        source_dir: 源目录路径
        target_prefix: 目标路径前缀（默认 /skills/）

    Returns:
        JSON 格式的操作结果，包含每个文件的传输状态
    """
    backend = get_backend_from_runtime(runtime)

    if backend is None:
        return await _json_dumps_result({"success": False, "error": "backend not available"})

    # 1. 路径安全检查
    for label, path in [("source_dir", source_dir), ("target_prefix", target_prefix)]:
        traversal_err = _check_path_traversal(path)
        if traversal_err:
            return await _json_dumps_result({"success": False, "error": f"{label} {traversal_err}"})

    # 确保 target_prefix 以 / 结尾
    if not target_prefix.endswith("/"):
        target_prefix += "/"

    # 防止同源传输（不能从 skills 传到 skills）
    # 中文：/skills/ 和 /memories/ 各自路由到同一个 backend 实例，
    # 若源和目标都落在同一个前缀下，等价于"从同一个存储读出再写回自己"，
    # 没有实际意义且容易误操作覆盖数据，因此直接拒绝
    if source_dir.startswith("/skills/") and target_prefix.startswith("/skills/"):
        return await _json_dumps_result(
            {
                "success": False,
                "error": "source and target cannot both be /skills/ (same backend)",
            }
        )
    if source_dir.startswith("/memories/") and target_prefix.startswith("/memories/"):
        return await _json_dumps_result(
            {
                "success": False,
                "error": "source and target cannot both be /memories/ (same backend)",
            }
        )

    # 2. 从 source_dir 提取目录名作为目标子路径
    dir_name = source_dir.rstrip("/").rsplit("/", 1)[-1]

    # 清洗 skill name（当目标是 /skills/ 时）
    # 中文：目标是技能存储时，目录名会被当作 skill 名称，需要用统一的
    # sanitize_skill_name 规则清洗（去除非法字符等），保证与其它创建 skill
    # 的入口（如技能编辑器）遵循同一套命名规范
    if target_prefix == "/skills/":
        from src.infra.skill.parser import sanitize_skill_name

        dir_name = sanitize_skill_name(dir_name)

    target_base = f"{target_prefix}{dir_name}"

    # 3. 列出源目录下所有文件
    file_paths = await _list_dir_files(backend, source_dir, limit=MAX_BATCH_FILES)

    if not file_paths:
        return await _json_dumps_result(
            {
                "success": True,
                "message": f"no files found in {source_dir}",
                "source_dir": source_dir,
                "target": target_base + "/",
                "transferred": 0,
                "skipped": 0,
                "failed": 0,
            }
        )

    # 文件数限制
    if len(file_paths) > MAX_BATCH_FILES:
        return await _json_dumps_result(
            {
                "success": False,
                "error": f"too many files: {len(file_paths)} (limit {MAX_BATCH_FILES})",
                "source_dir": source_dir,
            }
        )

    # 4. 逐个传输
    results: list[dict[str, Any]] = []
    total_size = 0
    transferred = 0
    skipped = 0
    failed = 0
    files_omitted = 0

    for file_path, known_size in file_paths:
        filename = file_path.rsplit("/", 1)[-1]

        # 计算相对路径，映射到目标
        # 中文：把 source_dir 前缀从完整路径中剥离，只保留相对于源目录的部分，
        # 再拼到 target_base 下，这样可以保留原有的多级子目录结构
        rel_path = file_path
        source_dir_stripped = source_dir.rstrip("/")
        if file_path.startswith(source_dir_stripped):
            rel_path = file_path[len(source_dir_stripped) :].lstrip("/")
        target_path = f"{target_base}/{rel_path}" if rel_path else f"{target_base}/{filename}"

        size_err = _check_known_file_size(known_size, filename)
        if size_err:
            files_omitted = _append_transfer_result(
                results,
                {"file": file_path, "status": "skipped", "error": size_err},
                files_omitted,
            )
            skipped += 1
            continue
        if known_size is not None and total_size + known_size > MAX_BATCH_SIZE:
            # 中文：这里用"预知大小"提前拦截，避免为了判断而白白下载一个必然会超限的文件；
            # 下面下载完成后还会用真实字节数再做一次兜底检查（known_size 可能拿不到或不准）
            files_omitted = _append_transfer_result(
                results,
                {
                    "file": file_path,
                    "status": "skipped",
                    "error": (
                        f"batch size limit exceeded ({total_size + known_size} > {MAX_BATCH_SIZE})"
                    ),
                },
                files_omitted,
            )
            skipped += 1
            continue

        # 下载
        try:
            content = await _download_from_backend(backend, file_path)
        except Exception as e:
            logger.warning(f"[transfer_path] Download failed for {file_path}: {e}")
            files_omitted = _append_transfer_result(
                results,
                {"file": file_path, "status": "failed", "error": str(e)},
                files_omitted,
            )
            failed += 1
            continue

        if content is None:
            files_omitted = _append_transfer_result(
                results,
                {"file": file_path, "status": "skipped", "error": "file not found or empty"},
                files_omitted,
            )
            skipped += 1
            continue

        # 文件校验
        validation_err = _validate_text_file(filename, content)
        if validation_err:
            files_omitted = _append_transfer_result(
                results,
                {"file": file_path, "status": "skipped", "error": validation_err},
                files_omitted,
            )
            skipped += 1
            continue

        # 总大小检查
        total_size += len(content)
        if total_size > MAX_BATCH_SIZE:
            files_omitted = _append_transfer_result(
                results,
                {
                    "file": file_path,
                    "status": "skipped",
                    "error": f"batch size limit exceeded ({total_size} > {MAX_BATCH_SIZE})",
                },
                files_omitted,
            )
            skipped += 1
            continue

        # 上传
        upload_err = await _upload_to_backend(backend, target_path, content)
        if upload_err:
            files_omitted = _append_transfer_result(
                results,
                {"file": file_path, "status": "failed", "error": upload_err},
                files_omitted,
            )
            failed += 1
        else:
            files_omitted = _append_transfer_result(
                results,
                {
                    "file": file_path,
                    "status": "transferred",
                    "target": target_path,
                    "size": len(content),
                },
                files_omitted,
            )
            transferred += 1

    logger.info(
        f"[transfer_path] {source_dir} -> {target_base}/ "
        f"(transferred={transferred}, skipped={skipped}, failed={failed}, "
        f"total_size={total_size})"
    )

    return await _json_dumps_result(
        {
            "success": failed == 0,
            "source_dir": source_dir,
            "target": target_base + "/",
            "transferred": transferred,
            "skipped": skipped,
            "failed": failed,
            "total_size": total_size,
            "files": results,
            "files_omitted": files_omitted,
        }
    )


def get_transfer_path_tool() -> BaseTool:
    """获取 transfer_path 工具实例"""
    return transfer_path

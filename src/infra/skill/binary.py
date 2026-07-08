"""
Skill binary file utilities

Handles detection, storage reference building, and parsing of binary files in skills.
Binary files are stored in S3/local storage, with a JSON reference in MongoDB.
"""

import json
import mimetypes
from typing import Optional

from pydantic import BaseModel, Field

from src.infra.async_utils import run_blocking_io

# Known binary file extensions (files that should go to S3, not MongoDB text storage)
# 已知二进制扩展名白名单：这类文件走 S3/本地对象存储，MongoDB 仅存一个 JSON 引用
BINARY_EXTENSIONS: set[str] = {
    # Images
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "bmp",
    "ico",
    "tiff",
    "tif",
    # Video
    "mp4",
    "webm",
    "mov",
    "avi",
    "mkv",
    "wmv",
    "flv",
    # Audio
    "mp3",
    "wav",
    "ogg",
    "aac",
    "flac",
    "m4a",
    "wma",
    # Binary documents
    "pdf",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    # Archives
    "zip",
    "tar",
    "gz",
    "bz2",
    "7z",
    "rar",
    # Fonts
    "woff",
    "woff2",
    "ttf",
    "eot",
    "otf",
    # Other binary
    "exe",
    "dll",
    "so",
    "dylib",
    "bin",
    "dat",
}

# Marker to identify binary references in MongoDB content field
# content 字段中出现此标记即表示存的是二进制引用而非真实文本，用于快速预判
BINARY_REF_MARKER = '"_binary_ref": true'


class SkillBinaryRef(BaseModel):
    """Binary file reference stored in MongoDB content field"""

    # populate_by_name 允许用字段名或别名(_binary_ref)两种方式赋值
    model_config = {"populate_by_name": True}

    # 固定为 True 的标记位；storage_key 指向对象存储中的实际文件
    binary_ref: bool = Field(default=True, alias="_binary_ref")
    storage_key: str  # S3/local storage key
    mime_type: str
    size: int


def is_binary_file(file_path: str, data: Optional[bytes] = None) -> bool:
    """
    Determine if a file should be treated as binary.

    Strategy:
    1. Check known binary extensions → binary
    2. If data provided, try UTF-8 decode → binary if fails
    3. Default to text
    """
    # 提取小写扩展名（无扩展名则为空串）
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""

    # Known binary extensions
    # 命中二进制白名单直接判定为二进制
    if ext in BINARY_EXTENSIONS:
        return True

    # Known text extensions — definitely not binary
    # 命中文本白名单直接判定为文本，避免误判
    text_exts = {
        "md",
        "txt",
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "json",
        "yaml",
        "yml",
        "toml",
        "xml",
        "csv",
        "html",
        "htm",
        "css",
        "scss",
        "less",
        "sh",
        "bat",
        "ps1",
        "sql",
        "rb",
        "go",
        "rs",
        "java",
        "c",
        "cpp",
        "h",
        "hpp",
        "cs",
        "php",
        "swift",
        "kt",
        "scala",
        "r",
        "lua",
        "pl",
        "ex",
        "exs",
        "erl",
        "clj",
        "hs",
        "ml",
        "vim",
        "el",
        "lisp",
        "cfg",
        "ini",
        "conf",
        "env",
        "gitignore",
        "dockerignore",
        "dockerfile",
        "makefile",
        "cmake",
        "gradle",
    }
    if ext in text_exts:
        return False

    # Unknown extension — try to decode if data available
    # 未知扩展名：若有内容，用启发式判断
    if data is not None:
        # Quick check: null bytes almost always indicate binary
        # 前 8KB 内出现空字节几乎可确定是二进制
        if b"\x00" in data[:8192]:
            return True
        try:
            # 能按 UTF-8 成功解码则视为文本
            data.decode("utf-8")
            return False
        except (UnicodeDecodeError, ValueError):
            return True

    # Default: treat unknown extensions without data as text
    # 无内容可判断的未知扩展名，默认按文本处理
    return False


def build_storage_key(user_id: str, skill_name: str, file_path: str) -> str:
    """Build S3/local storage key for a skill binary file."""
    # 对象存储 key 规则：按 用户/技能/文件路径 组织，保证隔离且可定位
    return f"skills/{user_id}/{skill_name}/{file_path}"


def build_binary_ref_content(storage_key: str, mime_type: str, size: int) -> str:
    """
    Build JSON string to store in MongoDB content field for a binary file.
    """
    # 构造并序列化二进制引用对象；by_alias=True 输出 _binary_ref 键
    ref = SkillBinaryRef(
        storage_key=storage_key,
        mime_type=mime_type,
        size=size,
    )
    return json.dumps(ref.model_dump(by_alias=True))


def parse_binary_ref(content: str) -> Optional[SkillBinaryRef]:
    """
    Detect and parse a binary file reference from MongoDB content.
    Returns None if content is not a binary reference (i.e., it's regular text).
    """
    # 无标记则一定是普通文本，快速返回，避免无谓的 JSON 解析
    if not content or BINARY_REF_MARKER not in content:
        return None
    try:
        data = json.loads(content)
        # 二次确认 _binary_ref 为 True 才当作引用
        if data.get("_binary_ref") is True:
            return SkillBinaryRef.model_validate(data)
    except (json.JSONDecodeError, Exception):
        # 解析失败则按普通文本对待
        pass
    return None


async def parse_binary_ref_async(content: str) -> Optional[SkillBinaryRef]:
    """Detect and parse a binary reference off the event loop."""
    # 大内容解析放线程池，避免阻塞事件循环
    return await run_blocking_io(parse_binary_ref, content)


def guess_mime_type(filename: str) -> str:
    """Guess MIME type from filename."""
    # 依据扩展名猜测 MIME 类型，无法识别时回退为通用二进制流
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"

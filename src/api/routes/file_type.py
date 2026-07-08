"""File type classification utilities"""

# 文件类型分类工具模块：根据文件名后缀与 MIME 类型，将上传文件归类为
# 图片 / 视频 / 音频 / 文档 / 未知（FileCategory），并提供各类别对应的
# 扩展名白名单与所需上传权限。被 upload.py 等上传接口用于类型校验与权限判定。
from enum import Enum
from typing import Optional


# 文件类别枚举：同时继承 str，便于直接与字符串比较/序列化（枚举值即 "image" 等）。
# 用作分类结果、权限映射与大小限制表的键。
class FileCategory(str, Enum):
    """File category enum"""

    # 图片
    IMAGE = "image"
    # 视频
    VIDEO = "video"
    # 音频
    AUDIO = "audio"
    # 文档（广义，涵盖代码、配置、压缩包等，详见 FILE_EXTENSIONS）
    DOCUMENT = "document"
    # 未知类型：MIME 与扩展名都无法识别时的兜底类别
    UNKNOWN = "unknown"


# File extension mappings
# 各文件类别允许的扩展名白名单（小写、不含点）。上传接口据此校验扩展名合法性；
# 文档类别刻意涵盖大量文本/代码/脚本/配置/压缩包扩展名，以支持代码与文档上传。
FILE_EXTENSIONS: dict[FileCategory, set[str]] = {
    FileCategory.IMAGE: {"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "ico"},
    FileCategory.VIDEO: {"mp4", "webm", "mov", "avi", "mkv", "wmv", "flv"},
    FileCategory.AUDIO: {"mp3", "wav", "ogg", "aac", "flac", "m4a", "wma"},
    FileCategory.DOCUMENT: {
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "txt",
        "md",
        "csv",
        "rtf",
        "odt",
        "ods",
        "odp",
        "epub",
        "json",
        "xml",
        "html",
        "htm",
        "dxf",
        "dwg",
        "log",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "tex",
        "diff",
        "patch",
        # Code / scripts
        "py",
        "js",
        "ts",
        "jsx",
        "tsx",
        "vue",
        "svelte",
        "go",
        "rs",
        "rb",
        "php",
        "java",
        "c",
        "cpp",
        "h",
        "cs",
        "swift",
        "kt",
        "scala",
        "dart",
        "lua",
        "r",
        "pl",
        "sql",
        "sh",
        "bash",
        "zsh",
        "fish",
        "ps1",
        "bat",
        "cmd",
        # Config / project files
        "properties",
        "gradle",
        "cmake",
        "env",
        "graphql",
        "proto",
        # Archives
        "zip",
        "rar",
        "7z",
        "tar",
        "gz",
        "bz2",
        "xz",
        "tgz",
    },
}

# MIME type prefixes
# MIME 类型前缀映射：MIME 以对应前缀开头即归入该类别（如 image/png → IMAGE）。
# 仅图片/视频/音频用前缀匹配；文档类 MIME 在 get_file_category 中单独判定。
MIME_TYPE_PREFIXES: dict[FileCategory, str] = {
    FileCategory.IMAGE: "image/",
    FileCategory.VIDEO: "video/",
    FileCategory.AUDIO: "audio/",
}


def get_file_category(filename: str, mime_type: Optional[str] = None) -> FileCategory:
    """
    Determine file category from filename and MIME type

    Args:
        filename: Original filename
        mime_type: Optional MIME type from upload

    Returns:
        FileCategory enum value
    """
    # Try MIME type first
    # 优先按 MIME 前缀判定图片/视频/音频（比扩展名更可靠）
    if mime_type:
        for category, prefix in MIME_TYPE_PREFIXES.items():
            if mime_type.startswith(prefix):
                return category
        # Handle specific MIME types
        # 特殊处理：PDF 归为文档
        if mime_type == "application/pdf":
            return FileCategory.DOCUMENT
        # Office 文档（msword 及 application/vnd.* 系列）归为文档
        if mime_type.startswith("application/msword") or mime_type.startswith("application/vnd."):
            return FileCategory.DOCUMENT

    # Fall back to extension
    # MIME 未命中时，回退到按扩展名匹配各类别白名单
    ext = filename.lower().split(".")[-1] if "." in filename else ""
    for category, extensions in FILE_EXTENSIONS.items():
        if ext in extensions:
            return category

    # 两种方式都无法识别时，归为未知类别
    return FileCategory.UNKNOWN


def get_permission_for_category(category: FileCategory) -> Optional[str]:
    """Get permission required for a file category"""
    # 文件类别到所需上传权限的映射；UNKNOWN 不在表中，返回 None（无专属权限）
    mapping = {
        FileCategory.IMAGE: "file:upload:image",
        FileCategory.VIDEO: "file:upload:video",
        FileCategory.AUDIO: "file:upload:audio",
        FileCategory.DOCUMENT: "file:upload:document",
    }
    return mapping.get(category)

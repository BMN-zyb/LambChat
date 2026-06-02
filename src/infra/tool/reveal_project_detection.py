"""Project reveal file detection helpers."""

import json
import mimetypes
import os
from typing import Literal, Optional

ProjectTemplate = Literal[
    "react", "vue", "vanilla", "static", "angular", "svelte", "solid", "nextjs"
]
RevealMode = Literal["project", "folder"]

BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".bmp",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".mp3",
    ".mp4",
    ".webm",
    ".zip",
    ".mpg",
    ".mpeg",
    ".mov",
    ".avi",
    ".wav",
    ".ogg",
    ".flac",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".gz",
    ".tar",
    ".bz2",
    ".7z",
    ".rar",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".dat",
    ".wasm",
}

# 文本文件白名单：既覆盖前端项目，也覆盖常见代码/脚本/配置/文档目录
TEXT_EXTENSIONS = {
    # Web 核心
    ".html",
    ".htm",
    ".css",
    # JavaScript / TypeScript
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    # 框架 / 预处理器
    ".vue",
    ".svelte",
    ".less",
    ".scss",
    ".sass",
    ".styl",
    # 数据 / 配置
    ".json",
    ".json5",
    ".toml",
    ".yaml",
    ".yml",
    # 模板 / 标记
    ".md",
    ".mdx",
    ".txt",
    ".graphql",
    ".gql",
    # 其他前端资源
    ".map",
    ".xml",
    # 通用代码 / 脚本
    ".py",
    ".rb",
    ".php",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".cs",
    ".swift",
    ".scala",
    ".pl",
    ".pm",
    ".r",
    ".lua",
    ".zig",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".bat",
    ".cmd",
    # 通用配置 / 数据
    ".ini",
    ".cfg",
    ".conf",
    ".properties",
    ".lock",
    ".csv",
    ".tsv",
    ".sql",
    ".proto",
    ".dockerfile",
}

IGNORE_DIRS = {
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".DS_Store",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "coverage",
    ".turbo",
    ".cache",
    ".parcel-cache",
}

IGNORE_FILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    "tsconfig.tsbuildinfo",
    ".eslintcache",
}

ALLOWED_TEXT_FILENAMES = {
    "Dockerfile",
    "Containerfile",
    "Makefile",
    "Procfile",
    "Gemfile",
    "Rakefile",
    "Jenkinsfile",
}

# 入口文件候选顺序（按模板类型分组，避免 React 项目误选 /index.html）
ENTRY_CANDIDATES_BY_TEMPLATE: dict[str, list[str]] = {
    "nextjs": [
        "/pages/index.tsx",
        "/pages/index.jsx",
        "/pages/_app.tsx",
        "/pages/_app.jsx",
        "/index.html",
    ],
    "react": [
        "/src/main.tsx",
        "/src/main.jsx",
        "/src/index.tsx",
        "/src/index.jsx",
        "/src/main.ts",
        "/src/main.js",
        "/main.tsx",
        "/main.jsx",
        "/main.js",
        "/src/App.tsx",
        "/src/App.jsx",
        "/App.tsx",
        "/App.jsx",
        "/index.html",
    ],
    "vue": [
        "/src/main.js",
        "/src/main.ts",
        "/main.js",
        "/main.ts",
        "/src/main.vue",
        "/src/App.vue",
        "/App.vue",
        "/index.html",
    ],
    "svelte": [
        "/src/App.svelte",
        "/App.svelte",
        "/src/main.svelte",
        "/main.svelte",
        "/index.html",
    ],
    "angular": [
        "/src/main.ts",
        "/src/main.js",
        "/main.ts",
        "/main.js",
        "/index.html",
    ],
    "solid": [
        "/src/index.tsx",
        "/src/index.jsx",
        "/src/main.tsx",
        "/src/main.jsx",
        "/index.html",
    ],
    # static / vanilla / fallback：index.html 优先
    "_default": [
        "/index.html",
        "/src/index.html",
        "/public/index.html",
        "/src/main.ts",
        "/src/index.ts",
        "/src/index.tsx",
        "/src/index.jsx",
        "/src/main.tsx",
        "/src/main.jsx",
        "/src/main.js",
        "/main.ts",
        "/index.ts",
        "/index.js",
        "/main.js",
        "/src/main.vue",
        "/src/App.svelte",
        "/index.tsx",
        "/index.jsx",
        "/App.tsx",
        "/App.jsx",
    ],
}


def _has_any_file(file_keys: set[str], candidates: tuple[str, ...]) -> bool:
    return any(path in file_keys for path in candidates)


def detect_template(
    package_json_content: str, file_keys: Optional[set[str]] = None
) -> ProjectTemplate:
    """根据 package.json 内容和文件结构检测项目模板类型"""
    normalized_file_keys = file_keys or set()

    try:
        package = json.loads(package_json_content)
        deps = {
            **package.get("dependencies", {}),
            **package.get("devDependencies", {}),
        }
        if "next" in deps:
            return "nextjs"
        if "solid-js" in deps:
            return "solid"
        if "svelte" in deps:
            return "svelte"
        if any(name.startswith("@angular/") for name in deps):
            return "angular"
        if "react" in deps:
            return "react"
        if "vue" in deps:
            # 如果有 vite.config，使用 vite-vue 模板以获得更好的支持
            if file_keys and any(
                f.endswith(("vite.config.js", "vite.config.ts")) for f in file_keys
            ):
                return "vue"  # 前端会自动检测为 vite-vue
            return "vue"
    except (json.JSONDecodeError, AttributeError):
        pass

    if _has_any_file(
        normalized_file_keys,
        (
            "/pages/index.tsx",
            "/pages/index.jsx",
            "/pages/_app.tsx",
            "/pages/_app.jsx",
        ),
    ):
        return "nextjs"

    if _has_any_file(
        normalized_file_keys,
        (
            "/src/App.svelte",
            "/App.svelte",
            "/src/main.svelte",
            "/main.svelte",
        ),
    ):
        return "svelte"

    if "/angular.json" in normalized_file_keys and _has_any_file(
        normalized_file_keys,
        (
            "/src/main.ts",
            "/src/main.js",
            "/main.ts",
            "/main.js",
        ),
    ):
        return "angular"

    if _has_any_file(
        normalized_file_keys,
        (
            "/src/main.jsx",
            "/src/main.tsx",
            "/src/index.jsx",
            "/src/index.tsx",
            "/main.jsx",
            "/main.tsx",
            "/index.jsx",
            "/index.tsx",
            "/App.jsx",
            "/App.tsx",
        ),
    ):
        return "react"

    if _has_any_file(
        normalized_file_keys,
        (
            "/src/main.vue",
            "/src/App.vue",
            "/App.vue",
        ),
    ):
        return "vue"

    if "/index.html" in normalized_file_keys:
        return "static"

    return "vanilla"


def _should_skip(rel_path: str) -> bool:
    """检查文件是否应该跳过（忽略目录、隐藏文件、非白名单文本文件）"""
    parts = rel_path.strip("/").split("/")
    filename = parts[-1] if parts else ""

    if any(p in IGNORE_DIRS or (p.startswith(".") and p not in IGNORE_DIRS) for p in parts[:-1]):
        return True
    if filename.startswith(".") and filename not in IGNORE_FILES:
        return True
    if filename in IGNORE_FILES:
        return True

    if filename in ALLOWED_TEXT_FILENAMES:
        return False

    # 跳过不在白名单中的非二进制文件
    ext = os.path.splitext(filename)[1].lower()
    if ext not in BINARY_EXTENSIONS and ext not in TEXT_EXTENSIONS:
        return True

    return False


def _is_binary(filename: str) -> bool:
    """根据扩展名判断是否为二进制文件"""
    ext = os.path.splitext(filename)[1].lower()
    return ext in BINARY_EXTENSIONS


def _get_mime_type(filename: str) -> str:
    """根据文件名获取 MIME 类型"""
    ext = os.path.splitext(filename)[1].lower()

    # 为前端文件扩展名提供明确的 MIME 类型映射
    frontend_mime_types = {
        ".vue": "text/plain",
        ".svelte": "text/plain",
        ".jsx": "text/plain",
        ".tsx": "text/plain",
        ".ts": "text/plain",
        ".mts": "text/plain",
        ".cts": "text/plain",
        ".mjs": "text/plain",
        ".cjs": "text/plain",
        ".scss": "text/plain",
        ".sass": "text/plain",
        ".less": "text/plain",
        ".styl": "text/plain",
        ".json5": "text/plain",
        ".toml": "text/plain",
        ".yaml": "text/plain",
        ".yml": "text/plain",
        ".md": "text/plain",
        ".mdx": "text/plain",
        ".graphql": "text/plain",
        ".gql": "text/plain",
    }

    if ext in frontend_mime_types:
        return frontend_mime_types[ext]

    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"


def _find_entry(file_keys: set[str], template: Optional[str] = None) -> Optional[str]:
    """查找项目入口文件，优先使用模板对应的候选列表"""
    candidates = ENTRY_CANDIDATES_BY_TEMPLATE.get(template or "_default", [])
    if not candidates:
        candidates = ENTRY_CANDIDATES_BY_TEMPLATE["_default"]
    for candidate in candidates:
        if candidate in file_keys:
            return candidate
    return None


def _resolve_reveal_mode(entry: Optional[str]) -> RevealMode:
    """根据是否找到可运行入口，决定展示为项目还是普通文件夹。"""
    return "project" if entry else "folder"

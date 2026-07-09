"""
Path utilities and storage cache for SkillsStoreBackend.

Contains:
- Regex patterns for skills paths
- Path normalization and parsing
- Global SkillStorage cache
- Async runner for sync wrappers
"""

import asyncio
import re

from src.infra.logging import get_logger
from src.infra.skill.storage import SkillStorage

logger = get_logger(__name__)

# 路径匹配正则：
#   SKILLS_PATH_PATTERN → /skills/<name>/<file...>，捕获 (skill 名, 文件相对路径)
#   SKILLS_ROOT_PATTERN → /skills 或 /skills/ 根路径
#   SKILLS_DIR_PATTERN  → /skills/<name>/，即某个 skill 的目录
SKILLS_PATH_PATTERN = re.compile(r"^/skills/([^/]+)/(.+)$")
SKILLS_ROOT_PATTERN = re.compile(r"^/skills/?$")
SKILLS_DIR_PATTERN = re.compile(r"^/skills/([^/]+)/?$")

# 合法 skill 名：单词字符、CJK 汉字（一-鿿）、连字符与点，用于校验名称（允许中文 skill 名）。
SKILL_NAME_PATTERN = re.compile(r"^[\w一-鿿\-.]+$")

# 按 user_id 缓存 SkillStorage 实例（每用户一个）；用 asyncio.Lock 保证并发创建安全，
# 超过上限时按插入顺序淘汰最早创建的一个（近似 FIFO）。
_storage_cache: dict[str, SkillStorage] = {}
_storage_lock = asyncio.Lock()
MAX_STORAGE_CACHE_SIZE = 1000


async def _get_cached_storage(user_id: str) -> SkillStorage:
    """获取缓存的 SkillStorage 实例（async-safe，带容量上限）"""
    async with _storage_lock:
        if user_id not in _storage_cache:
            # 容量已满：弹出最早插入的条目（dict 保序，next(iter()) 即最旧的 key）
            if len(_storage_cache) >= MAX_STORAGE_CACHE_SIZE:
                _storage_cache.pop(next(iter(_storage_cache)))
            _storage_cache[user_id] = SkillStorage()
        return _storage_cache[user_id]


def _run_async(coro):
    """
    在同步上下文中安全地运行异步协程。

    如果没有运行中的事件循环 → 使用 asyncio.run()
    如果已有运行中的事件循环 → 报错，要求调用方使用异步 API
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # 已在事件循环内：先 close 掉未 await 的协程（避免 "never awaited" 警告），再报错
        coro.close()
        raise RuntimeError(
            "SkillsStoreBackend synchronous API cannot run inside an active event loop; "
            "use the async backend methods instead."
        )

    return asyncio.run(coro)


def normalize_path(path: str) -> str:
    """标准化路径，确保始终以 /skills/ 开头"""
    # 空路径 → 直接返回 skills 根目录
    if not path:
        return "/skills/"

    # 已是标准 /skills/ 前缀 → 原样返回
    if path.startswith("/skills/"):
        return path

    # 缺少前导斜杠的 "skills/..." → 补上开头的 "/"
    if path.startswith("skills/"):
        return f"/{path}"

    # 其他以 "/" 开头的绝对路径 → 前面拼接 /skills
    if path.startswith("/"):
        return f"/skills{path}"

    # 剩余情况视为相对路径 → 拼到 /skills/ 下
    return f"/skills/{path}"


def parse_skill_path(path: str):
    """
    解析 skills 路径

    Returns:
        (skill_name, file_path) 或 None（如果路径无效）
    """
    match = SKILLS_PATH_PATTERN.match(path)
    if match:
        return match.group(1), match.group(2)
    return None


def is_skills_root(path: str) -> bool:
    """检查是否是 skills 根路径"""
    normalized = normalize_path(path)
    return normalized in ("/skills/", "/skills") or bool(SKILLS_ROOT_PATTERN.match(normalized))


def is_skill_dir(path: str) -> bool:
    """检查是否是某个 skill 的目录"""
    normalized = normalize_path(path)
    return bool(SKILLS_DIR_PATTERN.match(normalized))


def get_skill_name_from_dir(path: str) -> str | None:
    """从目录路径获取 skill 名称"""
    normalized = normalize_path(path)
    match = SKILLS_DIR_PATTERN.match(normalized)
    if match:
        return match.group(1)
    return None

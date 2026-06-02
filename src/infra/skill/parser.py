"""
Unified SKILL.md parser

Single source of truth for extracting metadata from SKILL.md files.
Supports YAML frontmatter with fallback to markdown-style extraction.
"""

import re
from itertools import islice
from typing import Optional

# 允许的 skill name 字符：字母、数字、下划线、中文、连字符、点
_SKILL_NAME_ALLOWED = re.compile(r"^[\w\u4e00-\u9fff\-.]+$")
_FRONTMATTER_MAX_BYTES = 64 * 1024


def sanitize_skill_name(name: str) -> str:
    """将 name 转为路径安全的 skill_name。

    - 去掉首尾空白
    - 不允许路径分隔符（/ \\），只取最后一段
    - 空格和非法字符替换为连字符
    - 合并连续连字符
    - 去掉首尾连字符
    - 校验结果必须只含允许字符
    """
    name = name.strip()
    # 不允许路径分隔符，只取最后一段（防止 skill/sub/name 这种路径式命名）
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^\w\u4e00-\u9fff\-.]", "-", name)
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip("-")
    # 最终校验：只允许安全字符
    if not _SKILL_NAME_ALLOWED.match(name):
        return "unnamed-skill"
    return name or "unnamed-skill"


def _iter_first_lines(content: str, limit: int):
    start = 0
    for _ in range(limit):
        if start >= len(content):
            return
        end = content.find("\n", start)
        if end == -1:
            yield content[start:]
            return
        yield content[start:end].rstrip("\r")
        start = end + 1


def parse_skill_md(content: str) -> tuple[Optional[str], str, list[str]]:
    """
    Parse SKILL.md content to extract name, description, and tags.

    Parsing priority:
    1. YAML frontmatter (--- ... ---) with name, description, tags fields
    2. Fallback: first `# Title` line as description
    3. Fallback: `description:` line as description

    Args:
        content: SKILL.md file content

    Returns:
        (name, description, tags) tuple.
        name may be None if not found.
        description defaults to "".
        tags defaults to [].
    """
    name: Optional[str] = None
    description = ""
    tags: list[str] = []

    # Try YAML frontmatter
    if content.startswith("---"):
        search_end = min(len(content), _FRONTMATTER_MAX_BYTES)
        closing = content.find("\n---", 3, search_end)
        if closing != -1:
            frontmatter_text = content[3:closing].strip()
            try:
                import yaml

                frontmatter = yaml.safe_load(frontmatter_text)
                if isinstance(frontmatter, dict):
                    name = frontmatter.get("name")
                    desc = frontmatter.get("description")
                    if isinstance(desc, str):
                        description = desc.strip()
                    t = frontmatter.get("tags")
                    if isinstance(t, list):
                        tags = [str(tag) for tag in t]
            except Exception:
                pass

    # Fallback: scan first 20 lines for name/description
    for line in islice(_iter_first_lines(content, 20), 20):
        stripped = line.strip()

        # name: (only if not already set from frontmatter)
        if name is None and stripped.startswith("name:"):
            name = stripped.split("name:", 1)[1].strip().strip('"').strip("'")

        # description: (only if not already set from frontmatter)
        if not description and stripped.startswith("description:"):
            val = stripped.split("description:", 1)[1].strip()
            if val not in ("|", ">"):
                description = val.strip('"').strip("'")

        # # Title as description (only if not already set)
        if not description and stripped.startswith("# "):
            description = stripped[2:].strip()

    return name, description, tags

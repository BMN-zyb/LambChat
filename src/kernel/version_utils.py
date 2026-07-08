"""Version comparison utilities."""

# 本模块目前仅被 src/api/routes/version.py 的 /version 接口使用：
# 用于比较"当前运行版本"（settings.APP_VERSION）与"GitHub 最新 release 的
# tag_name"，计算出 has_update 字段返回给前端，用来提示用户是否有新版本可更新。
from typing import Optional

# 使用 packaging 库进行符合 PEP 440 规范的版本号解析与比较，
# 能正确处理数字型的版本比较（而不是简单按字符串排序）
from packaging import version


# 把版本号前缀的 "v" 去掉，例如 "v1.2.3" -> "1.2.3"；
# Git tag 命名习惯上常带 v 前缀，而语义化版本比较不需要这个前缀，
# 统一去掉之后才能公平地比较两个版本号
def normalize_version(v: str) -> str:
    """Normalize version string, removing 'v' prefix."""
    # 仅在字符串非空且以 "v" 开头时才去掉前缀，否则原样返回
    if v and v.startswith("v"):
        return v[1:]
    return v


# 判断 latest 是否比 current 更新；current 通常是 settings.APP_VERSION，
# latest 通常是 GitHub 最新 release 的 tag_name（可能为 None，表示未获取到）
def has_new_version(current: str, latest: Optional[str]) -> bool:
    """Check if latest version is newer than current."""
    # latest 为空（None 或空字符串）时，直接认为没有新版本可用
    if not latest:
        return False
    try:
        # 正常路径：先分别去掉两个版本号的 "v" 前缀，再用 packaging.version.parse
        # 做符合 PEP 440 规范的版本号解析和比较（能正确处理如 "1.10.0" > "1.9.0"
        # 这种按数字位比较的场景，避免简单按字符串排序出错）
        current_norm = normalize_version(current)
        latest_norm = normalize_version(latest)
        # 用 PEP 440 规范比较两个规整化后的版本号，返回 latest 是否严格大于 current
        return version.parse(latest_norm) > version.parse(current_norm)
    except Exception:
        # 兜底分支：当版本号格式不规范、不符合 PEP 440 规范导致 parse 抛出异常时触发
        # Fallback to string comparison if parsing fails
        # 这里的字符串兜底比较本身并不精确——例如按字符串比较时 "9" 会大于 "10"，
        # 这在版本号语义上是错的；但作为最后一道防线，目的是避免直接抛出异常导致
        # 整个 /version 接口报错，属于"宁可给出不完全精确的结果，也不要让功能
        # 直接挂掉"的防御性设计取舍
        return latest > current

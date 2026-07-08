"""Configuration utility functions."""
# 本模块提供配置系统所需的底层工具函数，不依赖 Settings 类本身，方便被 base.py/constants.py
# 等模块在早期加载阶段引用。主要包括：
# 1) 短密钥/salt 的确定性扩展算法（_deterministic_expand 及其两个语义化封装）；
# 2) 从 pyproject.toml 读取应用版本号；
# 3) 从 SETTING_DEFINITIONS 查询某个配置项的默认值；
# 4) 启动时读取一次 git tag/commit hash，用于在管理后台展示当前部署的版本信息。

from __future__ import annotations

import base64
import hashlib
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.infra.logging import get_logger

# 预留的"仅类型检查"导入区：当前没有需要在运行时避免导入的类型，故为空；
# 保留该结构方便未来添加只给类型检查器看、不产生运行时依赖/循环导入的 import
if TYPE_CHECKING:
    pass

# 本模块专属 logger，日志会带上 "src.kernel.config.utils" 模块名前缀
logger = get_logger(__name__)

# __file__ 是当前文件 .../src/kernel/config/utils.py，向上 4 层 parent 依次是：
# config 目录 -> kernel 目录 -> src 目录 -> 项目根目录（与 pyproject.toml 同级）
# Project root directory (where pyproject.toml is)
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


def _deterministic_expand(key: str, min_length: int = 32) -> str:
    """Deterministically expand a short key to at least min_length characters.

    Uses iterated SHA-256 hashing to derive a key of sufficient length.
    The same input always produces the same output.

    Args:
        key: The original key string.
        min_length: Minimum required length of the output.

    Returns:
        A URL-safe base64-encoded string of at least min_length characters.
    """
    # 已经满足长度要求就原样返回；这个函数只做"扩展"，不做截断
    if len(key) >= min_length:
        return key

    # 编码为 bytes 才能喂给 hashlib
    result = key.encode("utf-8")
    # SHA-256 每次固定输出 32 字节；如果 min_length 超过 32，
    # 就对上一轮的哈希结果继续哈希，直到长度达标（迭代哈希扩展）
    while len(result) < min_length:
        result = hashlib.sha256(result).digest()

    # 用 URL-safe 的 base64 编码并去掉末尾的 "=" 填充，
    # 得到的字符串可以安全地当作普通文本/URL 参数使用
    return base64.urlsafe_b64encode(result).decode("utf-8").rstrip("=")


def expand_jwt_secret_key(key: str, min_length: int = 32) -> str:
    """Expand a short JWT secret key to the minimum required length.

    Uses deterministic SHA-256 hashing to expand short keys to 32 bytes.
    This ensures the same input always produces the same output.

    Args:
        key: The original secret key (can be any length)
        min_length: Minimum required length

    Returns:
        A 32-byte URL-safe base64-encoded key
    """
    # 语义化封装：调用方只需关心"这是在处理 JWT 密钥"，具体扩展算法委托给
    # 通用实现 _deterministic_expand，两者共享同一套确定性哈希逻辑
    return _deterministic_expand(key, min_length)


def expand_encryption_salt(salt: str, min_length: int = 16) -> str:
    """Expand a short encryption salt to the minimum required length.

    Uses deterministic SHA-256 hashing to expand short salts.
    This ensures the same input always produces the same output.

    Args:
        salt: The original encryption salt (can be any length)
        min_length: Minimum required length

    Returns:
        A URL-safe base64-encoded string of at least min_length characters
    """
    # 同样委托给 _deterministic_expand，仅默认 min_length 不同（16 字节，对应 KDF 的要求）
    return _deterministic_expand(salt, min_length)


def get_app_version() -> str:
    """Read version from pyproject.toml."""
    pyproject_path = PROJECT_ROOT / "pyproject.toml"
    try:
        # tomllib 要求以二进制模式打开文件
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
            # 读取 [project] 表下的 version 字段；取不到就兜底为 "1.0.0"
            return data.get("project", {}).get("version", "1.0.0")
    except Exception as e:
        # 部分部署方式（如打包后的镜像）可能不包含 pyproject.toml，
        # 这里做兜底而不是让启动流程直接崩溃
        logger.warning(f"Failed to read version from pyproject.toml: {e}")
        return "1.0.0"


def get_default_from_settings(key: str, definitions: dict | None = None) -> Any:
    """Get default value from SETTING_DEFINITIONS"""
    # definitions 参数默认为 None，允许调用方（如测试代码）传入自定义字典；
    # 不传时才去 import 真正的 SETTING_DEFINITIONS——延迟到函数内部 import，
    # 避免 utils.py 在模块顶层就依赖 definitions.py，降低循环导入风险
    if definitions is None:
        from src.kernel.config.definitions import SETTING_DEFINITIONS

        definitions = SETTING_DEFINITIONS
    if key in definitions:
        return definitions[key].get("default")
    return None


def get_git_info() -> tuple[str | None, str | None]:
    """Get git tag and commit hash at startup.

    Returns:
        tuple of (git_tag, commit_hash) or (None, None) if not in a git repo
    """
    try:
        # Get git describe (tag or commit)
        # git describe --tags --always：如果当前 commit 上/之前有 tag 就返回类似
        # "v1.2.3" 或 "v1.2.3-4-gabcdef" 的描述；仓库完全没有 tag 时，--always
        # 保证至少回退输出短 commit hash，而不是报错
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        describe = result.stdout.strip() if result.returncode == 0 else None

        # Get commit hash
        # 再单独取一次精确的短 commit hash，不依赖上面 describe 的回退格式
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else None

        # If describe looks like a tag (starts with v), use it as tag
        # 项目的版本 tag 约定以 "v" 开头（如 v1.2.3）；如果 describe 的结果不是
        # 这种格式（很可能是 --always 回退出的裸 commit hash），就不当作 tag 使用，
        # git_tag 保持 None，避免把 commit hash 误当成版本号展示给用户
        git_tag = describe if describe and describe.startswith("v") else None

        return git_tag, commit_hash
    except Exception:
        # 非 git 环境（例如从源码 tarball 部署、没装 git 命令）时静默降级，
        # 不应该因为拿不到版本信息就影响服务启动
        return None, None


# Get git info at module load time
# 在模块首次被 import 时（即进程启动阶段）就执行一次 git 命令并缓存结果，
# 避免运行期每次访问都 fork 子进程；base.py 的 Settings.__init__ 会用这两个值
# 回填 GIT_TAG / COMMIT_HASH 字段（仅当没有通过环境变量显式设置时）
GIT_TAG, COMMIT_HASH = get_git_info()

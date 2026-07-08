"""Prompt builder for user environment variable keys.

Only variable names are exposed to the model. Values stay encrypted in storage
and are injected into sandbox command execution by the backend.
"""
# 中文说明：本模块把"用户已配置的环境变量 key 列表"渲染进系统提示词，
# 让 LLM 知道有哪些变量名可以在沙箱脚本/命令中直接引用（如 $FIRECRAWL_API_KEY），
# 但绝不会把变量的真实值暴露给模型上下文——值始终只存在于加密存储与沙箱执行环境之间。
# 按用户维度做内存缓存，避免每轮对话都重新查库拼接提示词文本。

import time

from src.infra.envvar.storage import EnvVarStorage
from src.infra.logging import get_logger

logger = get_logger(__name__)

# 缓存存活时间（秒）
_CACHE_TTL = 300
# 缓存条目上限，防止用户量增长导致内存缓存无限膨胀
_MAX_PROMPT_CACHE_ENTRIES = 500
# 缓存：user_id -> (提示词分段, 写入时间戳)
_env_var_prompt_cache: dict[str, tuple[tuple[str, ...], float]] = {}


async def build_env_var_prompt_sections(
    user_id: str, force_refresh: bool = False
) -> tuple[str, ...]:
    """Build prompt sections listing environment variable keys for a user."""
    if not user_id:
        return ()

    # 顺带清理过期/超量缓存条目
    _cleanup_stale_cache()
    # 未强制刷新且缓存未过期时，直接复用缓存结果
    if not force_refresh and user_id in _env_var_prompt_cache:
        prompt_sections, ts = _env_var_prompt_cache[user_id]
        if time.time() - ts < _CACHE_TTL:
            return prompt_sections

    try:
        variables = await EnvVarStorage().list_vars(user_id)
    except Exception:
        logger.warning(
            "[EnvVar Prompt] Failed to list env vars for user %s", user_id, exc_info=True
        )
        return ()

    # 只取变量名（key），绝不读取/拼接变量的加密值
    keys = sorted(variable.key for variable in variables if getattr(variable, "key", ""))
    if not keys:
        prompt_sections = ()
    else:
        # 固定的引导文案，明确告知模型"只有变量名可见，不能打印/泄露具体值"
        intro_lines = [
            "## Available Environment Variables",
            "",
            "The following environment variables are configured for sandbox execution. "
            "Their secret contents are not shown. Use the names directly in shell commands "
            "or code, for example `$FIRECRAWL_API_KEY` in shell or "
            '`os.environ.get("FIRECRAWL_API_KEY")` in Python. Do not print or reveal secrets.',
        ]
        key_lines = [f"- `{key}`" for key in keys]
        prompt_sections = ("\n".join(intro_lines), "\n".join(key_lines))

    _env_var_prompt_cache[user_id] = (prompt_sections, time.time())
    return prompt_sections


async def build_env_var_prompt(user_id: str, force_refresh: bool = False) -> str:
    """Build a prompt section listing environment variable keys for a user."""
    # 兼容旧调用方的入口：把分段拼接成一整段字符串
    return "\n\n".join(await build_env_var_prompt_sections(user_id, force_refresh))


def invalidate_env_var_prompt_cache(user_id: str) -> None:
    """Invalidate cached env-var prompt for one user."""
    # 中文：env_var_set/delete/delete_all 等工具修改变量后必须调用本函数，
    # 否则该用户下次请求仍会看到旧的变量名列表
    _env_var_prompt_cache.pop(user_id, None)


def _cleanup_stale_cache() -> None:
    # 清理所有已超过 TTL 的缓存条目
    now = time.time()
    stale = [user_id for user_id, (_, ts) in _env_var_prompt_cache.items() if now - ts > _CACHE_TTL]
    for user_id in stale:
        del _env_var_prompt_cache[user_id]
    _cleanup_excess_prompt_cache_entries()


def _cleanup_excess_prompt_cache_entries() -> int:
    # 过期清理之外，若条目数仍超过上限，按写入时间从旧到新淘汰多余部分
    max_entries = max(int(_MAX_PROMPT_CACHE_ENTRIES), 1)
    if len(_env_var_prompt_cache) <= max_entries:
        return 0

    to_remove = len(_env_var_prompt_cache) - max_entries
    oldest = sorted(
        _env_var_prompt_cache.items(),
        key=lambda item: item[1][1],
    )[:to_remove]
    for user_id, _entry in oldest:
        _env_var_prompt_cache.pop(user_id, None)
    return len(oldest)

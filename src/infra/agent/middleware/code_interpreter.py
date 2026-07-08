"""Optional QuickJS code interpreter middleware for Deep Agents."""

from __future__ import annotations

from typing import Any

from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)


def _is_enabled_value(value: Any) -> bool:
    # 把布尔或字符串形态的开关值统一解析为 bool（兼容 "1"/"true"/"on" 等写法）
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled", "enable"}
    return False


def create_code_interpreter_middleware(agent_options: dict[str, Any] | None) -> list[Any]:
    """Create CodeInterpreterMiddleware only when globally and per-run enabled."""
    # 需要"全局开启 + 本次运行开启"双重满足才启用（返回列表便于直接拼进中间件链）
    # 全局开关未开 -> 不启用
    if not getattr(settings, "ENABLE_CODE_INTERPRETER", False):
        return []

    # 本次 agent 运行未开启 -> 不启用
    if not _is_enabled_value((agent_options or {}).get("enable_code_interpreter")):
        return []

    # 依赖包缺失时降级跳过（可选依赖，不应导致启动失败）
    try:
        from langchain_quickjs import CodeInterpreterMiddleware
    except ImportError:
        logger.warning(
            "Code interpreter requested but langchain_quickjs is not installed; skipping"
        )
        return []

    return [CodeInterpreterMiddleware()]

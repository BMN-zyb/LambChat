from typing import Any

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# "思考模式"（extended thinking / reasoning）在不同模型厂商协议里的表达方式
# 不一样：Anthropic 要求传一个具体的 token 预算数（budget_tokens），Google
# Gemini 则是接受一个定性的档位字符串（level）。本模块把前端/配置里五花八门
# 的输入（旧版布尔开关、各种同义词字符串、当前标准的档位名）统一规整为
# off/low/medium/high/max 这 5 个标准档位，再一次性生成一份同时包含
# level 和 budget_tokens 两种表达的配置字典，交给下游按目标 provider
# 选用其中需要的字段，本模块本身不关心具体是哪个 provider。
# ============================================================================

# 当前标准支持的"开启"档位（不含 off，off 单独处理表示完全关闭思考模式）
SUPPORTED_THINKING_LEVELS = frozenset({"low", "medium", "high", "max"})

# budget_tokens 映射表 (用于 Anthropic 协议)
BUDGET_TOKENS_MAP: dict[str, int] = {
    "low": 1024,
    "medium": 8192,
    "high": 32768,
    "max": 65536,
}


def normalize_thinking_level(value: Any) -> str:
    """Normalize legacy and current thinking option values."""
    # 兼容旧版"只有开关、没有档位"的配置：True 时给一个居中的默认档位（medium），
    # False 直接关闭
    if isinstance(value, bool):
        return "medium" if value else "off"

    if isinstance(value, str):
        normalized = value.strip().lower()
        # 已经是标准档位（或显式 off）直接原样返回
        if normalized in SUPPORTED_THINKING_LEVELS or normalized == "off":
            return normalized
        # 兼容各种"开启"同义词，统一归一化成 medium 默认档位
        if normalized in {"enabled", "enable", "on", "true"}:
            return "medium"
        # 兼容各种"关闭"同义词
        if normalized in {"disabled", "disable", "false", "none"}:
            return "off"

    # 任何无法识别的输入（未知字符串、None、数字等）一律安全兜底为关闭，
    # 保证思考模式只能被显式、明确地开启，不会因为脏数据被意外打开
    return "off"


def build_thinking_config(agent_options: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build provider thinking config from agent options.

    Returns a dict with:
      - "level": normalized level string (for Google protocol)
      - "budget_tokens": mapped token budget (for Anthropic protocol)
    """
    level = normalize_thinking_level((agent_options or {}).get("enable_thinking"))
    # 关闭状态下返回 None，调用方据此判断"完全不要往请求里加思考相关参数"
    if level == "off":
        return None

    return {
        "type": "enabled",
        # 同时提供 level（给 Google 协议用）和 budget_tokens（给 Anthropic 协议用），
        # 未知档位时 budget_tokens 兜底给 medium 对应的值
        "level": level,
        "budget_tokens": BUDGET_TOKENS_MAP.get(level, 8192),
    }

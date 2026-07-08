"""Monkey-patch deepagents defaults so summarization degrades gracefully on unknown models."""

from __future__ import annotations

from src.kernel.config import settings


def apply_deepagents_patches() -> None:
    """给 deepagents 的 summarization 中间件打补丁，让"未知模型"也能优雅降级。

    deepagents 计算"何时触发上下文摘要"依赖模型 profile 里的 max_input_tokens；
    当模型没有该字段时（很多第三方/自建模型如此），原逻辑给不出合理阈值。本补丁在
    这种情况下用 settings.DEEPAGENT_DEFAULT_MAX_INPUT_TOKENS 兜底，按 85%/10%
    推导 trigger/keep。补丁幂等（打过标记就跳过），仅替换模块级函数，无其他副作用。
    """
    import deepagents.middleware.summarization as _summarization

    # 幂等保护：若已打过补丁（带 _lambchat_patched 标记），直接返回，避免重复包装
    current = _summarization.compute_summarization_defaults
    if getattr(current, "_lambchat_patched", False):
        return

    original = current

    def _patched_compute_summarization_defaults(model):
        defaults = original(model)
        profile = getattr(model, "profile", None)
        # 模型已带合法的 max_input_tokens（int）→ 原默认阈值可用，原样返回
        has_profile = (
            profile is not None
            and isinstance(profile, dict)
            and "max_input_tokens" in profile
            and isinstance(profile["max_input_tokens"], int)
        )
        if has_profile:
            return defaults

        # 无 profile 时用配置的兜底上限；<=0 表示未配置兜底，放弃降级、沿用原默认值
        fallback_max_input_tokens = int(getattr(settings, "DEEPAGENT_DEFAULT_MAX_INPUT_TOKENS", 0))
        if fallback_max_input_tokens <= 0:
            return defaults

        # 触发摘要的阈值取上限的 85%，摘要后保留窗口取 10%（都至少 1 token）
        trigger_tokens = max(int(fallback_max_input_tokens * 0.85), 1)
        keep_tokens = max(int(fallback_max_input_tokens * 0.10), 1)
        return {
            "trigger": ("tokens", trigger_tokens),
            "keep": ("tokens", keep_tokens),
            "truncate_args_settings": {
                "trigger": ("tokens", trigger_tokens),
                "keep": ("tokens", keep_tokens),
            },
        }

    # 打上幂等标记并替换模块级函数，完成 monkey-patch
    _patched_compute_summarization_defaults._lambchat_patched = True  # type: ignore[attr-defined]
    _summarization.compute_summarization_defaults = _patched_compute_summarization_defaults

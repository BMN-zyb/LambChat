"""
Localized recovery user messages for task resumption.

These messages are emitted as normal ``user:message`` events so the frontend
can keep rendering them without any protocol changes.
"""

from __future__ import annotations

from typing import Final

# 支持的恢复提示语言集合；不在其中的语言码统一回退到英文（en）。
SUPPORTED_RECOVERY_LANGUAGES: Final[set[str]] = {"en", "zh", "ja", "ko", "ru"}

# 找不到对应恢复原因时使用的默认原因键。
DEFAULT_RECOVERY_REASON = "server_restart"

# 恢复提示文案表：外层 key 是「恢复原因」（server_restart=系统重启触发的自动
# 恢复，manual_resume=用户手动点击继续），内层 key 是语言码。恢复流程会把
# 对应文案作为一条普通 user:message 事件重新发给 agent，从而在不改动前后端
# 协议的前提下让模型「接着上次未完成的内容继续处理」。
_RECOVERY_MESSAGES: Final[dict[str, dict[str, str]]] = {
    "server_restart": {
        "en": "The previous task was interrupted due to a system restart. Please continue processing the unfinished content in the current session.",
        "zh": "由于系统重启，上一轮任务已中断。请继续处理当前会话中未完成的内容。",
        "ja": "システムの再起動により前回のタスクが中断されました。現在のセッションで未完了の内容の処理を継続してください。",
        "ko": "시스템 재시작으로 인해 이전 작업이 중단되었습니다. 현재 세션에서 완료되지 않은 내용을 계속 처리해 주세요.",
        "ru": "Предыдущая задача была прервана из-за перезапуска системы. Пожалуйста, продолжите обработку незавершенного содержимого в текущей сессии.",
    },
    "manual_resume": {
        "en": "Please continue processing the unfinished content in the current session.",
        "zh": "请继续处理当前会话中未完成的内容。",
        "ja": "現在のセッションで未完了の内容の処理を継続してください。",
        "ko": "현재 세션에서 완료되지 않은 내용을 계속 처리해 주세요.",
        "ru": "Пожалуйста, продолжите обработку незавершенного содержимого в текущей сессии.",
    },
}


# 把任意语言码归一化为受支持的恢复语言之一。
# 处理形如 "zh-CN,en;q=0.9" 的 Accept-Language：取第一段、去掉地区后缀、
# 转小写；不受支持则回退到 "en"。
def normalize_recovery_language(language: str | None) -> str:
    """Normalize a language code to one of the supported recovery locales."""
    if not language:
        return "en"
    normalized = language.split(",")[0].split("-")[0].strip().lower()
    return normalized if normalized in SUPPORTED_RECOVERY_LANGUAGES else "en"


# 根据「恢复原因 + 语言」查表构造恢复提示语。
# 原因不在表内时回退到默认原因（server_restart）；语言缺失时回退到英文，
# 保证任何输入都能返回一条合理文案。
def build_recovery_message(reason: str, language: str | None) -> str:
    """Build a localized recovery message for a resumed task."""
    localized_reason = _RECOVERY_MESSAGES.get(reason) or _RECOVERY_MESSAGES[DEFAULT_RECOVERY_REASON]
    normalized_language = normalize_recovery_language(language)
    return localized_reason.get(normalized_language) or localized_reason["en"]

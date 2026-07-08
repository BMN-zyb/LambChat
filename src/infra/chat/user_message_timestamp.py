from __future__ import annotations

# 本模块用于在用户消息前拼接一段"本地时区时间戳"前缀，让模型能感知用户发消息时的真实本地时间
# （而不是服务器所在时区的时间），从而在需要"现在几点""今天星期几"等时间相关回答时更准确。
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.infra.utils.datetime import utc_now


def _coerce_now(now: datetime | None) -> datetime:
    # 未显式传入时间则取当前 UTC 时间；若传入的时间是"naive"（不带时区信息），
    # 统一视为 UTC 时间补上 tzinfo，保证后续 astimezone 转换的正确性。
    current = now or utc_now()
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _resolve_timezone(user_timezone: str | None) -> tuple[tzinfo, str]:
    # 尝试把用户传入的 IANA 时区名（如 "Asia/Shanghai"）解析为 tzinfo；
    # 传入为空或解析失败（时区名不合法/系统缺少对应时区数据）时，安全回退到 UTC。
    timezone_name = (user_timezone or "").strip()
    if timezone_name:
        try:
            return ZoneInfo(timezone_name), timezone_name
        except ZoneInfoNotFoundError:
            pass
    return timezone.utc, "UTC"


def _format_offset(current: datetime) -> str:
    # 把 strftime("%z") 输出的紧凑偏移量（如 "+0800"）格式化为带冒号的标准形式（"+08:00"）；
    # 若时间没有有效偏移信息（长度不是 5），兜底返回 "+00:00"。
    offset = current.strftime("%z")
    if len(offset) == 5:
        return f"{offset[:3]}:{offset[3:]}"
    return "+00:00"


def format_user_message_with_timestamp(
    content: str,
    user_timezone: str | None,
    now: datetime | None = None,
) -> str:
    # 主入口：将传入时间转换为用户所在时区的本地时间，格式化后作为前缀拼接到原始消息内容前面。
    # now 参数主要用于测试时注入固定时间，生产环境默认使用当前 UTC 时间。
    current = _coerce_now(now)
    tz, timezone_label = _resolve_timezone(user_timezone)
    localized = current.astimezone(tz)
    timestamp = localized.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"[User message sent at: {timestamp} {_format_offset(localized)} {timezone_label}] "
        f"{content}"
    )

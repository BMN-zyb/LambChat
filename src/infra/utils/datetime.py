"""时间日期工具:统一以 UTC(带时区)处理时间,提供当前时间、ISO 字符串互转与时区归一化。"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    # 返回当前 UTC 时间(带 tzinfo 的 aware datetime),避免使用无时区的 naive 时间。
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    # 返回当前 UTC 时间的 ISO 8601 字符串。
    return utc_now().isoformat()


def ensure_utc(dt: datetime) -> datetime:
    # 把任意 datetime 归一到 UTC:无时区者视为 UTC 直接贴标签,有时区者换算到 UTC。
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso(s: str) -> datetime:
    # 解析 ISO 8601 字符串并归一到 UTC(保证结果一定是 aware 的 UTC 时间)。
    dt = datetime.fromisoformat(s)
    return ensure_utc(dt)


def to_iso(dt: datetime | None) -> str | None:
    # 把 datetime 转成 UTC 的 ISO 字符串;传入 None 则返回 None(便于处理可空字段)。
    if dt is None:
        return None
    return ensure_utc(dt).isoformat()

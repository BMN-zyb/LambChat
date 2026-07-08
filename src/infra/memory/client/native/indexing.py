"""Indexing helpers for the native memory backend."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# "记忆索引"（memory index）是注入到 Agent 上下文里的一段紧凑文本目录，
# 让模型在不加载全部记忆详情的情况下，先看到"有哪些记忆、大致是什么、
# 多久没更新了"，需要时再按 short_id 去精确检索完整内容。
# 核心是三件事：
#   1. choose_index_memories：给同一 memory_type 下的记忆打分排序，
#      挑出最值得展示在索引里的若干条（按类型各留 5 条）；
#   2. build_memory_index：按 memory_type 分组、排序、格式化成
#      <memory_index>...</memory_index> 文本块，并做进程内缓存；
#   3. evict_index_cache：给这份缓存做 TTL 过期 + 超量淘汰，避免常驻内存无限增长。
# ============================================================================

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from src.infra.memory.client.types import MemoryType
from src.infra.utils.datetime import ensure_utc, utc_now
from src.kernel.config import settings


def choose_index_memories(
    docs: list[dict[str, Any]],
    per_type_limit: int,
    now: datetime,
    staleness_days: int,
) -> list[dict[str, Any]]:
    # 对同一 memory_type 下的记忆按"综合价值"打分排序，价值由三部分加权组成：
    # 来源可信度、历史访问热度、新鲜度；分数并列时用 -age_days 让更新的记忆排前面
    def score(doc: dict[str, Any]) -> tuple[float, float]:
        source = str(doc.get("source", "manual"))
        # 来源权重：人工手动记下的（manual）最可信，其次是自动保留（auto_retained），
        # 再次是整合/压缩生成的（consolidated），其余来源给一个较低的默认分
        source_score = (
            2.0
            if source == "manual"
            else 1.0
            if source == "auto_retained"
            else 0.8
            if source == "consolidated"
            else 0.5
        )
        # 访问热度：访问次数越多说明越重要，但设置上限（5 次）避免热点记忆一直霸占索引位
        access_score = min(float(doc.get("access_count", 0) or 0), 5.0) * 0.3
        # 新鲜度：距上次更新的天数越多分数越低，staleness_days 是衰减到 0 的参考周期，
        # max(0.0, ...) 保证不会衰减成负分
        age_days = (now - ensure_utc(doc.get("updated_at", now))).days
        freshness_score = max(0.0, 2.0 - (age_days / max(staleness_days, 1)))
        # 主键是综合分数（越高越优先），次键 -age_days 用于分数打平时优先展示更新的记忆
        return (source_score + access_score + freshness_score, -age_days)

    ranked = sorted(docs, key=score, reverse=True)
    # 每个 memory_type 最多只保留 per_type_limit 条，控制索引整体长度不至过长
    return ranked[:per_type_limit]


def evict_index_cache(index_cache: dict[str, tuple[float, str]], max_size: int) -> None:
    # 用 time.monotonic() 而非 wall clock，避免系统时间被调整（如 NTP 校时）导致 TTL 判断错乱
    now = time.monotonic()
    cache_ttl = getattr(settings, "NATIVE_MEMORY_INDEX_CACHE_TTL", 300)
    # 第一步：清掉已超过 TTL 的过期缓存项（键是 user_id，值是 (构建时间, 索引文本)）
    expired = [uid for uid, (t, _) in index_cache.items() if (now - t) >= cache_ttl]
    for uid in expired:
        del index_cache[uid]
    # 第二步：即便都没过期，缓存用户数仍可能超过上限（活跃用户很多），
    # 此时按构建时间从旧到新排序，淘汰最旧的若干条，把总量收回 max_size 以内
    if len(index_cache) > max_size:
        sorted_entries = sorted(index_cache.items(), key=lambda x: x[1][0])
        to_remove = len(index_cache) - max_size
        for uid, _ in sorted_entries[:to_remove]:
            del index_cache[uid]


async def build_memory_index(backend, user_id: str) -> str:
    # 索引构建有一定开销（查库 + 排序 + 格式化），且短时间内很可能被同一用户的
    # 多轮对话反复调用，因此先查进程内缓存，命中且未过期就直接复用，不重新构建
    cache_ttl = getattr(settings, "NATIVE_MEMORY_INDEX_CACHE_TTL", 300)
    cached = backend._index_cache.get(user_id)
    if cached:
        built_at, cached_str = cached
        if (time.monotonic() - built_at) < cache_ttl:
            return cached_str

    staleness_days = getattr(settings, "NATIVE_MEMORY_STALENESS_DAYS", 30)
    # 只投影索引展示需要的字段，不取完整的记忆正文，减少查询和传输开销
    projection = {
        "title": 1,
        "index_label": 1,
        "summary": 1,
        "memory_id": 1,
        "updated_at": 1,
        "memory_type": 1,
        "source": 1,
        "access_count": 1,
    }
    # 排除 session_summary（会话摘要不属于"记忆索引"要展示的核心记忆类型），
    # 按更新时间倒序只取最近的 80 条，作为候选池交给下面的分组排序逻辑挑选
    docs = (
        await backend._collection.find(
            {"user_id": user_id, "source": {"$ne": "session_summary"}},
            projection,
        )
        .sort("updated_at", -1)
        .limit(80)
        .to_list(length=80)
    )

    if not docs:
        return ""

    now = utc_now()
    # 按 memory_type 分组，后面每组各自独立打分排序、各自限量展示
    grouped: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        grouped.setdefault(str(doc.get("memory_type", "")), []).append(doc)

    # 展示顺序固定：用户画像 > 反馈 > 项目 > 参考资料，未知类型排在最后（用 99 兜底）
    type_order = {
        MemoryType.USER.value: 0,
        MemoryType.FEEDBACK.value: 1,
        MemoryType.PROJECT.value: 2,
        MemoryType.REFERENCE.value: 3,
    }

    lines = ["<memory_index>"]
    for mtype in sorted(grouped.keys(), key=lambda key: type_order.get(key, 99)):
        chosen = choose_index_memories(
            grouped[mtype], per_type_limit=5, now=now, staleness_days=staleness_days
        )
        if not chosen:
            continue
        lines.append(f"\n## [{mtype}]")
        for item in chosen:
            # 按距今天数生成人类可读的新鲜度标签：当天不加标签、昨天特殊表述、
            # 一周内显示"Nd ago"，超过 staleness_days 则打上 stale: 前缀提示模型
            # 这条记忆可能已经过时，中间的普通区间统一显示"Nd ago"
            age_days = (now - ensure_utc(item["updated_at"])).days
            if age_days == 0:
                age_str = ""
            elif age_days == 1:
                age_str = "yesterday"
            elif age_days <= 7:
                age_str = f"{age_days}d ago"
            elif age_days > staleness_days:
                age_str = f"stale:{age_days}d"
            else:
                age_str = f"{age_days}d ago"
            # 展示标题优先用专门为索引准备的 index_label（更简洁），
            # 没有则退回普通 title，再没有就截取摘要前 30 字符兜底
            display_title = item.get("index_label") or item.get("title") or ""
            if not display_title:
                display_title = (item.get("summary") or "")[:30]
            # 只展示 memory_id 前 6 位作为简短引用标识，方便模型在后续工具调用里精确指向这条记忆
            short_id = (item.get("memory_id") or "")[:6]
            if short_id:
                lines.append(
                    f"- {display_title} ({short_id}, {age_str})"
                    if age_str
                    else f"- {display_title} ({short_id})"
                )
            else:
                lines.append(f"- {display_title} ({age_str})" if age_str else f"- {display_title}")

    lines.append("\n</memory_index>")
    result = "\n".join(lines)
    # 写入缓存前记录当前 monotonic 时间戳，供下次调用判断是否已过 TTL；
    # 写入后顺手触发一次淘汰，避免多用户场景下缓存字典无限增长
    backend._index_cache[user_id] = (time.monotonic(), result)
    evict_index_cache(backend._index_cache, backend._INDEX_CACHE_MAX_SIZE)
    return result

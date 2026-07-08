"""Consolidation helpers for the native memory backend."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# "记忆整合"（consolidation）是原生 MongoDB 记忆后端的定期维护任务，目的是
# 防止用户的记忆随时间无限堆积、变得冗余或过时。整体分三层动作：
#   1. 按规则直接剪除（prune）明显该丢弃的记忆：过期的会话摘要、长期不被
#      访问的自动记忆——manual（用户手动记的）永远不受这层规则影响；
#   2. 对剩余的自动产生的记忆按 memory_type 分批喂给 LLM 做"合并去重"，
#      LLM 决定哪些该合并、哪些该整体丢弃，产出新的 consolidated 记忆；
#   3. 整合完成后如果单用户记忆总量仍超过硬上限（200 条），再按"最旧优先"
#      继续淘汰非手动记忆，直到回落到上限以内。
# 全过程通过分布式锁（acquire_lock/release_lock）保证同一用户不会被并发
# 整合两次，避免出现重复处理、数据竞争。
# ============================================================================

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.memory.client.native.content import (
    build_content_fields,
    delete_memory_content,
    maybe_await,
)
from src.infra.memory.client.native.summaries import (
    build_index_label,
    llm_enrich_memory,
)
from src.infra.memory.client.types import MemoryType
from src.infra.utils.datetime import ensure_utc, utc_now
from src.kernel.config import settings

logger = get_logger(__name__)

# 单次整合最多扫描的记忆条数上限，防止单用户记忆量异常庞大时整合任务耗时失控
_CONSOLIDATION_MEMORY_SCAN_LIMIT = 500
# 喂给 LLM 做批量整合时，每批记忆的条数
_CONSOLIDATION_BATCH_SIZE = 30
# 超出单用户记忆上限后，按"最旧优先"批量淘汰时每批处理的条数
_CONSOLIDATION_CAP_PRUNE_BATCH_SIZE = 100
# 单条记忆内容喂给 LLM 前的最大字符数，超出会被截断（见 _clip_consolidation_input_content）
_CONSOLIDATION_INPUT_CONTENT_MAX_CHARS = 4000


def _consolidation_input_content_max_chars() -> int:
    # 支持通过配置覆盖默认截断长度，配置值非法（非数字等）时安全回退到默认常量，
    # 并强制不小于 1，避免上层截断逻辑因非正长度而出现异常行为
    try:
        value = int(
            getattr(
                settings,
                "NATIVE_MEMORY_CONSOLIDATION_INPUT_MAX_CHARS",
                _CONSOLIDATION_INPUT_CONTENT_MAX_CHARS,
            )
            or _CONSOLIDATION_INPUT_CONTENT_MAX_CHARS
        )
    except (TypeError, ValueError):
        value = _CONSOLIDATION_INPUT_CONTENT_MAX_CHARS
    return max(value, 1)


def _clip_consolidation_input_content(content: Any) -> str:
    # 防止个别记忆内容过长把整合 prompt 撑爆（浪费 token、拖慢响应甚至超出上下文窗口），
    # 超长时截断并附加说明，让 LLM 知道这段内容是被截断过的，不必因内容"戛然而止"而困惑
    text = str(content or "")
    max_chars = _consolidation_input_content_max_chars()
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars].rstrip()
        + f"\n\n[truncated from {len(text)} chars for memory consolidation]"
    )


async def consolidate_memories(
    backend,
    user_id: str,
    acquire_lock: Callable[[str, str], Awaitable[str]],
    release_lock: Callable[[str, str], Awaitable[None]],
) -> dict[str, Any]:
    # instance_id 是本次整合尝试的唯一身份标识，写入分布式锁记录里，
    # 用来标明"锁当前是被谁持有的"（例如便于排查/续期），并非用户或数据的标识
    instance_id = uuid.uuid4().hex[:8]
    lock_state = await acquire_lock(user_id, instance_id)
    if lock_state != "acquired":
        # 抢锁失败：说明同一用户已有另一个整合任务在跑（或锁服务暂时不可用），
        # 直接跳过本次整合，避免并发整合互相踩踏同一批记忆文档
        return {
            "merged": 0,
            "pruned": 0,
            "total_before": 0,
            "skipped": True,
            "reason": "lock_unavailable" if lock_state == "unavailable" else "lock_not_acquired",
        }

    try:
        # 允许 backend 覆盖整合实现（例如测试里打桩），否则走本模块的默认实现
        if hasattr(backend, "_do_consolidate"):
            return await backend._do_consolidate(user_id)
        return await do_consolidate(backend, user_id)
    finally:
        # 无论整合成功、失败还是抛异常，都必须释放锁，否则该用户会被永久锁死无法再整合
        await release_lock(user_id, instance_id)


async def do_consolidate(backend, user_id: str) -> dict[str, Any]:
    # 按创建时间从旧到新扫描该用户的记忆（不取 embedding 字段，减少数据传输量），
    # 上限 500 条防止单个用户记忆量过大导致整合任务本身跑得过久
    cursor = backend._collection.find(
        {"user_id": user_id},
        {"embedding": 0},
        sort=[("created_at", 1)],
    ).limit(_CONSOLIDATION_MEMORY_SCAN_LIMIT)

    now = utc_now()
    prune_threshold = int(getattr(settings, "NATIVE_MEMORY_PRUNE_THRESHOLD", 90))
    total_before = 0
    # 记录直接判定要剪除（不经过 LLM 合并、直接删除）的记忆 id
    pruned_ids: set[str] = set()
    # 每个 memory_type 一个缓冲区，攒够一批（_CONSOLIDATION_BATCH_SIZE）就送去给 LLM 合并
    buffers: dict[str, list[dict[str, Any]]] = {mtype.value: [] for mtype in MemoryType}
    # 统计经 LLM 合并后净减少的记忆条数（合并前条数 - 合并后条数，可能为负——说明反而拆分/新增了）
    reduced = 0

    async def flush_type(memory_type: str, *, force: bool = False) -> None:
        nonlocal reduced
        batch = buffers[memory_type]
        # 正常情况下只有攒够一整批（>= BATCH_SIZE）才触发合并；force=True 用于扫描结束后
        # 的收尾冲刷，此时允许处理小到 3 条的残余批次（少于 3 条没必要专门调用一次 LLM）
        while len(batch) >= _CONSOLIDATION_BATCH_SIZE or (force and len(batch) >= 3):
            if force:
                current_batch = batch[:_CONSOLIDATION_BATCH_SIZE]
                del batch[: len(current_batch)]
            else:
                current_batch = batch[:_CONSOLIDATION_BATCH_SIZE]
                del batch[:_CONSOLIDATION_BATCH_SIZE]

            consolidated = await _llm_batch_consolidate(backend, current_batch, memory_type)
            # None 表示这一批 LLM 合并失败或结果不可信，原样保留旧记忆，什么都不做
            if consolidated is None:
                continue
            old_store_keys = [
                str(m.get("content_store_key"))
                for m in current_batch
                if m.get("content_storage_mode") == "store" and m.get("content_store_key")
            ]
            old_ids = [m["memory_id"] for m in current_batch]
            # Delete store content first to avoid orphaned files on crash
            # 先删外部内容存储、再删 MongoDB 文档：这样万一中途崩溃，最坏情况是
            # 文档还在但指向的内容已被删（可检测、可修复），而不是内容泄漏成孤儿文件
            if old_store_keys:
                await _delete_memory_contents_limited(backend, user_id, old_store_keys)
            await backend._collection.delete_many(
                {"user_id": user_id, "memory_id": {"$in": old_ids}}
            )
            if consolidated:
                await backend._collection.insert_many(consolidated)
            reduced += len(current_batch) - len(consolidated)

    async for m in cursor:
        total_before += 1
        source = m.get("source", "")
        updated = ensure_utc(m.get("updated_at", now))
        age_days = (now - updated).days
        access_count = m.get("access_count", 0)

        # manual（用户手动记下的）记忆永远不参与自动剪除/合并，直接跳过
        if source == "manual":
            continue
        # 会话摘要只是短期上下文辅助，超过 7 天就没有保留价值，直接剪除
        if source == "session_summary" and age_days > 7:
            pruned_ids.add(m["memory_id"])
            continue
        if source == "auto_retained":
            # 自动保留的记忆按"存活时间 + 访问次数"综合判断是否该剪除：
            # 超过半年一律剪除；超过阈值（默认90天）且几乎没被访问过也剪除；
            # 超过一个月但从未被访问过（access_count == 0）同样剪除
            if age_days > 180:
                pruned_ids.add(m["memory_id"])
                continue
            elif age_days > prune_threshold and access_count <= 1:
                pruned_ids.add(m["memory_id"])
                continue
            elif age_days > 30 and access_count == 0:
                pruned_ids.add(m["memory_id"])
                continue

        # 幸存下来的记忆按类型放入对应缓冲区，并顺带尝试一次冲刷
        # （随扫描过程增量触发 LLM 合并，而不是等全部扫描完再统一处理，控制内存峰值）
        memory_type = m.get("memory_type")
        if memory_type in buffers:
            buffers[memory_type].append(m)
            await flush_type(memory_type)

    # 记忆总量太少时不值得整合（LLM 调用成本 > 收益），直接原样返回
    if total_before < 5:
        return {"merged": 0, "pruned": 0, "total_before": total_before}

    if pruned_ids:
        await backend._collection.delete_many(
            {"user_id": user_id, "memory_id": {"$in": list(pruned_ids)}}
        )

    # 扫描已结束，对每个类型做一次强制冲刷，把缓冲区里剩余的记忆（哪怕不满一整批）也处理掉
    for mtype in MemoryType:
        await flush_type(mtype.value, force=True)

    # 数据已发生变化，清掉该用户相关的缓存（记忆索引缓存等），避免读到整合前的旧视图
    await backend._invalidate_cache(user_id)

    # 整合之后如果记忆总数仍超过单用户硬上限，按"最旧且非手动"优先继续淘汰，
    # 分批处理（每批最多 _CONSOLIDATION_CAP_PRUNE_BATCH_SIZE 条）直到回落到上限内
    max_per_user = 200
    current_count = await backend._collection.count_documents({"user_id": user_id})
    cap_pruned = 0
    while current_count > max_per_user:
        excess = min(current_count - max_per_user, _CONSOLIDATION_CAP_PRUNE_BATCH_SIZE)
        oldest_auto = (
            backend._collection.find(
                {"user_id": user_id, "source": {"$ne": "manual"}},
                {"memory_id": 1, "content_storage_mode": 1, "content_store_key": 1},
            )
            .sort("created_at", 1)
            .limit(excess)
        )
        oldest_docs = await oldest_auto.to_list(length=excess)
        if oldest_docs:
            # Clean up content store entries before deleting MongoDB docs
            # 同样遵循"先清外部内容、再删文档"的顺序，避免产生孤儿内容文件
            store_keys = [
                d["content_store_key"]
                for d in oldest_docs
                if d.get("content_storage_mode") == "store" and d.get("content_store_key")
            ]
            if store_keys:
                await _delete_memory_contents_limited(backend, user_id, store_keys)
            cap_ids = [d["memory_id"] for d in oldest_docs]
            result = await backend._collection.delete_many(
                {"user_id": user_id, "memory_id": {"$in": cap_ids}}
            )
            deleted_count = int(result.deleted_count)
            cap_pruned += deleted_count
            # 查到了文档但实际删除数为 0（如被其它进程抢先删除），说明再循环也无济于事，直接退出防止死循环
            if deleted_count <= 0:
                break
            await backend._invalidate_cache(user_id)
            current_count -= deleted_count
            continue
        # 找不到更多可删的非手动记忆了（剩下的都是 manual，受保护），只能放弃继续淘汰
        break

    final_count = await backend._collection.count_documents({"user_id": user_id})

    return {
        "merged": reduced,
        "pruned": len(pruned_ids) + cap_pruned,
        "total_before": total_before,
        "total_after": final_count,
    }


def _split_batches(items: list[dict], max_size: int = 30) -> list[list[dict]]:
    # 通用的等长分块工具：把列表切成多个不超过 max_size 的子列表
    return [items[i : i + max_size] for i in range(0, len(items), max_size)]


async def _delete_memory_contents_limited(
    backend,
    user_id: str,
    content_store_keys: list[str],
) -> None:
    # 批量删除外部内容存储（大内容不直接放 MongoDB 文档里，而是存到独立的内容存储，
    # 文档里只留一个 content_store_key 引用），用一个简单的"工作池"模式并发删除，
    # 避免逐个 await 顺序删除导致耗时线性叠加
    if not content_store_keys:
        return

    next_index = 0
    # 并发度取配置值与待删数量的较小者，避免为少量任务创建过多协程
    concurrency = min(
        max(
            1,
            int(
                getattr(
                    settings,
                    "NATIVE_MEMORY_CONTENT_DELETE_CONCURRENCY",
                    4,
                )
                or 1
            ),
        ),
        len(content_store_keys),
    )

    async def _worker() -> None:
        # 每个 worker 循环从共享游标 next_index 里认领下一个待删 key，
        # 直到所有 key 都被认领完；因单线程事件循环里两行之间没有 await，
        # 天然不会有多个 worker 同时拿到同一个 index
        nonlocal next_index
        while next_index < len(content_store_keys):
            index = next_index
            next_index += 1
            await delete_memory_content(backend, user_id, content_store_keys[index])

    await asyncio.gather(*(_worker() for _ in range(concurrency)))


async def _enrich_item(
    backend, content: str, provided_summary: str, provided_title: str, provided_tags: list
) -> dict[str, Any] | None:
    """Enrich a single consolidated item. Returns None if content is too short."""
    # 合并后内容过短（不足 10 字符）意味着这条记忆没有实质信息量，直接判定丢弃
    if not content or len(content) < 10:
        return None

    if provided_summary and provided_title and provided_tags:
        # LLM 在整合阶段已经一并给出了 summary/title/tags，直接复用，
        # 省去再调用一次 llm_enrich_memory 的开销；tags 仍做一轮基本校验
        # （必须是字符串、长度至少 2）并限制最多 5 个，防止脏数据混入
        summary = provided_summary
        title = provided_title
        tags = [str(t) for t in provided_tags if isinstance(t, str) and len(t) >= 2][:5]
    else:
        # 缺任何一项就整体走独立的富化调用，用 LLM 补全缺失字段
        # （已提供的字段作为兜底，防止富化结果反而把已有的好数据覆盖成空）
        enriched = await llm_enrich_memory(backend, content)
        summary = enriched.get("summary") or provided_summary
        title = enriched.get("title") or provided_title
        tags = enriched.get("tags") or []

    return {"summary": summary, "title": title, "tags": tags}


async def _llm_batch_consolidate(backend, memories: list[dict], expected_type: str):
    # 核心整合逻辑：把一批同类型记忆打包丢给 LLM，让它输出"合并去重后"的结果；
    # 任何环节出错（模型调用失败、返回不是合法 JSON、格式不符预期）都统一在
    # 最外层 except 兜底为 None，交由调用方保留原始记忆不做改动（保守失败策略）
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        model = await maybe_await(backend._get_memory_model())
        # 把每条记忆格式化为"[序号] (创建日期) 内容"的形式喂给模型，
        # 内容经过 _clip_consolidation_input_content 截断以控制 prompt 总长度
        items_text = "\n".join(
            f"[{i + 1}] ({m.get('created_at', '').strftime('%Y-%m-%d') if isinstance(m.get('created_at'), datetime) else 'unknown'}) "
            f"{_clip_consolidation_input_content(m.get('content', ''))}"
            for i, m in enumerate(memories)
        )
        # 提示词明确规则：合并同主题记忆（新信息优先）、剔除重复/过于笼统/过时/
        # 被更新记忆推翻/过短的记忆，每条输出记忆只表达一个聚焦的事实，
        # 并要求严格只输出 JSON 数组，方便下游直接解析
        prompt = (
            "You are a memory consolidation assistant. Given a list of memories, "
            "produce a clean, deduplicated, consolidated set.\n\n"
            "Rules:\n"
            "1. MERGE memories about the same topic — combine all unique facts, "
            "prefer newer info when conflicting\n"
            "2. KEEP memories that are unique, specific, and still relevant\n"
            "3. DELETE (omit from output) memories that are:\n"
            "   - Duplicates or near-duplicates of another memory\n"
            "   - Too vague or generic to be useful\n"
            "   - Outdated (old project status that has since changed)\n"
            "   - Contradicted by a newer memory\n"
            "   - Shorter than 15 characters\n"
            "4. Each output memory should be ONE focused fact or observation\n"
            "5. When merging, preserve all unique details from all source memories\n"
            '6. Keep memory type as: "{type}"\n\n'
            'Return ONLY a JSON array: [{{"content": "...", "summary": "...", "title": "...", "tags": ["...", "..."]}}]\n'
            "title should be max 25 chars, a short label for this memory.\n"
            "tags should be 3-5 meaningful keywords.\n"
            "Memories to delete should simply be OMITTED from the array.\n\n"
            f"Input memories (oldest first):\n{items_text}"
        ).format(type=expected_type)

        # system 提示特意强调"保守"（拿不准就保留），降低 LLM 过度删除导致信息丢失的风险
        response = await model.ainvoke(
            [
                SystemMessage(
                    content="You consolidate memories. Output only JSON. Be conservative — when in doubt, keep it."
                ),
                HumanMessage(content=prompt),
            ],
        )
        text = response.content
        # 某些模型返回的 content 是多模态内容块列表而非纯字符串，需要从中找出 text 块；
        # 找不到任何文本块说明返回格式完全不符合预期，直接放弃这一批
        if isinstance(text, list):
            for item in text:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    break
            else:
                return None
        text = str(text).strip()
        # 兼容模型习惯用 ```json ... ``` 包裹输出的情况，去掉首尾的代码块围栏
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        # JSON 解析属于 CPU 密集操作，丢线程池执行，避免文本较大时阻塞事件循环
        parsed = await run_blocking_io(json.loads, text.strip())
        # 结果必须是数组；输入有 3 条以上记忆却解出空数组，更可能是模型异常/幻觉
        # 而非"真的全部重复"，为安全起见也当作失败处理，保留原记忆不做删除
        if not isinstance(parsed, list) or (not parsed and len(memories) >= 3):
            return None

        now = utc_now()
        # 对 LLM 产出的每条记忆做富化（补全/校验 summary、title、tags），并发执行提速
        enrich_results = await _enrich_items_limited(backend, parsed)

        docs = []
        for item, meta in zip(parsed, enrich_results):
            # meta 为 None 表示该条内容过短已被 _enrich_item 判定丢弃
            if meta is None:
                continue
            content = item.get("content", "").strip()
            # Build content fields and embed concurrently across items
            # 构造内容存储字段（决定内联存储还是外部存储）与计算向量嵌入是两个独立的
            # 异步操作，用 gather 并发执行以缩短整体耗时
            memory_id = uuid.uuid4().hex
            content_fields, embedding = await asyncio.gather(
                build_content_fields(backend, memories[0]["user_id"], memory_id, content),
                backend._maybe_embed(content),
            )
            docs.append(
                {
                    "memory_id": memory_id,
                    # 同一批记忆必然属于同一用户，直接取第一条的 user_id
                    "user_id": memories[0]["user_id"],
                    "summary": meta["summary"][:100],
                    "title": meta["title"][:25],
                    "index_label": build_index_label(meta["title"], meta["summary"], content),
                    "memory_type": expected_type,
                    "context": "consolidated",
                    "tags": meta["tags"],
                    # source 标记为 consolidated，供后续整合/索引排序时区分记忆的产生方式
                    "source": "consolidated",
                    "embedding": embedding,
                    "created_at": now,
                    "updated_at": now,
                    "accessed_at": now,
                    # 新生成的记忆访问次数清零，重新开始计数
                    "access_count": 0,
                    **content_fields,
                }
            )
        return docs if docs else None
    except Exception as e:
        logger.warning(
            "[NativeMemory] Batch consolidation failed (batch of %d): %s", len(memories), e
        )
        return None


# 对外公开别名：供其它模块/测试以非私有名称引用同一实现
llm_batch_consolidate = _llm_batch_consolidate


async def _enrich_items_limited(backend, parsed: list[dict]) -> list[dict[str, Any] | None]:
    # 与 _delete_memory_contents_limited 相同的"工作池"并发限流模式，
    # 只是这里额外用了一把锁保护游标自增（效果等价，写法更保守显式）
    if not parsed:
        return []

    results: list[dict[str, Any] | None] = [None] * len(parsed)
    next_index = 0
    lock = asyncio.Lock()
    concurrency = min(
        max(
            1,
            int(
                getattr(
                    settings,
                    "NATIVE_MEMORY_CONSOLIDATION_ENRICH_CONCURRENCY",
                    4,
                )
                or 1
            ),
        ),
        len(parsed),
    )

    async def _worker() -> None:
        nonlocal next_index
        while True:
            async with lock:
                if next_index >= len(parsed):
                    return
                index = next_index
                next_index += 1
            item = parsed[index]
            results[index] = await _enrich_item(
                backend,
                item.get("content", "").strip(),
                item.get("summary", "").strip(),
                item.get("title", "").strip(),
                item.get("tags") or [],
            )

    await asyncio.gather(*(_worker() for _ in range(concurrency)))
    return results

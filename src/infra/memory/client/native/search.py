"""Search helpers for the native memory backend."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# recall_memories 是整个原生记忆后端检索能力的总入口，内部是一套"混合检索 +
# 融合排序 + 重排"的流水线，零外部依赖也能跑（纯文本检索），配置了 embedding
# 接口后还能叠加语义检索：
#   1. 文本检索 text_search：优先用 MongoDB 原生 $text 全文索引，索引缺失/
#      报错/查不到结果时退化为 keyword_fallback（正则关键词匹配）；
#   2. 语义检索 vector_search：优先用 MongoDB Atlas 的 $vectorSearch（近似
#      向量检索），Atlas 特性不可用时退化为拉一批候选文档在 Python 里算
#      余弦相似度（cosine_similarity）暴力比对；
#   3. 融合 rrf_merge：用 Reciprocal Rank Fusion 算法把文本检索和语义检索
#      两路排名（分数量纲完全不同，无法直接比较数值）融合成一份统一排序；
#   4. 重排 rerank_candidates：优先调用外部 rerank API 精排，未配置时退化为
#      local_rerank（纯规则的字段重叠度打分，不依赖任何外部服务）；
#   5. 兜底 recent_context_fallback：如果以上都没检索到结果，且看起来像是
#      "给我一份概览"式的宽泛提问，就退化为直接返回最近更新的若干条记忆。
# 全程贯彻"能力分层退化"的设计思路：越高级的能力（Atlas 向量检索、外部
# rerank API）都是可选增强，缺失时始终有更朴素但零依赖的兜底方案可用。
# ============================================================================

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Optional

import httpx

from src.infra.memory.client.native.content import hydrate_formatted_memory
from src.infra.memory.client.native.models import (
    STOPWORDS,
    char_ngrams,
    cosine_similarity,
    has_cjk,
)
from src.infra.utils.datetime import ensure_utc, utc_now
from src.kernel.config import settings

# 单次 recall 最多允许请求的结果条数上限
NATIVE_MEMORY_RECALL_MAX_RESULTS = 20
# 查询文本被截断前允许的最大字符数，防止超长 query 拖慢正则/文本检索
NATIVE_MEMORY_RECALL_QUERY_MAX_CHARS = 2_000


def _clip_recall_query(query: str) -> str:
    # 配置值非法时用默认常量兜底，且强制不小于 1，避免下游按非正长度截断出异常结果
    max_chars = max(
        int(
            getattr(
                settings,
                "NATIVE_MEMORY_RECALL_QUERY_MAX_CHARS",
                NATIVE_MEMORY_RECALL_QUERY_MAX_CHARS,
            )
            or 0
        ),
        1,
    )
    normalized = str(query or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip()


async def _hydrate_memories_limited(
    backend, memories: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    # 对最终确定要返回的这批记忆做"内容还原"（把截断预览换成完整原文），
    # 用工作池模式限制并发度，避免一次性发起过多外部存储读取请求
    if not memories:
        return []

    results: list[dict[str, Any] | None] = [None] * len(memories)
    next_index = 0
    lock = asyncio.Lock()
    worker_count = min(
        max(1, int(getattr(settings, "NATIVE_MEMORY_HYDRATE_CONCURRENCY", 4) or 1)),
        len(memories),
    )

    async def _worker() -> None:
        nonlocal next_index
        while True:
            async with lock:
                if next_index >= len(memories):
                    return
                index = next_index
                next_index += 1
            results[index] = await hydrate_formatted_memory(backend, memories[index])

    await asyncio.gather(*(_worker() for _ in range(worker_count)))
    return [memory for memory in results if memory is not None]


def build_keyword_clauses(query: str) -> list[dict[str, Any]]:
    # 当 $text 全文索引不可用/无结果时的兜底方案：把 query 拆成若干"关键词"，
    # 为每个关键词在 content/summary/title 三个字段上各构造一条大小写不敏感的
    # 正则匹配子句，最终以 $or 组合供 keyword_fallback 使用
    normalized = query.strip()
    terms: list[str] = []

    if has_cjk(normalized):
        # 中文没有分词边界，用滑动窗口切出候选"词"：优先取 3 字片段（更具体、
        # 误判率更低），凑不够 5 个再补 2 字片段；全部由高频虚词组成的片段
        # （如"的是在了和有"的任意组合）几乎不带信息量，直接跳过
        compact = re.sub(r"\s+", "", normalized)
        seen: set[str] = set()
        for n in (3, 2):
            for i in range(max(len(compact) - n + 1, 0)):
                term = compact[i : i + n]
                if len(term) < 2 or term in seen:
                    continue
                if all(ch in "的是在了和有" for ch in term):
                    continue
                seen.add(term)
                terms.append(term)
                if len(terms) >= 5:
                    break
            if len(terms) >= 5:
                break
    else:
        # 非中文：直接按空白分词，保留长度 >= 2 且非停用词的词，最多取 5 个
        terms = [w for w in normalized.lower().split() if len(w) >= 2 and w not in STOPWORDS][:5]

    clauses: list[dict[str, Any]] = []
    for term in terms:
        # re.escape 转义关键词里的正则特殊字符，避免用户输入被当成正则表达式解释
        escaped = re.escape(term)
        clauses.append({"content": {"$regex": escaped, "$options": "i"}})
        clauses.append({"summary": {"$regex": escaped, "$options": "i"}})
        clauses.append({"title": {"$regex": escaped, "$options": "i"}})
    return clauses


def format_memory(doc: dict, score: float, now: datetime | None = None) -> dict:
    # 把原始 Mongo 文档 + 检索得到的分数，统一转换成对外返回的"格式化记忆"结构
    current_time = now or utc_now()
    staleness_days = (current_time - ensure_utc(doc["updated_at"])).days
    staleness_days_cfg = getattr(settings, "NATIVE_MEMORY_STALENESS_DAYS", 30)

    result: dict[str, Any] = {
        "memory_id": doc["memory_id"],
        "user_id": doc.get("user_id"),
        "text": doc["content"],
        "preview": doc.get("content", ""),
        "summary": doc["summary"],
        "title": doc.get("title", ""),
        "type": doc["memory_type"],
        "source": doc.get("source", "manual"),
        "storage_mode": doc.get("content_storage_mode", "inline"),
        "content_store_key": doc.get("content_store_key"),
        "created_at": doc["created_at"].isoformat()
        if isinstance(doc["created_at"], datetime)
        else str(doc["created_at"]),
        "score": score,
    }
    # 超过陈旧阈值时附加一条警告，提示调用方（通常是 LLM）这条记忆可能已经过时，
    # 引用时需要谨慎或应该重新确认
    if staleness_days > staleness_days_cfg:
        result["staleness_warning"] = (
            f"This memory is {staleness_days} days old and may be outdated"
        )
    return result


def prioritize_sources(memories: list[dict]) -> list[dict]:
    # 最终展示顺序优先按来源可信度分层：手动 > 自动保留 > 整合生成 > 会话摘要
    # （未知来源给中间档 50），同一层内再按相关性分数从高到低排（取负号实现降序）
    source_order = {
        "manual": 0,
        "auto_retained": 1,
        "consolidated": 2,
        "session_summary": 99,
    }
    return sorted(
        memories,
        key=lambda memory: (
            source_order.get(str(memory.get("source", "")), 50),
            -float(memory.get("score", 0.0) or 0.0),
        ),
    )


def is_context_overview_query(query: str) -> bool:
    # 识别"给我一份概览"类的宽泛提问——这类查询往往不会跟任何具体记忆的文本
    # 直接命中，但用户其实是想知道"目前有哪些相关记忆"，需要走不同的兜底策略
    lowered = query.strip().lower()
    overview_markers = (
        "user preferences",
        "project context",
        "context overview",
        "what should i know",
        "memory overview",
        "relevant memories",
    )
    return any(marker in lowered for marker in overview_markers)


async def recent_context_fallback(
    collection, user_id: str, limit: int, memory_types: Optional[list[str]]
) -> list[dict]:
    # 兜底策略：不做任何相关性匹配，直接按更新时间取最近的若干条记忆，
    # 用于常规检索完全没有命中、但查询看起来是想要"整体概览"的场景
    base: dict[str, Any] = {"user_id": user_id, "source": {"$ne": "session_summary"}}
    if memory_types:
        base["memory_type"] = {"$in": memory_types}
    cursor = (
        collection.find(
            base,
            {
                "memory_id": 1,
                "user_id": 1,
                "content": 1,
                "summary": 1,
                "title": 1,
                "memory_type": 1,
                "source": 1,
                "content_storage_mode": 1,
                "content_store_key": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        )
        .sort("updated_at", -1)
        .limit(limit)
    )
    docs = await cursor.to_list(length=limit)
    # 没有真实相关性分数，统一给 0.0（后续 prioritize_sources/阈值过滤时按此处理）
    return [format_memory(doc, 0.0) for doc in docs]


async def text_search(
    collection, logger, user_id: str, query: str, limit: int, memory_types: Optional[list[str]]
) -> list[dict]:
    # 优先走 MongoDB 原生全文索引（$text），排序依据是 MongoDB 计算的 textScore
    base: dict[str, Any] = {"user_id": user_id, "source": {"$ne": "session_summary"}}
    if memory_types:
        base["memory_type"] = {"$in": memory_types}
    base["$text"] = {"$search": query}

    try:
        cursor = (
            collection.find(base, {"score": {"$meta": "textScore"}})
            .sort([("score", {"$meta": "textScore"})])
            .limit(limit)
        )
        docs = await cursor.to_list(length=limit)
    except Exception:
        # $text 查询失败（最常见原因是全文索引没建成功，参见 backend.py 里
        # 索引创建的 try/except），退化为基于正则的关键词匹配
        logger.debug("[NativeMemory] Text search failed, falling back to keyword match")
        docs = await keyword_fallback(collection, user_id, query, limit, memory_types)
    else:
        # 索引存在但没查到任何结果时，同样尝试关键词兜底再搏一次
        # （$text 的分词/语言分析器有时会漏掉全文索引里能查到的内容）
        if not docs:
            docs = await keyword_fallback(collection, user_id, query, limit, memory_types)

    return [format_memory(doc, doc.get("score", 0)) for doc in docs]


async def keyword_fallback(
    collection, user_id: str, query: str, limit: int, memory_types: Optional[list[str]]
) -> list[dict]:
    clauses = build_keyword_clauses(query)
    # 抽不出任何有效关键词（如 query 全是停用词/太短），没有子句可查，直接返回空
    if not clauses:
        return []

    base: dict[str, Any] = {
        "user_id": user_id,
        "source": {"$ne": "session_summary"},
        "$or": clauses,
    }
    if memory_types:
        base["memory_type"] = {"$in": memory_types}

    _projection = {
        "memory_id": 1,
        "user_id": 1,
        "content": 1,
        "summary": 1,
        "title": 1,
        "memory_type": 1,
        "source": 1,
        "content_storage_mode": 1,
        "content_store_key": 1,
        "created_at": 1,
        "updated_at": 1,
    }
    try:
        cursor = collection.find(base, _projection)
    except TypeError:
        # 兼容某些驱动/测试用的 fake collection 不接受 projection 参数的情况
        cursor = collection.find(base)
    cursor = cursor.sort("updated_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def vector_search(
    backend, user_id: str, query: str, limit: int, memory_types: Optional[list[str]]
) -> list[dict]:
    # 没配置 embedding 能力就直接没有语义检索结果（上层据此退化为纯文本检索）
    query_vec = await backend._maybe_embed(query)
    if not query_vec:
        return []

    base: dict[str, Any] = {
        "user_id": user_id,
        "source": {"$ne": "session_summary"},
        "embedding": {"$exists": True, "$ne": None},
    }
    if memory_types:
        base["memory_type"] = {"$in": memory_types}

    try:
        # 优先尝试 MongoDB Atlas 的原生向量检索（$vectorSearch 是 Atlas Search
        # 特有的聚合阶段，自建/非 Atlas 的 MongoDB 不支持，会在这里抛异常）。
        # numCandidates 故意设置成远大于 limit（5 倍），给 ANN 近似搜索留出
        # 足够的候选空间，再交给 $match 按用户/类型精确过滤
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "native_mem_vector_idx",
                    "path": "embedding",
                    "queryVector": query_vec,
                    "numCandidates": limit * 5,
                    "limit": limit,
                }
            },
            {"$match": base},
        ]
        cursor = backend._collection.aggregate(pipeline)
        docs = await cursor.to_list(length=limit)
        return [format_memory(doc, doc.get("score", 1.0)) for doc in docs]
    except Exception:
        # Atlas 特性不可用（如自建 MongoDB、索引未创建等），静默转入下面的
        # Python 侧暴力余弦相似度兜底方案
        pass

    backend._logger.debug(
        "[NativeMemory] Atlas $vectorSearch unavailable, using Python cosine fallback"
    )
    projection = {
        "user_id": 1,
        "memory_id": 1,
        "content": 1,
        "title": 1,
        "content_storage_mode": 1,
        "content_store_key": 1,
        "summary": 1,
        "memory_type": 1,
        "source": 1,
        "created_at": 1,
        "updated_at": 1,
        "embedding": 1,
    }
    # 暴力扫描没有索引加速，必须严格限制扫描规模（最多 limit 的 3 倍，且不超过
    # 100 条），只在"最近更新"的一个有限窗口内比对，牺牲一些召回率换取性能
    scan_limit = min(limit * 3, 100)
    cursor = backend._collection.find(base, projection).sort("updated_at", -1).limit(scan_limit)
    docs = await cursor.to_list(length=scan_limit)
    scored = []
    for d in docs:
        emb = d.get("embedding")
        if emb:
            sim = cosine_similarity(query_vec, emb)
            scored.append((sim, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [format_memory(d, sim) for sim, d in scored[:limit]]


def _query_terms(query: str) -> set[str]:
    # 供本地重排（local_rerank）使用的查询分词：与 build_keyword_clauses 不同，
    # 这里中文同时取 2-gram 和 3-gram 混合（覆盖短词和稍长短语两种粒度），
    # 目的是尽量提高与候选记忆字段的重叠检出率，而不是像 Mongo 检索那样要控制子句数量
    normalized = query.strip().lower()
    if not normalized:
        return set()
    if has_cjk(normalized):
        return char_ngrams(normalized, 2) | char_ngrams(normalized, 3)
    return {w for w in re.findall(r"\w+", normalized) if len(w) >= 2 and w not in STOPWORDS}


def _field_overlap_score(query_terms: set[str], text: str) -> float:
    # 计算 query 词集合与某个字段文本词集合的重叠程度，融合两个维度：
    # coverage（query 中有多大比例的词在这个字段里出现，反映"字段覆盖了多少查询意图"）
    # 和 density（字段自身的词里有多大比例命中 query，反映"字段内容有多聚焦"），
    # coverage 权重更高（0.7 vs 0.3），因为"查询被覆盖到什么程度"通常比
    # "字段有多干净"更重要
    if not query_terms or not text.strip():
        return 0.0
    lowered = text.lower()
    if has_cjk(lowered):
        field_terms = char_ngrams(lowered, 2) | char_ngrams(lowered, 3)
    else:
        field_terms = {w for w in re.findall(r"\w+", lowered) if len(w) >= 2 and w not in STOPWORDS}
    if not field_terms:
        return 0.0
    overlap = len(query_terms & field_terms)
    coverage = overlap / max(len(query_terms), 1)
    density = overlap / max(len(field_terms), 1)
    return coverage * 0.7 + density * 0.3


def local_rerank(query: str, candidates: list[dict], max_results: int) -> list[dict]:
    # 不依赖任何外部服务的重排方案：在已有检索分数（base_score，来自文本/
    # 向量检索或融合后的 RRF 分数）之上，叠加标题/摘要/正文三个字段各自的
    # 词重叠分数，按字段的"信号强度"给不同权重——命中标题最有说服力（权重
    # 0.8），其次摘要（0.6），正文因为篇幅大、噪声多，权重最低（0.3）
    query_terms = _query_terms(query)

    def score(candidate: dict) -> tuple[float, float, float, float]:
        title_score = _field_overlap_score(query_terms, str(candidate.get("title", "")))
        summary_score = _field_overlap_score(query_terms, str(candidate.get("summary", "")))
        text_score = _field_overlap_score(query_terms, str(candidate.get("text", "")))
        base_score = float(candidate.get("score", 0.0) or 0.0)
        blended = base_score + title_score * 0.8 + summary_score * 0.6 + text_score * 0.3
        # 元组排序：主键是综合分，后面几个分量分数作为并列时的次级排序依据，
        # 保证排序结果稳定、可复现
        return (
            blended,
            title_score,
            summary_score,
            text_score,
        )

    ranked = sorted(candidates, key=score, reverse=True)
    return ranked[:max_results]


async def rerank_candidates(query: str, candidates: list[dict], max_results: int) -> list[dict]:
    # 重排的"高级"路径：调用外部 rerank API（如 Cohere/Jina 兼容协议）取得
    # 更精准的相关性排序；未配置该能力，或候选数 <= 1（无需重排）时，
    # 直接退化为免费、零依赖的 local_rerank
    rerank_model = getattr(settings, "NATIVE_MEMORY_RERANK_MODEL", "") or ""
    api_base = getattr(settings, "NATIVE_MEMORY_RERANK_API_BASE", "") or ""
    api_key = getattr(settings, "NATIVE_MEMORY_RERANK_API_KEY", "") or ""

    if not rerank_model or not api_base or not api_key or len(candidates) <= 1:
        return local_rerank(query, candidates, max_results)

    # 把每个候选的标题/摘要/正文拼成一段文本喂给 rerank API，跳过空字段避免多余空行
    documents = [
        "\n".join(
            part
            for part in (
                str(candidate.get("title", "")).strip(),
                str(candidate.get("summary", "")).strip(),
                str(candidate.get("text", "")).strip(),
            )
            if part
        )
        for candidate in candidates
    ]

    try:
        async with httpx.AsyncClient(
            base_url=api_base.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        ) as client:
            response = await client.post(
                "/v1/rerank",
                json={
                    "model": rerank_model,
                    "query": query,
                    "documents": documents,
                    "top_n": max_results,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        # 网络异常、超时、非 2xx 状态码等任何问题，都不影响主流程，退化为本地重排
        return local_rerank(query, candidates, max_results)

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        # 返回体格式不符合预期（比如换了个不兼容的 rerank 服务），同样退化处理
        return local_rerank(query, candidates, max_results)

    ranked: list[dict] = []
    seen: set[int] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        # 校验索引合法且未重复出现，防御 rerank API 返回异常/越界/重复索引
        if not isinstance(idx, int) or idx < 0 or idx >= len(candidates) or idx in seen:
            continue
        # 用浅拷贝而非直接引用原候选对象，避免覆盖 score 字段影响到别处仍持有的引用
        candidate = dict(candidates[idx])
        if "relevance_score" in item:
            candidate["score"] = float(item["relevance_score"])
        ranked.append(candidate)
        seen.add(idx)

    # API 返回了 results 字段但里面一个有效项都没解析出来，仍然兜底本地重排，
    # 保证这个函数无论如何都能返回一份可用的排序结果
    return ranked[:max_results] if ranked else local_rerank(query, candidates, max_results)


def rrf_merge(
    text_results: list[dict], vector_results: list[dict], max_results: int, k: int = 60
) -> list[dict]:
    # Reciprocal Rank Fusion：文本检索分数（textScore/正则匹配无分）和向量
    # 检索分数（余弦相似度）量纲完全不同，无法直接加权比较数值，RRF 的思路是
    # 只看每条结果在各自列表里的"排名"而不看具体分数值，用 1/(k+rank+1) 累加，
    # k=60 是信息检索领域的经验常数，用来压低排名靠后结果的权重差异；
    # 同一条记忆如果两路都命中，两边的贡献会相加，天然获得"两种方法都认可"的加分
    scores: dict[str, dict] = {}

    for rank, item in enumerate(text_results):
        mid = item["memory_id"]
        if mid not in scores:
            scores[mid] = {"data": item, "rrf_score": 0.0}
        scores[mid]["rrf_score"] += 1.0 / (k + rank + 1)

    for rank, item in enumerate(vector_results):
        mid = item["memory_id"]
        if mid not in scores:
            scores[mid] = {"data": item, "rrf_score": 0.0}
        scores[mid]["rrf_score"] += 1.0 / (k + rank + 1)

    merged = sorted(scores.values(), key=lambda x: x["rrf_score"], reverse=True)
    return [entry["data"] for entry in merged[:max_results]]


async def recall_memories(
    backend,
    user_id: str,
    query: str,
    max_results: int = 5,
    memory_types: Optional[list[str]] = None,
    touch_access: bool = True,
    enable_rerank: bool = True,
) -> dict[str, Any]:
    # 整个混合检索流水线的总编排：限幅参数 -> 并发检索 -> RRF 融合 ->
    # 概览兜底 -> 重排 -> 来源分层排序 -> 截断+阈值过滤 -> 内容还原 -> 访问计数
    max_results = max(1, min(int(max_results or 1), NATIVE_MEMORY_RECALL_MAX_RESULTS))
    query = _clip_recall_query(query)
    # 先构造协程但不立即 await，方便下面视情况决定是单独等待还是与向量检索并发等待
    text_coro = text_search(
        backend._collection, backend._logger, user_id, query, max_results * 2, memory_types
    )

    if backend._embedding_fn:
        # 配置了 embedding 能力：文本检索和向量检索并发执行，减少总耗时；
        # 两路都各自多取一倍（max_results * 2）候选，给后续融合/重排留出挑选空间
        text_results, vector_results = await asyncio.gather(
            text_coro,
            vector_search(backend, user_id, query, max_results * 2, memory_types),
        )
    else:
        # 未配置 embedding：只有文本检索这一路，向量结果为空列表
        text_results = await text_coro
        vector_results = []

    memories = rrf_merge(text_results, vector_results, max_results * 2)

    # 两路检索都完全没有命中，但看起来是"要概览"式的宽泛提问时，
    # 退化为直接返回最近的记忆，而不是让调用方拿到一个空列表
    if not memories and is_context_overview_query(query):
        memories = await recent_context_fallback(
            backend._collection, user_id, max_results * 2, memory_types
        )

    # 只有候选数确实超过最终需要的数量时才值得重排（否则重排也不会改变最终返回集合）
    if enable_rerank and memories and len(memories) > max_results:
        memories = await rerank_candidates(query, memories, max_results)
    # 来源可信度排序放在重排之后执行，因此最终结果里"信任等级"优先于"相关性精排分"
    memories = prioritize_sources(memories)

    if memories:
        memories = memories[:max_results]
        # 相关性分数低于阈值的结果直接丢弃——宁可少返回也不返回不靠谱的记忆，
        # 因此这里返回的记忆条数可能小于 max_results
        min_score = getattr(settings, "NATIVE_MEMORY_RECALL_MIN_SCORE", 0.3)
        if min_score > 0:
            memories = [m for m in memories if m.get("score", 1.0) >= min_score]
        # 内容还原放在最后一步，只对真正会被返回的记忆做，避免为被淘汰的候选浪费开销
        memories = await _hydrate_memories_limited(backend, memories)

        if touch_access:
            # 只有"真实的"检索才计入访问统计，内部去重检测等场景会传 touch_access=False
            await backend._update_access_stats([m["memory_id"] for m in memories], user_id)

    return {
        "success": True,
        "query": query,
        "memories": memories,
        "search_mode": "hybrid" if backend._embedding_fn else "text",
    }

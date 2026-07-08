"""Summary and label helpers for the native memory backend."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 记忆需要展示用的 title/summary/tags 三件套，理想情况下交给 LLM 一次调用
# （llm_enrich_memory）统一生成；但 LLM 调用有延迟和失败概率，因此本模块也
# 配套提供一整套不依赖 LLM 的规则回退方案（build_summary/_fallback_title/
# _fallback_tags/_fallback_enrich），在 LLM 不可用、超时或解析失败时兜底，
# 保证记忆功能在没有可用模型的环境下也能完整运作（只是效果不如 LLM 精炼）。
# build_index_label 则是专门给"记忆索引"展示用的更短标签，刻意不调用 LLM，
# 因为索引本身条目多、调用频繁，追求的是零延迟的确定性输出。
# ============================================================================

from __future__ import annotations

import json
import logging
import warnings
from typing import Any

# 压制 jieba 内部正则转义序列在新版 Python 上触发的 SyntaxWarning，与本项目逻辑无关
with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)
    import jieba.posseg as pseg

from src.infra.async_utils import run_blocking_io
from src.infra.memory.client.native.models import CJK_STOPWORDS, has_cjk

logger = logging.getLogger(__name__)


def build_summary(content: str, max_len: int = 100) -> str:
    """Take the first sentence from content, supporting both CJK and English."""
    flat = content.replace("\n", " ").strip()

    # 依次尝试中英文常见的句末标点，找出"第一句话"结束的位置（取所有标记里
    # 最靠前出现的那个，即最早结束的一句），優先把第一句完整地作为摘要
    best_pos = len(flat)
    for marker in ("。", "！", "？", ". ", "! ", "? ", "；", "; "):
        pos = flat.find(marker)
        if pos != -1 and pos < best_pos:
            best_pos = pos + len(marker)

    first_sentence = flat[:best_pos].strip()
    if first_sentence and len(first_sentence) <= max_len:
        return first_sentence

    # 第一句话本身就超长（或没找到句末标点）时，退化为直接截断整段文本
    if len(flat) <= max_len:
        return flat
    if has_cjk(flat):
        # 中文按字符数截断即可，不存在"截断到单词中间"的问题
        return flat[:max_len].strip() + "..."
    # 英文尽量截到最后一个完整单词的边界（空格处），避免截断出半个单词；
    # 但如果最后一个空格离得太靠前（不到一半长度），说明这样截会丢太多内容，
    # 不如直接硬截断
    truncated = flat[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len // 2:
        return truncated[:last_space].strip() + "..."
    return truncated.strip() + "..."


def build_index_label(title: str, summary: str, content: str) -> str:
    """Build a compact deterministic label for memory indexes without extra LLM calls."""
    # 记忆索引条目多、构建频繁，这里刻意不调用 LLM，只是从已有的 title/summary/
    # content 里择优（依次尝试）取一个作为种子文本，再复用 build_summary 截到更短的
    # 25 字符，保证零延迟且结果确定（同样输入永远得到同样输出）
    seed = (title or summary or content).strip()
    if not seed:
        return ""
    return build_summary(seed, 25)


def _fallback_tags(content: str) -> list[str]:
    """Rule-based tag fallback when LLM is unavailable."""
    # 直接复用 classification.py 里基于 jieba 分词 + 词性过滤的规则式标签提取
    from src.infra.memory.client.native.classification import extract_tags

    return extract_tags(content)


# 强调"tags 应该是有意义的关键词，不是滑动字符窗口"，是因为如果不特别提示，
# 模型有时会把 tags 退化成类似 n-gram 的字符片段，失去关键词应有的语义价值；
# 同时要求用输入本身的语言输出，避免模型自作聪明把中文内容的标签翻译成英文
_ENRICH_SYSTEM = (
    "You are a memory tagging assistant. Respond with ONLY a JSON object, no markdown or explanation.\n"
    'Keys: "title" (max 25 chars), "summary" (max 80 chars), "tags" (array of 3-5 keyword strings).\n'
    "Tags should be meaningful keywords, NOT sliding character windows. Use the language of the input."
)


async def llm_enrich_memory(backend: Any, content: str) -> dict[str, Any]:
    """Single LLM call to extract title, summary, and tags together."""
    # 用一次 LLM 调用同时产出 title/summary/tags 三个字段，比分别调用三次更省成本；
    # 任何环节失败（模型调用异常、返回格式不对、JSON 解析失败）都统一兜底为
    # 纯规则的 _fallback_enrich，保证调用方总能拿到一个可用的结果
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        model = await backend._get_memory_model()
        response = await model.ainvoke(
            [
                SystemMessage(content=_ENRICH_SYSTEM),
                # 只截取内容前 500 字符喂给模型，摘要/标题/标签任务不需要看完整内容，
                # 这样能显著降低 token 消耗和延迟
                HumanMessage(content=f"Annotate this memory:\n\n{content[:500]}"),
            ],
        )
        text = response.content
        # 兼容多模态内容块列表的返回格式，取出其中的文本块；一个都找不到就直接走规则兜底
        if isinstance(text, list):
            for item in text:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    break
            else:
                return _fallback_enrich(content)

        # 去除模型常见的 ```json ... ``` 代码块围栏（注意 str.strip(chars) 是按
        # 字符集合逐个剥离两端字符，不是删除子串，这里之所以能生效是因为
        # 围栏两端出现的字符恰好都落在 "```json"/"```" 这两个字符集合里）
        text = str(text).strip().strip("```json").strip("```").strip()
        # JSON 解析属于 CPU 密集操作，丢线程池执行避免阻塞事件循环
        data = await run_blocking_io(json.loads, text)
        return {
            # 模型没给或给的是空字符串时，用规则方法（build_summary/_fallback_tags）兜底
            "title": str(data.get("title", ""))[:25] or build_summary(content, 25),
            "summary": str(data.get("summary", ""))[:100] or build_summary(content),
            "tags": [
                str(t) for t in (data.get("tags") or []) if isinstance(t, str) and len(t) >= 2
            ][:5]
            or _fallback_tags(content),
        }
    except Exception as e:
        logger.debug("[NativeMemory] LLM enrich failed, using fallback: %s", e)
        return _fallback_enrich(content)


def _fallback_title(content: str, summary: str) -> str:
    """Build a short title that differs from the summary — extract key nouns/phrases."""
    import re

    flat = content.replace("\n", " ").strip()
    if has_cjk(flat):
        # 逐个尝试常见的中文分句标点，clause 每次只在"找到的分隔符位置比当前
        # clause 还短"时才收缩，效果等价于取所有分隔符里最靠前出现的那个，
        # 即"第一个分句"作为候选文本
        clause = flat
        for sep in ("，", "。", "！", "？", "；", "、"):
            pos = flat.find(sep)
            if 2 < pos < len(clause):
                clause = flat[:pos]
        try:
            # 对第一分句做词性过滤分词，只挑名词/英文片段/动名词，取前 3 个拼接成标题，
            # 这样标题往往是几个关键名词的组合，而不是完整句子，与 summary 有所区分
            words = [
                (w, f)
                for w, f in pseg.cut(clause)
                if w.strip() and len(w) >= 2 and f in ("n", "nr", "ns", "nt", "nz", "eng", "vn")
            ][:3]
            if words:
                title = "".join(w for w, _ in words)
                return title[:25] if len(title) > 25 else title
        except Exception:
            pass
        # 分词失败或没挑出任何关键词，退化为直接摘取前 25 字符
        return build_summary(flat, 25)

    # 非中文：先把非字母数字字符替换成空格，方便后续按空白切词
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", flat)
    # 这里额外维护一份内联的英文停用词集合（与 models.py 的 STOPWORDS 内容
    # 大部分重叠但并非直接复用），叠加上 CJK_STOPWORDS 一起作为过滤基准
    stop: set[str] = set(CJK_STOPWORDS) | {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "and",
        "but",
        "or",
        "not",
        "this",
        "that",
        "it",
        "its",
        "i",
        "my",
        "me",
        "you",
        "your",
        "we",
        "our",
        "they",
        "their",
        "he",
        "she",
    }
    # 保留长度 >= 3 且不是停用词的词，取前 4 个拼接作为标题候选
    keyword_words: list[str] = [w for w in cleaned.split() if w.lower() not in stop and len(w) >= 3]
    title = " ".join(keyword_words[:4]) if keyword_words else ""
    if not title or len(title) < 3:
        return build_summary(flat, 25)
    if len(title) > 25:
        # 超长时改为逐词累加，直到再加一个词就会超过 25 字符为止，
        # 尽量保留完整单词而不是硬切断
        result = ""
        for w in keyword_words:
            candidate = f"{result} {w}".strip()
            if len(candidate) > 25:
                break
            result = candidate
        title = result or build_summary(flat, 25)
    # 如果拼出来的标题恰好和摘要的开头部分重复，说明这个标题没有额外信息量，
    # 改用 build_summary 重新生成，避免 title 与 summary 看起来像同一句话的重复展示
    if title == summary[: len(title)]:
        title = build_summary(flat, 25)
    return title


def _fallback_enrich(content: str) -> dict[str, Any]:
    """Rule-based fallback for all enrich fields."""
    # 纯规则链路：先出摘要，再基于摘要生成不重复的标题，最后独立提取标签，
    # 全程不依赖任何 LLM 调用
    summary = build_summary(content)
    title = _fallback_title(content, summary)
    return {
        "title": title,
        "summary": summary,
        "tags": _fallback_tags(content),
    }

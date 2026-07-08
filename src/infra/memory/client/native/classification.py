"""Classification helpers for the native memory backend."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 本模块回答两类问题：
#   1. "这段内容值不值得被记住？"——is_manual_memory_worthy（手动记忆的宽松校验）
#      / passes_lightweight_memory_filter（自动记忆候选的轻量前置过滤），
#      靠一组启发式规则（是否像代码/路径、是否是"正在做某事"的临时状态描述等）
#      在不调用 LLM 的前提下快速筛掉明显不值得记的内容；
#   2. "这条新记忆是不是和已有的记忆重复/该合并？"——word_similarity 及其上层的
#      deduplicate_against_existing / find_existing_memory_match，用 Jaccard
#      相似度（中文用字符 n-gram、英文用词集合）做轻量级文本相似度比较，
#      避免同一主题反复产生近似重复的记忆。
# 这些都是规则/统计方法而非 LLM，目的是把"明显能判断"的情况挡在真正需要
# LLM 参与（摘要生成、整合去重）之前，节省调用开销。
# ============================================================================

from __future__ import annotations

import re
import warnings
from typing import Any, Awaitable, Callable, Optional

# jieba 在较新 Python 版本上会因内部正则里的无效转义序列触发 SyntaxWarning，
# 这里只是压制这个与本项目逻辑无关的第三方库警告，不影响功能
with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)
    import jieba.posseg as pseg

from src.infra.memory.client.native.models import CJK_STOPWORDS, STOPWORDS, char_ngrams, has_cjk

# jieba 分词后认为"有实际意义、值得作为标签"的词性集合：
# n/nr/ns/nt/nz 是各类名词（普通名词/人名/地名/机构名/其他专名），
# v/vn 是动词/动名词，a 是形容词，eng 是夹杂的英文片段，x 是无法归类的其它符号；
# 助词、连词、代词等词性不在此列，天然被排除
_USEFUL_POS = frozenset({"n", "nr", "ns", "nt", "nz", "v", "vn", "a", "eng", "x"})

# 出现这些片段基本可以断定内容是代码/命令行输出/报错堆栈，不是值得长期记住的事实
_CODE_MARKERS = (
    "import ",
    "def ",
    "class ",
    "traceback",
    "exception:",
    "error:",
    "git ",
    "pip install",
    "npm install",
    "npm run",
    "src/",
    "node_modules",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
)

# 以这些短语开头，大概率是在描述"正在进行中的临时动作"，而非可长期沉淀的事实
_TRANSIENT_STARTS = (
    "正在",
    "现在",
    "刚刚",
    "我在看",
    "我在改",
    "我来",
    "让我",
    "准备",
    "先",
    "currently",
    "right now",
    "i am checking",
    "i'm checking",
    "i am looking",
    "i'm looking",
    "let me",
)

# 只要文本中任意位置出现这些片段，也倾向于判定为临时状态描述
_TRANSIENT_CONTAINS = (
    "看一下",
    "改一下",
    "查一下",
    "reading",
    "checking",
    "searching",
)


def word_similarity(a: str, b: str) -> float:
    """Jaccard similarity — character n-grams for CJK, word sets otherwise."""
    # 中文没有空格分词边界，退化用字符二元组集合近似代替"词集合"；
    # 英文等则直接按空白切分小写单词，两种情况下都用标准 Jaccard 公式：
    # 交集大小 / 并集大小
    if has_cjk(a) or has_cjk(b):
        set_a = char_ngrams(a, 2)
        set_b = char_ngrams(b, 2)
    else:
        set_a = set(a.lower().split())
        set_b = set(b.lower().split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def looks_like_code_or_path(content: str) -> bool:
    # 路径/URL 一类内容通常会出现多个斜杠，凭经验取 3 个作为判定门槛
    if content.count("/") + content.count("\\") >= 3:
        return True
    return any(marker in content for marker in _CODE_MARKERS)


def is_transient_status_content(content: str) -> bool:
    stripped = content.strip()
    return stripped.startswith(_TRANSIENT_STARTS) or any(
        marker in stripped.lower() for marker in _TRANSIENT_CONTAINS
    )


def passes_lightweight_memory_filter(content: str) -> bool:
    # 供"自动记忆"候选使用的轻量前置过滤：太短、像临时状态描述、像代码/路径的
    # 内容一律直接拒绝，不需要动用 LLM 就能筛掉这些明显不该记的候选
    if len(content) < 5:
        return False
    if is_transient_status_content(content):
        return False
    if looks_like_code_or_path(content):
        return False
    return True


def is_manual_memory_worthy(content: str, context: Optional[str] = None) -> bool:
    stripped = content.strip()
    if len(stripped) < 5:
        return False
    # For explicit manual retention, skip transient/code filters entirely
    # — the user explicitly chose to save this content.
    # context 标明是 project/reference 类记忆时直接放行，跳过临时状态/代码过滤：
    # 用户手动保存代码片段作为参考资料是合理场景，不应被"像代码"这条规则误拒
    if context and any(kw in context.lower() for kw in ("project", "reference")):
        return True
    if not passes_lightweight_memory_filter(stripped):
        return False
    return True


def extract_tags(content: str) -> list[str]:
    """Extract keyword tags using jieba for Chinese, whitespace for English."""
    tags: list[str] = []
    seen: set[str] = set()

    if has_cjk(content):
        cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", content)
        # jieba.posseg.cut 同时给出分词结果和词性标注，方便下面按词性过滤
        words = pseg.cut(cleaned)
        for word, flag in words:
            w = word.strip()
            # 过滤空串、停用词、单字词（单字通常语义太模糊不适合做标签）
            if not w or w in CJK_STOPWORDS or len(w) < 2:
                continue
            # 词性不在"有意义"的集合里（如助词、代词、连词）就丢弃
            if flag not in _USEFUL_POS:
                continue
            # 用 seen 集合去重，同时保留首次出现的顺序
            if w not in seen:
                tags.append(w)
                seen.add(w)
    else:
        # 非中文：没有分词器可用，简单按空白切分后去掉两侧标点、转小写，
        # 只保留长度 >= 3 且不是停用词的词作为标签
        for w in content.lower().split():
            clean = w.strip(".,!?;:()[]{}\"'").lower()
            if len(clean) >= 3 and clean not in STOPWORDS and clean not in seen:
                tags.append(clean)
                seen.add(clean)

    # 最多只保留前 5 个标签，避免标签列表过长喧宾夺主
    return tags[:5]


async def deduplicate_against_existing(
    fetch_recent: Callable[[str], Awaitable[list[dict[str, Any]]]],
    user_id: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # 从候选记忆列表里剔除掉那些与"近期已有记忆"过于相似的项，避免重复入库
    if not candidates:
        return candidates

    recent = await fetch_recent(user_id)
    recent_summaries = [doc["summary"] for doc in recent if doc.get("summary")]
    if not recent_summaries:
        return candidates

    filtered = []
    for mem in candidates:
        summary = mem.get("summary", "")
        # 候选没有 summary 时无法比较相似度，保守起见直接保留，不主动过滤掉
        if not summary:
            filtered.append(mem)
            continue
        # 只要和任意一条近期记忆的相似度超过阈值（中文文本用 0.55，英文用 0.6，
        # 因为字符 n-gram 的 Jaccard 分数分布与词级 Jaccard 不完全一致），
        # 就判定为重复，从候选中剔除
        if any(
            word_similarity(summary, rs) > (0.55 if has_cjk(summary + rs) else 0.6)
            for rs in recent_summaries
        ):
            continue
        filtered.append(mem)
    return filtered


async def find_existing_memory_match(
    fetch_recent: Callable[[str], Awaitable[list[dict[str, Any]]]],
    user_id: str,
    summary: str,
    memory_type: str,
) -> dict[str, Any] | None:
    # 在近期同类型记忆里找与新摘要最相似的一条，用于决定 retain() 该更新它
    # 而不是新建一条记忆；判重阈值比 deduplicate_against_existing 略低
    # （0.55/0.5 vs 0.55/0.6），因为这里追求的是"该不该合并更新"这类更宽松的判断，
    # 而不是"是否重复到该被直接丢弃"
    recent = await fetch_recent(user_id)
    best_match: dict[str, Any] | None = None
    best_score = 0.0
    threshold = 0.55 if has_cjk(summary) else 0.5

    for doc in recent:
        # 类型不同的记忆即使内容相似也不合并（如 project 记忆和 reference 记忆语义不同）
        if doc.get("memory_type") != memory_type:
            continue
        existing_summary = str(doc.get("summary") or "").strip()
        if not existing_summary:
            continue
        score = word_similarity(summary, existing_summary)
        # 遍历取相似度最高、且达到阈值的一条作为最终匹配结果
        if score >= threshold and score > best_score:
            best_score = score
            best_match = doc
    return best_match

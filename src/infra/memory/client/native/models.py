"""Shared models and constants for the native memory backend."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 原生记忆后端零外部依赖（不借助 Elasticsearch/专用向量库等），检索能力完全
# 靠本文件这几个基础工具函数自己实现：
#   - 停用词表（英文 STOPWORDS + 中文 CJK_STOPWORDS）：在做关键词匹配/打分前
#     过滤掉没有实际检索价值的高频虚词；
#   - char_ngrams：中文没有天然的空格分词边界，用字符级 n-gram（默认二元）
#     近似模拟词的重叠度，作为中文文本相似匹配的替代方案；
#   - cosine_similarity：对 embedding 向量做余弦相似度计算，用于语义检索排序。
# ============================================================================

from __future__ import annotations

import re

# 原生记忆统一存放在这个 MongoDB collection 里
COLLECTION_NAME = "native_memories"

# 英文停用词表：检索/打分前过滤掉的高频虚词（冠词、介词、连词等），
# 它们几乎不携带主题信息，保留会稀释关键词匹配的信号
STOPWORDS = frozenset(
    "the a an is are was were be been being have has had do does did will would "
    "could should may might can shall to of in for on with at by from as into "
    "through and but or not this that it its i my me you your we our they their "
    "he she his her also just very so if then when where what how which who "
    "there here about up out all some any no each every both few more most "
    "other some such only own same than too most".split()
)

# 中文停用词表，作用与 STOPWORDS 相同，但由于中文没有空格分词，
# 实际使用时通常配合字符级切分或分词结果一起过滤
CJK_STOPWORDS = frozenset(
    "的 了 是 在 和 与 也 都 就 要 会 能 有 这 那 一 不 个 吧 啊 呢 吗 呀 "
    "把 被 让 给 对 从 到 向 比 用 以 为 所 之 其 着 过 地 得 很 已 还 "
    "再 又 却 并 因为 所以 如果 但是 而且 或者 虽然 不过".split()
)


# 通过 Unicode 码位区间判断是否含有中日韩统一表意文字
def has_cjk(text: str) -> bool:
    """Check if text contains CJK characters."""
    # 检索时据此决定走英文分词还是中文 n-gram 两套不同的文本处理路径
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def char_ngrams(text: str, n: int = 2) -> set[str]:
    """Extract character n-grams from text, useful for Chinese similarity."""
    # 先去掉所有空白字符，避免因空格/换行打断本应连续的字符序列而漏掉合法的 n-gram
    cleaned = re.sub(r"\s+", "", text)
    if len(cleaned) < n:
        return set()
    # 滑动窗口切出所有连续 n 个字符的子串，用集合去重（只关心是否出现过，不关心次数），
    # 两段文本的 n-gram 集合重叠越多通常意味着内容越相近，借此弥补中文缺乏分词边界的问题
    return {cleaned[i : i + n] for i in range(len(cleaned) - n + 1)}


def cosine_similarity(a: list[float], b: list[float]) -> float:
    # 标准余弦相似度：两向量点积除以各自模长的乘积，用于比较两个 embedding 的语义接近程度
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    # 任一向量为零向量时模长为 0，直接返回 0 相似度，避免除零异常
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

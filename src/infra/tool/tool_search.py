"""
加权关键词搜索引擎 — 移植自 Claude Code 的 ToolSearchTool。

分层评分：
  名称精确匹配  10-12 分  (MCP 工具 +20%)
  名称部分匹配   5- 6 分
  hint(首行描述)  4 分
  描述全文匹配   2 分
  +term 必选词预过滤
"""

from __future__ import annotations

import re
import weakref
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from src.infra.logging import get_logger

logger = get_logger(__name__)

# Module-level cache keyed by object id with a weakref finalizer, so transient
# tool objects do not accumulate in long-lived worker processes.
# 按对象 id 缓存解析结果，并用弱引用终结器在工具对象被回收时自动清理，
# 避免长驻进程中临时工具对象堆积造成内存泄漏
_parse_cache: dict[int, tuple[weakref.ReferenceType[BaseTool], "_ParsedTool"]] = {}


def _normalize_search_text(value: str) -> str:
    """Normalize separators so `web_search`, `web-search`, and `web search` match alike."""
    # 把下划线/冒号/连字符/空白统一折叠成单个空格并转小写，
    # 使不同分隔风格的名称能被同等匹配
    return re.sub(r"[_:\-\s]+", " ", value).strip().lower()


@dataclass
class ToolSearchResult:
    """搜索结果"""

    name: str
    description: str
    score: float
    tool: "BaseTool"


@dataclass
class _ParsedTool:
    """工具解析缓存"""

    # 原始工具名
    name: str
    # 小写全名
    full: str
    # 归一化后的全名（分隔符折叠为空格）
    normalized_full: str
    # 名称按分隔符切分后的词片段列表，用于逐词精确/部分匹配
    parts: list[str]
    # hint：描述首行（小写），权重高于全文
    hint: str
    # 全文描述（小写）
    desc: str
    # 是否为 MCP 工具：命中时分数上调（业务上更希望优先命中 MCP 工具）
    is_mcp: bool


def _parse_tool(tool: "BaseTool") -> _ParsedTool:
    """将 LangChain BaseTool 解析为搜索用结构（带模块级缓存）"""
    # 以对象 id 作为缓存键；由于 id 可能被回收后复用，需再用弱引用校验是否仍是同一对象
    tid = id(tool)
    cached = _parse_cache.get(tid)
    if cached is not None:
        cached_ref, cached_parsed = cached
        # 弱引用仍指向同一对象才算命中，否则视为 id 复用，丢弃旧缓存
        if cached_ref() is tool:
            return cached_parsed
        _parse_cache.pop(tid, None)
    name = tool.name
    desc = getattr(tool, "description", "") or ""
    # hint 取描述首行，通常是最精炼的能力概述
    hint = desc.split("\n")[0].strip()
    pt = _ParsedTool(
        name=name,
        full=name.lower(),
        normalized_full=_normalize_search_text(name),
        parts=name.replace("_", " ").replace("-", " ").replace(":", " ").lower().split(),
        hint=hint.lower(),
        desc=desc.lower(),
        # 带 server 属性或以 "mcp" 开头即认为是 MCP 工具
        is_mcp=getattr(tool, "server", "") != "" or name.startswith("mcp"),
    )
    try:

        # 终结器：工具对象被 GC 时自动从缓存移除对应条目
        def _on_finalize(_wref: weakref.ReferenceType[BaseTool], _tid: int = tid) -> None:
            _parse_cache.pop(_tid, None)

        ref = weakref.ref(tool, _on_finalize)
        if ref is None:
            return pt
        tool_ref: weakref.ReferenceType[BaseTool] = ref
    except TypeError:
        # 某些对象不支持弱引用：直接返回解析结果，不写入缓存
        return pt
    _parse_cache[tid] = (tool_ref, pt)
    return pt


def _compile_term_patterns(terms: list[str]) -> list[tuple[str, str, re.Pattern[str]]]:
    """编译搜索词为 (原始词, 小写词, 词边界正则) 列表"""
    patterns: list[tuple[str, str, re.Pattern[str]]] = []
    for term in terms:
        term_lower = _normalize_search_text(term)
        try:
            # 用 \b 词边界正则做整词匹配，避免 "cat" 命中 "category"
            patterns.append(
                (term, term_lower, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
            )
        except re.error:
            # Fallback: plain substring
            # 正则编译失败（异常字符）时退化为普通子串匹配
            patterns.append((term, term_lower, re.compile(re.escape(term), re.IGNORECASE)))
    return patterns


def search_tools_with_keywords(
    query: str,
    tools: list["BaseTool"],
    max_results: int = 10,
    min_score: float = 2.0,
) -> list[ToolSearchResult]:
    """
    加权关键词搜索，移植自 Claude Code 的 searchToolsWithKeywords()。

    Args:
        query: 搜索关键词，支持 +term 必选语法和 select:A,B 直接选择
        tools: 待搜索的工具列表
        max_results: 最大返回数量
        min_score: 最低分数阈值

    Returns:
        按分数降序排列的搜索结果
    """
    if not query.strip() or not tools:
        return []

    query_lower = query.strip().lower()

    # select:ToolA,ToolB 直接选择语法
    # 特殊语法：显式点名选择工具，绕过评分，直接给最高分返回
    if query_lower.startswith("select:"):
        names_str = query_lower[len("select:") :]
        target_names = {n.strip() for n in names_str.split(",") if n.strip()}
        results: list[ToolSearchResult] = []
        for tool in tools:
            if tool.name.lower() in target_names:
                results.append(
                    ToolSearchResult(
                        name=tool.name,
                        description=getattr(tool, "description", "") or "",
                        score=100.0,
                        tool=tool,
                    )
                )
        return results[:max_results]

    # 解析搜索词，分离必选词（+term）
    # +前缀词既是必选词（用于预过滤），也参与后续评分
    raw_terms: list[str] = query.split()
    required_terms: list[str] = []
    search_terms: list[str] = []
    for term in raw_terms:
        if term.startswith("+"):
            required_terms.append(term[1:])
            search_terms.append(term[1:])
        else:
            search_terms.append(term)

    if not search_terms:
        return []

    # 编译正则
    compiled = _compile_term_patterns(search_terms)
    required_compiled = _compile_term_patterns(required_terms)

    # 解析所有工具（缓存友好）
    parsed_tools = [(tool, _parse_tool(tool)) for tool in tools]

    # 必选词预过滤
    # 只保留"每个必选词都能在名称/hint/描述任一处命中"的工具，再进入评分
    candidates: list[tuple["BaseTool", _ParsedTool]] = []
    for tool, pt in parsed_tools:
        if not required_compiled:
            candidates.append((tool, pt))
            continue
        all_match = True
        for _term, _tl, pattern in required_compiled:
            # 归一化子串或词边界正则任一命中即算该必选词满足
            if (
                _tl not in pt.normalized_full
                and not pattern.search(pt.full)
                and not pattern.search(pt.hint)
                and not pattern.search(pt.desc)
            ):
                all_match = False
                break
        if all_match:
            candidates.append((tool, pt))

    # 评分
    # 分层加权：名称精确 > 名称部分 > 全名回退 > hint > 描述；MCP 工具整体乘 1.2
    scored: list[ToolSearchResult] = []
    for tool, pt in candidates:
        score = 0.0
        mcp_mult = 1.2 if pt.is_mcp else 1.0

        for term, term_lower, pattern in compiled:
            # 名称精确匹配（整个 part 等于 term）
            # 最高权重：某个名称片段与搜索词完全相等
            if term_lower in pt.parts:
                score += 12 if pt.is_mcp else 10
            elif any(term_lower in part for part in pt.parts):
                # 次高权重：搜索词是某个名称片段的子串
                score += 6 if pt.is_mcp else 5

            # 全名回退（低权重）
            # 仅在前面未得分时兜底：搜索词出现在全名任意位置
            if score == 0 and (term_lower in pt.normalized_full or term_lower in pt.full):
                score += 3

            # hint 匹配（词边界）
            if pattern.search(pt.hint):
                score += 4

            # 描述匹配（词边界）
            if pattern.search(pt.desc):
                score += 2

        # MCP 工具整体加成
        score *= mcp_mult
        # 低于阈值的结果直接丢弃
        if score >= min_score:
            scored.append(
                ToolSearchResult(
                    name=pt.name,
                    description=getattr(tool, "description", "") or "",
                    score=round(score, 1),
                    tool=tool,
                )
            )

    # 按分数降序，截断到 max_results
    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:max_results]

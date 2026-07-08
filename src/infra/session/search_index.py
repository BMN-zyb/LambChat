"""Helpers for session search indexing and match previews."""

from __future__ import annotations

import re
from dataclasses import dataclass

# \u641C\u7D22\u7D22\u5F15\u7ED3\u6784\u7248\u672C\u53F7\uFF1A\u5206\u8BCD/\u7D22\u5F15\u7B56\u7565\u53D8\u66F4\u65F6\u9012\u589E\uFF0C\u7528\u4E8E\u8BC6\u522B\u5E76\u56DE\u586B\u8FC7\u671F\u7D22\u5F15
SESSION_SEARCH_INDEX_VERSION = 3
# \u5355\u4E2A\u4F1A\u8BDD\u6700\u591A\u4FDD\u7559\u7684\u68C0\u7D22\u8BCD\u6570\u91CF\uFF0C\u9632\u6B62 term \u6570\u7EC4\u65E0\u9650\u81A8\u80C0
MAX_SESSION_SEARCH_TERMS = 4096
# \u4F1A\u8BDD\u5168\u6587\u9884\u89C8\u6587\u672C\u7684\u5B57\u7B26\u4E0A\u9650\uFF0C\u63A7\u5236\u6587\u6863\u4F53\u79EF
MAX_SESSION_SEARCH_TEXT_CHARS = 24000
# \u641C\u7D22\u547D\u4E2D\u9884\u89C8\u7247\u6BB5\u7684\u6700\u5927\u5B57\u7B26\u6570
MAX_PREVIEW_CHARS = 160

# \u5206\u8BCD\u6B63\u5219\uFF1A\u5339\u914D\u62C9\u4E01\u5B57\u6BCD/\u6570\u5B57/\u4E0B\u5212\u7EBF\u8FDE\u7EED\u4E32\uFF0C\u6216 CJK\uFF08\u4E2D\u65E5\u97E9\uFF09\u6C49\u5B57\u8FDE\u7EED\u4E32
_WORD_OR_CJK_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+")
# \u8FDE\u7EED\u7A7A\u767D\u6298\u53E0\u6B63\u5219
_WHITESPACE_RE = re.compile(r"\s+")


# \u4F1A\u8BDD\u641C\u7D22\u7D22\u5F15\u8F7D\u8377\uFF1A\u4E00\u6B21\u6027\u6253\u5305\u5F85\u6301\u4E45\u5316\u7684\u5404\u7C7B\u68C0\u7D22\u5B57\u6BB5\uFF0Cfrozen \u4FDD\u8BC1\u4E0D\u53EF\u53D8
@dataclass(frozen=True)
class SessionSearchIndexPayload:
    # \u4F1A\u8BDD\u540D\u79F0\u62C6\u51FA\u7684\u68C0\u7D22\u8BCD
    name_search_terms: list[str]
    # \u7528\u6237\u6D88\u606F\u7D2F\u79EF\u7684\u68C0\u7D22\u8BCD
    message_search_terms: list[str]
    # \u540D\u79F0\u8BCD + \u6D88\u606F\u8BCD\u5408\u5E76\u53BB\u91CD\u540E\u7684\u6700\u7EC8\u68C0\u7D22\u8BCD\uFF08\u5B9E\u9645\u7528\u4E8E\u67E5\u8BE2\u5339\u914D\uFF09
    search_terms: list[str]
    # \u7528\u4E8E\u751F\u6210\u9884\u89C8\u7247\u6BB5\u7684\u5168\u6587\u6587\u672C
    search_text: str
    # \u6700\u8FD1\u4E00\u6761\u7528\u6237\u6D88\u606F\uFF08\u89C4\u8303\u5316\u540E\uFF09
    latest_user_message: str
    # \u751F\u6210\u8BE5\u8F7D\u8377\u65F6\u7684\u7D22\u5F15\u7248\u672C\u53F7
    search_index_version: int = SESSION_SEARCH_INDEX_VERSION


def normalize_search_text(text: str | None) -> str:
    """Collapse whitespace while keeping user-visible wording intact."""
    # \u6298\u53E0\u8FDE\u7EED\u7A7A\u767D\u4E3A\u5355\u4E2A\u7A7A\u683C\u5E76\u53BB\u9664\u9996\u5C3E\u7A7A\u767D\uFF0C\u4FDD\u7559\u539F\u59CB\u53EF\u89C1\u6587\u5B57
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip()


def build_search_terms(text: str | None) -> list[str]:
    """Build compact search terms that work for latin substrings and CJK phrases."""
    # \u6784\u5EFA"\u5165\u5E93"\u7528\u68C0\u7D22\u8BCD\uFF1A\u540C\u65F6\u8986\u76D6\u62C9\u4E01\u5B50\u4E32\u4E0E CJK \u77ED\u8BED\uFF0C\u517C\u987E\u53EC\u56DE
    normalized = normalize_search_text(text)
    if not normalized:
        return []

    terms: list[str] = []
    seen: set[str] = set()

    # \u5185\u90E8\u53BB\u91CD\u6536\u96C6\u5668\uFF1A\u7EDF\u4E00\u5C0F\u5199\u3001\u53BB\u7A7A\u767D\uFF0C\u91CD\u590D\u7684\u8DF3\u8FC7
    def add(term: str) -> None:
        clean = term.strip().lower()
        if not clean or clean in seen:
            return
        seen.add(clean)
        terms.append(clean)

    for match in _WORD_OR_CJK_RE.finditer(normalized):
        token = match.group(0)
        if token.isascii():
            # \u62C9\u4E01\u8BCD\uFF1A\u6574\u8BCD\u5165\u5E93\uFF1B\u957F\u5EA6 >=4 \u65F6\u518D\u5207 3-gram\uFF0C\u652F\u6301\u5B50\u4E32\u68C0\u7D22
            lowered = token.lower()
            add(lowered)
            if len(lowered) >= 4:
                for index in range(len(lowered) - 2):
                    add(lowered[index : index + 3])
        else:
            # CJK \u4E32\uFF1A\u6574\u4E32 + \u5355\u5B57 + \u76F8\u90BB\u4E8C\u5B57\u7EC4\u5408\uFF08bigram\uFF09\uFF0C\u517C\u987E\u6574\u8BCD\u4E0E\u77ED\u8BED\u5339\u914D
            add(token)
            if len(token) == 1:
                continue
            for char in token:
                add(char)
            for index in range(len(token) - 1):
                add(token[index : index + 2])

    # \u622A\u65AD\u5230\u4E0A\u9650\uFF0C\u907F\u514D\u8D85\u957F
    return terms[:MAX_SESSION_SEARCH_TERMS]


def build_search_query_terms(text: str | None) -> list[str]:
    """Build query terms optimized for substring-style matching."""
    # \u6784\u5EFA"\u67E5\u8BE2"\u7528\u68C0\u7D22\u8BCD\uFF1A\u4E0E\u5165\u5E93\u7B56\u7565\u5BF9\u9F50\uFF0C\u4F46\u62C9\u4E01\u957F\u8BCD\u53EA\u4FDD\u7559 3-gram \u4EE5\u505A\u5B50\u4E32\u5339\u914D
    normalized = normalize_search_text(text)
    if not normalized:
        return []

    terms: list[str] = []
    seen: set[str] = set()

    # \u540C\u6837\u7684\u53BB\u91CD\u6536\u96C6\u5668
    def add(term: str) -> None:
        clean = term.strip().lower()
        if not clean or clean in seen:
            return
        seen.add(clean)
        terms.append(clean)

    for match in _WORD_OR_CJK_RE.finditer(normalized):
        token = match.group(0)
        if token.isascii():
            lowered = token.lower()
            # \u77ED\u62C9\u4E01\u8BCD\uFF08<4\uFF09\u76F4\u63A5\u6574\u8BCD\u67E5\u8BE2
            if len(lowered) < 4:
                add(lowered)
                continue
            # \u957F\u62C9\u4E01\u8BCD\u53EA\u7528 3-gram\uFF0C\u4E0E\u5165\u5E93\u7AEF\u7684\u5B50\u4E32\u7D22\u5F15\u5BF9\u5E94
            for index in range(len(lowered) - 2):
                add(lowered[index : index + 3])
            continue

        # CJK \u5355\u5B57\u76F4\u63A5\u67E5\u8BE2
        if len(token) == 1:
            add(token)
            continue
        # CJK \u591A\u5B57\uFF1A\u5355\u5B57 + bigram
        for char in token:
            add(char)
        for index in range(len(token) - 1):
            add(token[index : index + 2])

    return terms[:MAX_SESSION_SEARCH_TERMS]


def build_search_preview(search_text: str | None, query: str | None) -> str | None:
    """Extract a short preview snippet for the current search query."""
    # 为当前查询从全文里截取一段带上下文的高亮预览
    normalized_query = normalize_search_text(query)
    if search_text:
        # 优先逐行匹配：命中的整行直接作为预览返回
        for raw_line in search_text.splitlines():
            normalized_line = normalize_search_text(raw_line)
            if not normalized_line:
                continue
            if _find_match_start(normalized_line, normalized_query) != -1:
                return normalized_line[:MAX_PREVIEW_CHARS]

    normalized_text = normalize_search_text(search_text)
    if not normalized_text:
        return None
    # 无查询词时返回全文开头片段
    if not normalized_query:
        return normalized_text[:MAX_PREVIEW_CHARS]

    # 整体文本里定位命中位置，未命中返回 None
    start = _find_match_start(normalized_text, normalized_query)
    if start == -1:
        return None

    # 以命中片段为中心，向前留 32 字符、向后留 96 字符构成窗口
    query_match = _find_match_token(normalized_text, normalized_query) or normalized_query
    end = start + len(query_match)
    window_start = max(0, start - 32)
    window_end = min(len(normalized_text), end + 96)
    snippet = normalized_text[window_start:window_end].strip()
    # 窗口非首尾时补省略号提示还有上下文
    if window_start > 0:
        snippet = f"...{snippet}"
    if window_end < len(normalized_text):
        snippet = f"{snippet}..."
    return snippet[:MAX_PREVIEW_CHARS]


def compose_session_search_index(
    *,
    session_name: str | None,
    message_search_terms: list[str] | None,
    search_text: str | None,
    latest_user_message: str | None,
) -> SessionSearchIndexPayload:
    """Compose the persisted session-side search document."""
    # 组装完整的会话搜索载荷：名称词 + 消息词合并，并把最新消息追加进全文
    name_terms = build_search_terms(session_name)
    message_terms = _truncate_terms(message_search_terms or [])
    combined_terms = _truncate_terms([*name_terms, *message_terms])
    normalized_latest = normalize_search_text(latest_user_message)

    return SessionSearchIndexPayload(
        name_search_terms=name_terms,
        message_search_terms=message_terms,
        search_terms=combined_terms,
        search_text=_append_search_text(search_text, normalized_latest),
        latest_user_message=normalized_latest,
    )


def append_message_to_search_index(
    *,
    session_name: str | None,
    existing_message_search_terms: list[str] | None,
    existing_search_text: str | None,
    latest_user_message: str | None,
) -> SessionSearchIndexPayload:
    """Update session-side search data with one more user message."""
    # 增量：把新一条用户消息的检索词并入已有词表，并重组载荷
    normalized_latest = normalize_search_text(latest_user_message)
    added_terms = build_search_terms(normalized_latest)
    merged_terms = _truncate_terms([*(existing_message_search_terms or []), *added_terms])
    return compose_session_search_index(
        session_name=session_name,
        message_search_terms=merged_terms,
        search_text=existing_search_text,
        latest_user_message=normalized_latest,
    )


def build_backfilled_search_index(
    *,
    session_name: str | None,
    user_messages: list[str],
) -> SessionSearchIndexPayload:
    """Build a full search index for an existing session from stored user messages."""
    # 回填：从历史所有用户消息一次性重建整份索引
    normalized_messages = [
        normalized for message in user_messages if (normalized := normalize_search_text(message))
    ]
    message_terms: list[str] = []
    for message in normalized_messages:
        message_terms.extend(build_search_terms(message))

    # 全文取除最后一条外的历史行，最后一条单独作为 latest_user_message
    return compose_session_search_index(
        session_name=session_name,
        message_search_terms=message_terms,
        search_text="\n".join(normalized_messages[:-1]),
        latest_user_message=normalized_messages[-1] if normalized_messages else "",
    )


def merge_search_state(
    *,
    session_name: str | None,
    base_message_terms: list[str] | None,
    base_search_text: str | None,
    base_latest_user_message: str | None,
    extra_message_terms: list[str] | None,
    extra_search_text: str | None,
    extra_latest_user_message: str | None,
) -> SessionSearchIndexPayload:
    """Merge two session search states without dropping newer live content."""
    # 合并两份搜索状态（base=回填结果，extra=期间实时新增），确保新内容不被覆盖丢失
    merged_terms = _truncate_terms([*(base_message_terms or []), *(extra_message_terms or [])])

    # 分别取出两边"不含最新消息"的历史行，避免最新消息被重复计入
    base_history_lines = _normalize_search_lines(
        _normalize_search_text_without_latest(base_search_text, base_latest_user_message)
    )
    extra_history_lines = _normalize_search_lines(
        _normalize_search_text_without_latest(extra_search_text, extra_latest_user_message)
    )
    base_latest = normalize_search_text(base_latest_user_message)
    extra_latest = normalize_search_text(extra_latest_user_message)
    # 以实时侧的最新消息优先
    latest = extra_latest or base_latest

    # 用重叠检测拼接历史行，避免边界重复
    history_lines = _merge_search_lines(base_history_lines, extra_history_lines)
    # 若 base 的最新消息已不再是全局最新，则把它补回历史行，避免丢失
    if (
        base_latest
        and base_latest != latest
        and (not history_lines or history_lines[-1] != base_latest)
    ):
        history_lines.append(base_latest)
    merged_text = _join_search_lines(history_lines)

    return compose_session_search_index(
        session_name=session_name,
        message_search_terms=merged_terms,
        search_text=merged_text,
        latest_user_message=latest,
    )


def _truncate_terms(terms: list[str]) -> list[str]:
    # 去重（小写、去空白）并截断到检索词上限
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = term.strip().lower()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
        if len(deduped) >= MAX_SESSION_SEARCH_TERMS:
            break
    return deduped


def _append_search_text(existing: str | None, latest: str) -> str:
    # 把最新消息作为新的一行追加到全文尾部
    lines = _normalize_search_lines(existing)
    if latest:
        lines.append(latest)
    return _join_search_lines(lines)


def _normalize_search_text_without_latest(search_text: str | None, latest: str | None) -> str:
    # 规范化全文并去掉末尾那条与 latest 相同的行（防止最新消息重复统计）
    lines = _normalize_search_lines(search_text)
    normalized_latest = normalize_search_text(latest)
    if not lines or not normalized_latest:
        return _join_search_lines(lines)
    if lines[-1] == normalized_latest:
        lines = lines[:-1]
    return _join_search_lines(lines)


def _normalize_search_lines(text: str | None) -> list[str]:
    # 按行切分并逐行规范化，丢弃空行
    if not text:
        return []
    return [
        normalized
        for raw_line in text.splitlines()
        if (normalized := normalize_search_text(raw_line))
    ]


def _merge_search_lines(base_lines: list[str], extra_lines: list[str]) -> list[str]:
    # 拼接两段历史行，并去除 base 尾部与 extra 头部的最大重叠段，避免重复
    if not base_lines:
        return list(extra_lines)
    if not extra_lines:
        return list(base_lines)

    # 从最大可能重叠长度向下寻找 base 后缀 == extra 前缀 的重叠
    max_overlap = min(len(base_lines), len(extra_lines))
    overlap = 0
    for size in range(max_overlap, 0, -1):
        if base_lines[-size:] == extra_lines[:size]:
            overlap = size
            break
    return [*base_lines, *extra_lines[overlap:]]


def _join_search_lines(lines: list[str]) -> str:
    # 从最新行往前拼接全文，直到达到字符上限，保证保留最新内容
    if not lines:
        return ""

    kept: list[str] = []
    total_chars = 0
    # 逆序遍历（最新的先保留），换行符按 1 个分隔符计入长度
    for line in reversed(lines):
        line_len = len(line)
        separator_len = 1 if kept else 0
        if total_chars + separator_len + line_len > MAX_SESSION_SEARCH_TEXT_CHARS:
            # 已保留内容达到上限则停止；若单行本身超限则截取其末尾片段
            if kept:
                break
            return line[-MAX_SESSION_SEARCH_TEXT_CHARS:]
        kept.append(line)
        total_chars += separator_len + line_len
    # kept 是逆序的，还原为正序输出
    return "\n".join(reversed(kept))


def _find_match_start(text: str, query: str) -> int:
    # 返回查询在文本中的起始下标（大小写不敏感），未命中返回 -1
    match = _find_match_token(text, query)
    if not match:
        return -1
    return text.lower().find(match.lower())


def _find_match_token(text: str, query: str) -> str | None:
    # 寻找可命中的片段：优先整串匹配，否则退化为查询中最长的能命中的分词
    text_lower = text.lower()
    query_lower = query.lower()
    if query_lower and query_lower in text_lower:
        return query

    # 把查询拆成词/CJK 片段，按长度从长到短尝试，返回第一个命中的
    segments = [segment.group(0) for segment in _WORD_OR_CJK_RE.finditer(query)]
    segments.sort(key=len, reverse=True)
    for segment in segments:
        if segment.lower() in text_lower:
            return segment
    return None

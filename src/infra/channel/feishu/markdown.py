"""
Feishu Markdown adapter for converting standard Markdown to Feishu card elements.

飞书卡片 markdown 标签支持的语法:
- **粗体** / *斜体* / ~~删除线~~
- `行内代码`
- ```代码块```（原生支持，含语法高亮）
- [链接](url)
- 引用块 (> )
- 有序/无序列表

不支持（需要转换）:
- 标题 (#)（转换为粗体）
- 表格（转换为飞书 table 组件）
- 图片 (![alt](url))（需要用 img 元素）
"""

import re
from typing import Any

# 匹配 markdown 表格（表头 + 分隔行 + 数据行）
_TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
    re.MULTILINE,
)

# 匹配代码块（```...```，跨行，非贪婪），用于在处理表格/标题前先把代码块"挖走"暂存，
# 避免代码块内部恰好含有 | 或 # 之类字符时被表格正则或标题正则误伤
_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)")

# 匹配 send:// 图片 URI（markdown 格式: ![alt](send://...) 和裸格式: send://...）
_SEND_IMAGE_MD_RE = re.compile(r"!\[([^\]]*)\]\((send://[^)\s]+\.(?:png|jpeg|jpg|gif|bmp|webp))\)")
_SEND_IMAGE_RAW_RE = re.compile(r"(send://[^)\s]+\.(?:png|jpeg|jpg|gif|bmp|webp))\)?")


def _parse_md_table(table_text: str) -> dict | None:
    """将 markdown 表格解析为飞书 table 组件元素"""
    # 至少要有表头行 + 分隔行 + 一行数据，否则不构成合法表格
    lines = [line.strip() for line in table_text.strip().split("\n") if line.strip()]
    if len(lines) < 3:
        return None

    # 按 | 切分一行，并去掉首尾多余的 | 与空白，得到每个单元格的纯文本
    def split_row(line: str) -> list[str]:
        return [c.strip() for c in line.strip("|").split("|")]

    # 第 0 行是表头，第 1 行是分隔行（如 |---|---|，直接跳过不用），第 2 行起才是数据
    headers = split_row(lines[0])
    rows = [split_row(line) for line in lines[2:]]
    # 飞书 table 组件要求每列声明 name/display_name，这里用列序号 c0/c1/... 作为内部字段名
    columns = [
        {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
        for i, h in enumerate(headers)
    ]
    return {
        "tag": "table",
        "page_size": len(rows) + 1,
        "columns": columns,
        # 每行数据按列名组装成字典；某行单元格数少于表头列数时用空字符串补齐，防止越界
        "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
    }


class FeishuMarkdownAdapter:
    """将标准 Markdown 转换为飞书卡片 elements 列表

    - 普通文本/代码块 → {"tag": "markdown", "content": ...}
    - 表格 → {"tag": "table", ...}（飞书 markdown 不支持表格语法）
    - 标题 → 转为粗体（飞书 markdown 不支持 # 标题语法）
    """

    @classmethod
    def build_elements(cls, text: str) -> list[dict]:
        """将 markdown 文本转换为飞书卡片 elements 列表

        返回多个元素：markdown 文本块 + table 组件，按原文顺序排列。
        """
        if not text:
            return []

        # 1. 保护代码块，避免内部内容被表格正则误匹配
        protected, code_blocks = cls._protect_code_blocks(text)

        # 2. 按表格拆分内容，生成 elements
        elements = []
        last_end = 0
        table_count = 0
        max_tables = 5  # 飞书卡片表格数量限制

        for m in _TABLE_RE.finditer(protected):
            # 表格前的文本
            before = protected[last_end : m.start()]
            if before.strip():
                before = cls._restore_code_blocks(before, code_blocks)
                elements.extend(cls._text_to_elements(before))

            # 表格本身
            table_text = cls._restore_code_blocks(m.group(1), code_blocks)
            if table_count < max_tables:
                # 未超限：优先尝试结构化解析为 table 组件；解析失败（如列数不一致）则退化为纯文本
                table_el = _parse_md_table(table_text)
                if table_el:
                    elements.append(table_el)
                else:
                    elements.append({"tag": "markdown", "content": cls._adapt_text(table_text)})
                table_count += 1
            else:
                # 超出表格限制，降级为 markdown 文本
                elements.append({"tag": "markdown", "content": cls._adapt_text(table_text)})

            # 推进游标到本次匹配表格的结尾，下一轮从这里继续找文本/表格
            last_end = m.end()

        # 剩余文本
        remaining = protected[last_end:]
        if remaining.strip():
            remaining = cls._restore_code_blocks(remaining, code_blocks)
            elements.extend(cls._text_to_elements(remaining))

        # 全文没有产出任何 element（例如整段都是空白）时，兜底返回原文本的单个 markdown 元素
        return elements or [{"tag": "markdown", "content": text.strip()}]

    @classmethod
    def adapt(cls, text: str) -> str:
        """简单适配：仅处理标题和段落间距（向后兼容）"""
        if not text:
            return text
        return cls._adapt_text(text)

    @classmethod
    async def build_elements_with_images(cls, text: str, image_uploader: Any) -> list[dict]:
        """将 markdown 文本转换为飞书卡片 elements，支持 send:// 图片上传嵌入。

        Args:
            text: markdown 文本，可能包含 send://... 图片 URI
            image_uploader: 异步回调 async (uri: str) -> str|None，
                            接收图片 URI 字符串返回飞书 image_key

        Returns:
            飞书卡片 elements 列表（含 img 元素）
        """
        if not text:
            return []

        # 1. 提取所有 send:// 图片 URI
        # 先匹配标准 markdown 图片语法 ![alt](send://...)，再匹配裸 URI（无 ![]() 包裹的情形），
        # 两种写法都可能出现在文本里，用 image_uris 去重合并，保证同一张图不会被重复上传
        image_uris: list[str] = []
        for m in _SEND_IMAGE_MD_RE.finditer(text):
            image_uris.append(m.group(2))
        for m in _SEND_IMAGE_RAW_RE.finditer(text):
            uri = m.group(1)
            if uri not in image_uris:
                image_uris.append(uri)

        # 2. 从文本中移除 send:// 图片引用
        # 图片会被单独转换为 img 元素追加在末尾，原文里的引用需要清空，否则正文里会残留裸链接
        cleaned = _SEND_IMAGE_MD_RE.sub("", text)
        cleaned = _SEND_IMAGE_RAW_RE.sub("", cleaned)

        # 3. 上传图片到飞书
        # 逐个调用外部传入的异步上传回调；单张图片上传失败不影响其它图片和正文，直接跳过
        image_elements: list[dict] = []
        for uri in image_uris:
            try:
                image_key = await image_uploader(uri)
                if image_key:
                    image_elements.append({"tag": "img", "img_key": image_key})
            except Exception:
                pass  # Skip failed uploads

        # 4. 对剩余文本构建普通 elements
        text_elements = cls.build_elements(cleaned.strip())

        # 5. 合并：文本 elements + 图片 elements
        return text_elements + image_elements

    @classmethod
    def _adapt_text(cls, text: str) -> str:
        """对文本做飞书兼容适配：标题转粗体 + 清理空行"""
        # 同样需要先保护代码块，避免标题正则（# 开头）误伤代码块里以 # 开头的内容（如注释）
        text, code_blocks = cls._protect_code_blocks(text)
        text = cls._convert_headers(text)
        text = cls._fix_paragraphs(text)
        text = cls._restore_code_blocks(text, code_blocks)
        return text.strip()

    @classmethod
    def _text_to_elements(cls, text: str) -> list[dict]:
        """将文本段转为 markdown elements（处理标题转粗体）"""
        adapted = cls._adapt_text(text)
        # 适配后可能变成空字符串（如原文本全是空行），此时不生成任何元素
        if adapted:
            return [{"tag": "markdown", "content": adapted}]
        return []

    @classmethod
    def _protect_code_blocks(cls, text: str) -> tuple[str, list[str]]:
        """提取代码块用占位符保护"""
        code_blocks: list[str] = []

        # 把匹配到的整段代码块原文存进列表，原地替换成一个不可能出现在正文里的
        # \x00CODEBLOCK_i\x00 占位符（\x00 是空字符，普通 markdown 文本不会包含），
        # 后续所有基于正则的处理（标题转换、表格匹配等）都不会再触碰代码块内容
        def replace_block(match: re.Match) -> str:
            code_blocks.append(match.group(0))
            return f"\x00CODEBLOCK_{len(code_blocks) - 1}\x00"

        text = _CODE_BLOCK_RE.sub(replace_block, text)
        return text, code_blocks

    @classmethod
    def _convert_headers(cls, text: str) -> str:
        """将 markdown 标题转换为粗体"""
        lines = text.split("\n")
        result = []
        for line in lines:
            # 匹配 1~6 个 # 开头的标题行，捕获 # 后面的标题正文
            header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if header_match:
                content = header_match.group(2)
                # 飞书 markdown 不支持标题语法，统一降级为粗体，并补一行空行制造段落间距
                result.append(f"**{content}**")
                result.append("")
            else:
                result.append(line)
        return "\n".join(result)

    @classmethod
    def _fix_paragraphs(cls, text: str) -> str:
        """移除多余空行"""
        # 标题转粗体时会插入空行，可能与原文已有的空行叠加成 3 个以上换行，这里统一压缩回两个
        return re.sub(r"\n{3,}", "\n\n", text)

    @classmethod
    def _restore_code_blocks(cls, text: str, code_blocks: list[str]) -> str:
        """恢复代码块"""
        # 按索引把占位符替换回原始代码块内容，与 _protect_code_blocks 中的编号一一对应
        for idx, block in enumerate(code_blocks):
            text = text.replace(f"\x00CODEBLOCK_{idx}\x00", block)
        return text

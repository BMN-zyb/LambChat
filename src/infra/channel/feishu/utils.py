"""
Feishu message content extraction utilities.
"""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 飞书（Feishu/Lark）消息的 content 字段本质上是一段 JSON 字符串，但具体结构
# 因 msg_type（text/post/image/interactive/share_chat 等）而完全不同，卡片类
# 消息（interactive）内部还是一套嵌套的"元素树"结构。
# 本模块提供两类函数：
#   - extract_* ：把飞书原始消息内容解析成人类可读的文本 / 图片 key 列表，
#     主要用于把飞书消息转换成 Agent 可理解的纯文本上下文；
#   - build_* ：反过来把纯文本 / 图片 key / 文件 key 组装成飞书要求的 JSON
#     content，用于向飞书发送消息。
# 由于飞书卡片消息的字段可能是字符串形式的二次编码 JSON，也可能直接是字典，
# 各函数在解析前都做了防御性的类型检查，解析失败时一律安静降级为占位符文本。
# ============================================================================

import json


def extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """Extract text representation from share cards and interactive messages."""
    # 分享卡片、系统消息等类型本身没有直接的纯文本内容，只能按 msg_type
    # 分发到对应的占位符文本；仅 interactive（互动卡片）需要递归提取卡片元素里的文字
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        # 互动卡片消息结构最复杂，交给专门的递归解析函数处理
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    # 用换行拼接已提取的片段；什么都没提取到时用 [msg_type] 兜底，避免返回空字符串
    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """Recursively extract text and links from interactive card content."""
    parts = []

    # 卡片内容有时以字符串形式嵌套传入（即二次 JSON 编码），需要先尝试解析；
    # 解析失败则退化为把原始字符串当作纯文本返回
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    # 标题字段可能是 {"content": ...} / {"text": ...} 形式的字典，也可能是纯字符串
    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    # elements 是卡片正文的元素数组（二维：外层是"行"，内层是每行的元素），
    # 逐个交给 _extract_element_content 按 tag 类型分派解析
    for elements in (
        content.get("elements", []) if isinstance(content.get("elements"), list) else []
    ):
        for element in elements:
            parts.extend(_extract_element_content(element))

    # card 字段代表嵌套卡片（如卡片模板引用），递归提取其内容
    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    # header 是卡片头部，标题结构与顶层 title 类似，但多嵌套了一层 header
    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: dict) -> list[str]:
    """Extract content from a single card element."""
    parts = []

    if not isinstance(element, dict):
        return parts

    # tag 决定元素类型，飞书卡片的元素协议与前端组件一一对应
    tag = element.get("tag", "")

    if tag in ("markdown", "lark_md"):
        # markdown / lark_md：内容直接就是一段 markdown 文本
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "div":
        # div：普通文本块，text 之外还可能带 fields（多列文本）
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        # a：超链接元素，链接地址和链接文本都保留（分别标注 link: 前缀）
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        # button：按钮文本 + 跳转链接（url 与 multi_url.url 二选一取值）
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        # img：没有 alt 文本时统一用占位符 [image] 表示
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        # note：备注块，内部还是一组子元素，递归解析
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        # column_set：分栏布局，需要先展开 columns，再展开每一列自己的 elements
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    else:
        # 未识别的 tag：尝试当作容器元素，递归展开其 elements，尽量不丢内容
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    return parts


def extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """Extract text and image keys from Feishu post (rich text) message."""

    def _parse_block(block: dict) -> tuple[str | None, list[str]]:
        # 单个富文本块：content 是"行数组"，每行又是"元素数组"
        # （tag 可能是 text/a/at/img），逐元素分类累积文本与图片 key
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if title := block.get("title"):
            texts.append(title)
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "img" and (key := el.get("image_key")):
                    images.append(key)
        return (" ".join(texts).strip() or None), images

    # Unwrap optional {"post": ...} envelope
    # 飞书 post 消息有时会包一层 {"post": {...}} 外壳，有时又直接就是内容本身，
    # 先尝试剥掉外壳，方便后面统一按同一套逻辑处理
    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []

    # Direct format
    # 非多语言格式：root 本身直接带 content 字段
    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs

    # Localized: prefer known locales, then fall back to any dict child
    # 多语言格式：root 下按语言代码分层（如 zh_cn/en_us/ja_jp），优先按已知
    # 语言顺序尝试；都没命中的话再退化为遍历所有字典类型的子值兜底解析
    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs

    return "", []


# Message type display mapping
# 无需（也无法）展开解析正文的简单消息类型，统一用占位符表示
MSG_TYPE_MAP: dict[str, str] = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def extract_message_text(content: str, msg_type: str) -> str:
    """
    Extract text content from a Feishu message.

    Args:
        content: Raw message content (JSON string)
        msg_type: Message type (text, image, post, etc.)

    Returns:
        Extracted text representation
    """
    # text 类型：content 就是 {"text": "..."}；JSON 解析失败时把原始
    # 字符串当作文本兜底返回，保证调用方总能拿到一个可展示的字符串
    if msg_type == "text":
        try:
            data = json.loads(content)
            return data.get("text", content)
        except (json.JSONDecodeError, TypeError):
            return content

    # post 富文本类型：委托 extract_post_content 解析，这里只取文本部分
    if msg_type == "post":
        try:
            data = json.loads(content)
            text, _ = extract_post_content(data)
            return text
        except (json.JSONDecodeError, TypeError):
            return "[rich text]"

    # image/audio/file/sticker 等简单类型直接查表返回占位符
    if msg_type in MSG_TYPE_MAP:
        return MSG_TYPE_MAP[msg_type]

    # Try to extract from share cards and interactive messages
    # 其余类型（分享卡片 / 互动卡片等）尝试解析 JSON 后走 extract_share_card_content；
    # content 不是合法 JSON 时安静忽略，最终统一兜底为 [msg_type]
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return extract_share_card_content(data, msg_type)
    except (json.JSONDecodeError, TypeError):
        pass

    return f"[{msg_type}]"


def extract_image_keys(content: str, msg_type: str) -> list[str]:
    """
    Extract image keys from a Feishu message.

    Args:
        content: Raw message content (JSON string)
        msg_type: Message type

    Returns:
        List of image keys
    """
    # image 类型：content 里直接带单个 image_key
    if msg_type == "image":
        try:
            data = json.loads(content)
            if key := data.get("image_key"):
                return [key]
        except (json.JSONDecodeError, TypeError):
            pass

    # post 富文本类型：可能内嵌多张图片，委托 extract_post_content 统一提取
    if msg_type == "post":
        try:
            data = json.loads(content)
            _, images = extract_post_content(data)
            return images
        except (json.JSONDecodeError, TypeError):
            pass

    # 其它类型没有图片，返回空列表
    return []


def build_text_content(text: str) -> str:
    """Build JSON content for a text message."""
    # ensure_ascii=False：保留中文等非 ASCII 字符原样输出，不转义成 \uXXXX
    return json.dumps({"text": text}, ensure_ascii=False)


def build_image_content(image_key: str) -> str:
    """Build JSON content for an image message."""
    return json.dumps({"image_key": image_key}, ensure_ascii=False)


def build_file_content(file_key: str, file_name: str | None = None) -> str:
    """Build JSON content for a file message."""
    # file_name 允许缺省，用空字符串占位，避免传 None 给飞书接口导致报错
    return json.dumps({"file_key": file_key, "file_name": file_name or ""}, ensure_ascii=False)

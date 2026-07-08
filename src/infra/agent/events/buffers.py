"""Small, bounded stream buffers used by agent event processing."""

# 缓冲键：(子 agent 深度, run_id, 命名空间)，用于区分不同流来源的文本
BufferKey = tuple[int, str | None, str | None]


class TextChunkBuffer:
    """Accumulate text chunks for one stream key and flush as joined text."""

    # 用 __slots__ 固定属性、降低内存开销（流式高频创建/累积）
    __slots__ = ("_length", "_parts", "flush_size", "key")

    def __init__(self, flush_size: int) -> None:
        # flush_size：累计字符数达到该阈值就建议刷出，控制 SSE 分块粒度
        self.flush_size = flush_size
        # _parts：暂存尚未刷出的文本片段；_length：累计字符长度
        self._parts: list[str] = []
        self._length = 0
        # key：当前缓冲内容所属的流键，None 表示尚未绑定
        self.key: BufferKey | None = None

    @property
    def has_pending(self) -> bool:
        # 是否存在待刷出的文本
        return self._length > 0

    def key_changed(self, key: BufferKey) -> bool:
        # 判断新到的流键是否与当前缓冲的流键不同（不同则需先刷出旧内容）
        return self.has_pending and self.key is not None and self.key != key

    def consume_ready(self, key: BufferKey) -> tuple[str, BufferKey | None] | None:
        """Consume pending text when appending a different stream key requires a flush."""
        # 流键切换时先把旧缓冲内容刷出，避免不同来源的文本被错误拼接
        if self.key_changed(key):
            return self.consume()
        return None

    def append(self, text: str, key: BufferKey) -> bool:
        """Append text and return whether size threshold asks for a flush."""
        # 空串直接忽略，不触发刷出
        if not text:
            return False

        # 累积片段并更新长度与当前流键
        self._parts.append(text)
        self._length += len(text)
        self.key = key
        # 返回是否已达到刷出阈值
        return self._length >= self.flush_size

    def consume(self) -> tuple[str, BufferKey | None]:
        # 无待刷内容时，仅返回空串与当前 key 并清空状态
        if not self.has_pending:
            key = self.key
            self.clear()
            return "", key

        # 拼接所有片段为完整文本，连同其流键一起返回，然后清空
        text = "".join(self._parts)
        key = self.key
        self.clear()
        return text, key

    def clear(self) -> None:
        # 重置缓冲到初始空状态
        self._parts.clear()
        self._length = 0
        self.key = None

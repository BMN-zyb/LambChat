"""Native memory backend package."""

# 只对外暴露 NativeMemoryBackend 这一个类，classification/consolidation/content/
# indexing/models/search/summaries 等内部实现模块均不作为公开 API
from .backend import NativeMemoryBackend

__all__ = ["NativeMemoryBackend"]

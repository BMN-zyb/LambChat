"""
会话分享模块
"""

# 对外只暴露持久化层 ShareStorage，具体的 SEO 元信息生成逻辑见同目录下的 seo.py（不在此处导出）。
from src.infra.share.storage import ShareStorage

__all__ = ["ShareStorage"]

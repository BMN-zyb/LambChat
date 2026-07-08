"""
Feedback 模块

提供用户反馈的存储和管理功能。
"""

# FeedbackManager：业务门面，编排点赞/评论等反馈操作及会话/运行级统计聚合
# FeedbackStorage：底层 MongoDB 持久化
from src.infra.feedback.manager import FeedbackManager
from src.infra.feedback.storage import FeedbackStorage

# 对外导出反馈存储与管理器
__all__ = ["FeedbackStorage", "FeedbackManager"]

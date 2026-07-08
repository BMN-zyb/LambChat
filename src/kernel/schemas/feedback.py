"""
用户反馈 Schema

定义用户反馈的数据模型。
反馈关联到每个 run_id，用户对每个 run 只能提交一次反馈。
"""

# 主要使用方：src/infra/feedback/manager.py / storage.py（反馈的存取与统计聚合）、
# src/api/routes/feedback.py（提交/查询反馈的 HTTP 接口）。
# 依赖 src/kernel/schemas/agent.py 中的 AttachmentSchema 复用图片附件结构。
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.kernel.schemas.agent import AttachmentSchema

# 评分值类型：up（好评）或 down（差评）
RatingValue = Literal["up", "down"]


class FeedbackBase(BaseModel):
    """反馈基础模型"""

    # 评分：取值限定为 RatingValue（up=好评 / down=差评）
    rating: RatingValue = Field(..., description="评分：up（好评）或 down（差评）")
    comment: Optional[str] = Field(None, max_length=1000, description="可选评论")
    # 可选图片附件，复用 schemas/agent.py 中的 AttachmentSchema 结构
    attachments: Optional[list[AttachmentSchema]] = Field(None, description="可选图片附件")


# 创建反馈的请求体：在基础字段之上补充要关联的会话与运行标识。
class FeedbackCreate(FeedbackBase):
    """创建反馈请求"""

    session_id: str = Field(..., description="会话ID")
    # run_id 作为反馈的关联/幂等键：同一个 run_id 只能提交一次反馈（由业务层保证）
    run_id: str = Field(..., description="运行ID")


# 反馈的响应模型（数据库实体视图）。
class Feedback(FeedbackBase):
    """反馈响应模型"""

    # 反馈记录 ID
    id: str
    # 提交反馈的用户 ID
    user_id: str
    # 提交反馈的用户名（冗余存储，避免展示时再反查用户表）
    username: str
    session_id: str
    run_id: str
    # 提交时间
    created_at: datetime

    # 允许从属性对象（如 Mongo 文档转换后的对象）直接构造本模型
    model_config = ConfigDict(from_attributes=True)


# 数据库中的反馈文档：目前字段与 Feedback 完全一致（用 pass 占位），
# 为未来可能加入的仅内部使用、不对外返回的字段预留扩展位置。
class FeedbackInDB(Feedback):
    """数据库中的反馈文档（包含敏感字段)"""

    pass


# 反馈统计信息，通常用于展示某个范围（如某个 Agent/团队）的整体好评情况。
class FeedbackStats(BaseModel):
    """反馈统计信息"""

    # 反馈总条数
    total_count: int = 0
    # 好评（up）条数
    up_count: int = 0
    # 差评（down）条数
    down_count: int = 0
    # 好评率，单位百分比（0~100），计算方式为 up_count / total_count * 100，四舍五入到 1 位小数
    up_percentage: float = 0.0


# 反馈列表接口的响应体。
class FeedbackListResponse(BaseModel):
    """反馈列表响应"""

    # 当前页的反馈列表
    items: list[Feedback]
    # 满足条件的总条数
    total: int
    # 当前筛选条件下的统计信息
    stats: FeedbackStats

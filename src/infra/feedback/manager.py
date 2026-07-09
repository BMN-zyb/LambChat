"""
用户反馈管理器

提供用户反馈的业务逻辑。
每个用户对每个 run 只能提交一次反馈。
"""

# ---------------------------------------------------------------------------
# 模块说明：用户反馈业务管理层（Manager）
#
# 本模块是反馈功能的业务门面（service 层），位于 API 路由与存储层之间：
#   - 对上：给路由层提供 submit/get/list/delete/stats 等语义清晰的方法；
#   - 对下：把实际的数据库读写全部委托给 FeedbackStorage（storage.py）。
# Manager 本身几乎不含复杂逻辑，主要负责编排存储调用、打印审计日志，
# 以及把多次查询结果组装成对外的响应模型。
# 「每个用户对每个 run 只能反馈一次」这条业务规则的落地在 storage 层：
# 既有应用层预检查（给出友好报错），又有数据库唯一索引兜底（防并发重复写）。
# ---------------------------------------------------------------------------

from typing import Optional

from src.infra.feedback.storage import FeedbackStorage
from src.infra.logging import get_logger
from src.kernel.schemas.feedback import (
    Feedback,
    FeedbackCreate,
    FeedbackListResponse,
    FeedbackStats,
    RatingValue,
)

logger = get_logger(__name__)


class FeedbackManager:
    """用户反馈管理器"""

    # 注：本类没有在此模块内提供进程级单例获取函数，单例缓存由调用方
    # （src/api/routes/feedback.py 的 lru_cache）负责管理

    def __init__(self):
        self.storage = FeedbackStorage()

    async def submit_feedback(
        self,
        user_id: str,
        username: str,
        data: FeedbackCreate,
    ) -> Feedback:
        """
        提交反馈（每个 run 只能提交一次）

        Args:
            user_id: 用户ID
            username: 用户名
            data: 反馈数据

        Returns:
            创建的反馈

        Raises:
            ValueError: 如果已对该 run 提交过反馈
        """
        # 重复提交的拦截逻辑在 storage.create 内部完成（应用层预检查 + 唯一索引兜底）
        feedback = await self.storage.create(data, user_id, username)
        logger.info(f"Feedback submitted by user {username} for run {data.run_id}")
        return feedback

    async def get_feedback(self, feedback_id: str) -> Optional[Feedback]:
        """
        获取单个反馈

        Args:
            feedback_id: 反馈ID

        Returns:
            反馈对象
        """
        return await self.storage.get_by_id(feedback_id)

    async def get_user_feedback_for_run(
        self,
        user_id: str,
        session_id: str,
        run_id: str,
    ) -> Optional[Feedback]:
        """
        获取用户对某个 run 的反馈

        Args:
            user_id: 用户ID
            session_id: 会话ID
            run_id: 运行ID

        Returns:
            反馈对象，如果不存在则返回None
        """
        return await self.storage.get_user_feedback_for_run(user_id, session_id, run_id)

    async def get_feedback_by_run(
        self,
        session_id: str,
        run_id: str,
    ) -> list[Feedback]:
        """
        获取某个 run 的所有反馈

        Args:
            session_id: 会话ID
            run_id: 运行ID

        Returns:
            反馈列表
        """
        return await self.storage.get_by_run(session_id, run_id)

    async def list_feedback(
        self,
        skip: int = 0,
        limit: int = 50,
        rating: Optional[RatingValue] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> FeedbackListResponse:
        """
        获取反馈列表

        Args:
            skip: 跳过数量
            limit: 限制数量
            rating: 评分过滤
            user_id: 用户ID过滤
            session_id: 会话ID过滤

        Returns:
            反馈列表响应
        """
        # 列表数据、总数、整体统计分别对应三次独立的存储层查询，
        # 三者互不依赖，一并打包进响应模型返回给前端
        items = await self.storage.list(skip, limit, rating, user_id, session_id)
        total = await self.storage.count(rating, user_id, session_id)
        stats = await self.storage.get_stats(session_id)
        return FeedbackListResponse(items=items, total=total, stats=stats)

    async def delete_feedback(self, feedback_id: str) -> bool:
        """
        删除反馈

        Args:
            feedback_id: 反馈ID

        Returns:
            是否删除成功
        """
        success = await self.storage.delete(feedback_id)
        if success:
            logger.info(f"Feedback {feedback_id} deleted")
        return success

    async def get_stats(
        self,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> FeedbackStats:
        """
        获取反馈统计信息

        Args:
            session_id: 可选的会话ID过滤
            run_id: 可选的运行ID过滤

        Returns:
            统计信息
        """
        return await self.storage.get_stats(session_id, run_id)

    async def close(self) -> None:
        await self.storage.close()

"""Presenter 事件存储 mixin (Redis + MongoDB)

处理 trace 创建、事件持久化、token usage 保证和 trace 完成。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger

if TYPE_CHECKING:
    from src.infra.session.dual_writer import DualEventWriter
    from src.infra.writer.presenter_config import PresenterConfig

logger = get_logger(__name__)


class StoragePresenterMixin:
    """事件存储 mixin —— 需要 self.config / self._dual_writer / self.trace_id 等属性。"""

    # Attributes provided by the Presenter host class
    config: PresenterConfig
    trace_id: str
    run_id: str
    _step_count: int
    _tool_calls: list[dict[str, Any]]
    _dual_writer: DualEventWriter | None
    _trace_created: bool
    _done_recorded: bool
    _goal_end_recorded: bool
    _completed: bool
    _token_usage_recorded: bool

    # ------------------------------------------------------------------
    # DualWriter 获取
    # ------------------------------------------------------------------

    async def _get_dual_writer(self):
        """延迟获取 DualEventWriter"""
        if self._dual_writer is None:
            try:
                from src.infra.session.dual_writer import get_dual_writer

                self._dual_writer = get_dual_writer()
                logger.debug("dual_writer initialized: %s", self._dual_writer is not None)
            except Exception as e:
                logger.warning("Failed to init dual_writer: %s", e)
        return self._dual_writer

    # ------------------------------------------------------------------
    # Trace 元数据
    # ------------------------------------------------------------------

    async def _build_identity_metadata(self) -> Dict[str, Any]:
        """Build non-sensitive user identity metadata for tracing systems."""
        metadata: Dict[str, Any] = {}

        if not self.config.user_id:
            return metadata

        metadata["user_id"] = self.config.user_id

        try:
            from src.infra.user.storage import UserStorage

            user = await UserStorage().get_by_id(self.config.user_id)
            username = getattr(user, "username", None) if user else None
            if username:
                metadata["username"] = username
        except Exception as e:
            logger.debug("Failed to enrich trace metadata for user %s: %s", self.config.user_id, e)

        return metadata

    async def _build_trace_metadata(self) -> Dict[str, Any]:
        """Build trace metadata, enriching it with non-sensitive user identity when available."""
        metadata: Dict[str, Any] = {
            "agent_name": self.config.agent_name,
        }
        metadata.update(await self._build_identity_metadata())
        return metadata

    async def build_langsmith_metadata(self) -> Dict[str, Any]:
        """Build metadata that should be attached to LangSmith runs."""
        metadata = await self._build_identity_metadata()
        if self.config.agent_name:
            metadata["agent_name"] = self.config.agent_name
        return metadata

    # ------------------------------------------------------------------
    # Trace 生命周期
    # ------------------------------------------------------------------

    async def _ensure_trace(self):
        """确保 trace 已创建"""
        if self._trace_created:
            return

        dual_writer = await self._get_dual_writer()
        if not dual_writer:
            logger.debug("_ensure_trace: dual_writer is None, skipping")
            return

        # 如果没有 session_id，跳过 trace 创建
        if not self.config.session_id:
            logger.debug(
                "_ensure_trace: no session_id (config.session_id=%s), skipping",
                self.config.session_id,
            )
            return

        try:
            logger.debug(
                "Creating trace: trace_id=%s, session_id=%s",
                self.trace_id,
                self.config.session_id,
            )
            metadata = await self._build_trace_metadata()
            await dual_writer.create_trace(
                trace_id=self.trace_id,
                session_id=self.config.session_id,
                agent_id=self.config.agent_id,
                run_id=self.run_id,
                user_id=self.config.user_id,
                metadata=metadata,
            )
            self._trace_created = True
            logger.debug("Trace created successfully: %s", self.trace_id)
        except Exception as e:
            logger.warning("Failed to create trace: %s", e)

    # ------------------------------------------------------------------
    # 事件存储
    # ------------------------------------------------------------------

    async def save_event(self, event: Dict[str, Any]) -> None:
        """
        保存 SSE 事件到 Redis + MongoDB (按 trace 聚合)

        Args:
            event: SSE 事件字典，包含 event 和 data 字段
        """
        if not self.config.enable_storage:
            return

        try:
            await self._ensure_trace()

            event_type = event.get("event", "unknown")
            if event_type == "done" and self._done_recorded:
                return
            if event_type == "goal:end" and self._goal_end_recorded:
                return
            data = event.get("data", {})

            # 如果 data 是字符串（旧格式或外部传入），需要解析并清理
            # 如果是 dict（来自优化后的 _build_event），已经 sanitize 过，直接使用
            if isinstance(data, str):
                try:
                    data = await run_blocking_io(json.loads, data)
                except json.JSONDecodeError:
                    data = {"raw": data}
                data = self._sanitize_for_json(data)  # type: ignore[attr-defined]

            dual_writer = await self._get_dual_writer()
            if dual_writer and self.config.session_id:
                if event_type == "done":
                    await self._ensure_token_usage_event()
                await dual_writer.write_event(
                    session_id=self.config.session_id,
                    event_type=event_type,
                    data=data,
                    trace_id=self.trace_id,
                    agent_id=self.config.agent_id,
                    run_id=self.run_id,
                )
                if event_type == "token:usage":
                    self._token_usage_recorded = True
                elif event_type == "goal:end":
                    self._goal_end_recorded = True
                elif event_type == "done":
                    self._done_recorded = True
        except Exception as e:
            logger.warning("Failed to save event: %s", e)

    async def _ensure_token_usage_event(self) -> None:
        """Persist a token usage event before terminal trace status, even if usage is zero."""
        if self._token_usage_recorded or not self.config.enable_storage:
            return
        if not self.config.session_id:
            return

        await self.save_event(self.present_token_usage())  # type: ignore[attr-defined]

    async def complete(self, status: str = "completed") -> None:
        """
        标记 trace 完成

        应该在流结束时调用此方法。
        会先刷新 MongoDB 写入缓冲，确保所有事件已持久化。

        Args:
            status: 完成状态 (completed/error)
        """
        if self._completed:
            return

        dual_writer = await self._get_dual_writer()
        if dual_writer and self.config.session_id:
            try:
                await self._ensure_token_usage_event()
                # 先刷新 MongoDB 缓冲，确保所有事件已写入
                await dual_writer.flush_mongo_buffer()
                await dual_writer.complete_trace(
                    trace_id=self.trace_id,
                    status=status,
                    metadata={
                        "step_count": self._step_count,
                        "tool_calls": len(self._tool_calls),
                    },
                )
                self._completed = True
                logger.debug("Trace completed: %s, status=%s", self.trace_id, status)

                # AI 回复完成或出错时递增未读计数，确保用户下次打开能看到。
                if should_increment_unread_for_trace_status(status) and self.config.session_id:
                    try:
                        from src.infra.session.manager import SessionManager

                        mgr = SessionManager()
                        await mgr.increment_unread_count(self.config.session_id)
                    except Exception as e:
                        logger.warning("Failed to increment unread_count: %s", e)
            except Exception as e:
                logger.warning("Failed to complete trace %s: %s", self.trace_id, e)

    async def emit(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """发送单个事件（自动保存）"""
        event_type = event.get("event", "unknown")
        data = event.get("data", {})
        agent_id = data.get("agent_id") if isinstance(data, dict) else None
        depth = data.get("depth") if isinstance(data, dict) else None
        if agent_id or (depth and depth > 0):
            logger.debug(
                f"[Presenter.emit] event_type={event_type}, agent_id={agent_id}, depth={depth}"
            )
        await self.save_event(event)
        return event


# 延迟导入避免循环依赖
from src.infra.writer.presenter_config import should_increment_unread_for_trace_status  # noqa: E402

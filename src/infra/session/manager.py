"""
会话管理器
"""

# 会话管理器（SessionManager）：会话领域对外的统一入口，聚合三层存储——
# 会话元数据存储（SessionStorage）、事件溯源存储（TraceStorage）与附件文件记录
# （FileRecordStorage）。
# 主要职责：
#   - 会话 CRUD、列表、未读计数、收藏、停用等元数据操作（多为对 storage 的转发）；
#   - 消息与附件的生命周期：清空/删除会话时，先释放附件引用计数、回收无引用文件，
#     再删除对应的 trace 与 LangGraph 检查点，避免残留脏数据与孤儿文件；
#   - 事件溯源读取：跨 trace 聚合出会话的事件流 / trace 列表；
#   - fork（分叉）：以某条用户消息或命名检查点为锚点，复制锚点之前的历史 trace，
#     派生出一个可独立续聊的新会话；并优先直接克隆 LangGraph 检查点，克隆失败时
#     退化为「用复制出的历史消息重建并种入检查点」，最后重建搜索索引使历史可检索。
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import List, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.session.storage import SessionStorage
from src.infra.session.trace_storage import get_trace_storage
# checkpoint：LangGraph 检查点相关工具（从 trace 事件重建消息、fork 时克隆检查点、删除线程检查点、按消息种入检查点）
from src.infra.storage.checkpoint import (
    build_messages_from_trace_events,
    clone_checkpoints_for_fork,
    delete_checkpoints_for_thread,
    seed_checkpoint_from_messages,
)
from src.infra.storage.s3.service import get_or_init_storage, get_s3_enabled
from src.infra.upload.file_record import FileRecordStorage
from src.infra.utils.datetime import utc_now, utc_now_iso
from src.kernel.exceptions import NotFoundError, SessionError
from src.kernel.schemas.session import (
    Session,
    SessionCheckpoint,
    SessionCreate,
    SessionUpdate,
    clone_session_metadata,
)

logger = get_logger(__name__)

# 收集会话附件时扫描的用户消息事件上限，避免超大会话拖垮扫描
SESSION_ATTACHMENT_EVENT_SCAN_LIMIT = 1000
# fork 复制 trace 时的批量插入大小，减少数据库往返
SESSION_FORK_TRACE_INSERT_BATCH_SIZE = 25


# fork 克隆历史的结果对象：既携带复制的 trace 数与检查点消息，
# 又通过 __len__/__iter__ 兼容旧调用方把它当作"文档列表"使用
@dataclass
class SessionForkCloneResult:
    # 已复制的 trace 数量
    copied_trace_count: int = 0
    # 由复制的 trace 重建出的检查点消息（仅在需要种入检查点时收集）
    checkpoint_messages: list[object] = field(default_factory=list)
    # 兼容字段：供旧代码以可迭代方式遍历克隆产生的文档（不参与 repr）
    _compat_docs: list[dict] = field(default_factory=list, repr=False)

    def __len__(self) -> int:
        # 令 len(result) 返回复制的 trace 数（向后兼容）
        return self.copied_trace_count

    def __iter__(self):
        # 令 for ... in result 遍历兼容文档列表（向后兼容）
        return iter(self._compat_docs)


class SessionManager:
    """
    会话管理器

    提供会话的 CRUD 功能。
    """

    def __init__(self):
        # 会话元数据存储层
        self.storage = SessionStorage()
        # trace 存储延迟加载
        self._trace_storage = None
        # 文件记录存储：管理附件引用计数
        self._file_record_storage = FileRecordStorage()

    @property
    def trace_storage(self):
        """延迟加载 trace 存储"""
        if self._trace_storage is None:
            self._trace_storage = get_trace_storage()
        return self._trace_storage

    async def create_session(
        self,
        session_data: SessionCreate,
        user_id: Optional[str] = None,
    ) -> Session:
        """创建会话"""
        return await self.storage.create(session_data, user_id)

    async def get_session(self, session_id: str) -> Optional[Session]:
        """获取会话（优先使用自定义 session_id）"""
        # 优先使用自定义 session_id 查询
        session = await self.storage.get_by_session_id(session_id)
        if session:
            return session
        # 兼容旧的 ObjectId 查询
        return await self.storage.get_by_id(session_id)

    async def get_sessions(self, session_ids: list[str]) -> dict[str, Session]:
        """批量获取会话，返回 {session_id: Session} 映射"""
        return await self.storage.get_by_session_ids(session_ids)

    async def get_session_events(
        self,
        session_id: str,
        since_seq: Optional[int] = None,
        limit: int = 100,
    ) -> List[dict]:
        """获取会话事件（从 traces 聚合）"""
        # since_seq 已弃用，显式丢弃以保持签名兼容
        del since_seq
        return await self.trace_storage.get_session_events(session_id, max_events=limit)

    async def get_session_traces(
        self,
        session_id: str,
        limit: int = 50,
        skip: int = 0,
    ) -> List[dict]:
        """获取会话的所有 traces"""
        return await self.trace_storage.list_traces(
            session_id=session_id,
            limit=limit,
            skip=skip,
        )

    async def update_session(
        self,
        session_id: str,
        session_data: SessionUpdate,
    ) -> Optional[Session]:
        """更新会话"""
        return await self.storage.update(session_id, session_data)

    async def update_session_metadata(self, session_id: str, metadata: dict) -> bool:
        """Update metadata fields without materializing the full session."""
        return await self.storage.update_metadata_only(session_id, metadata)

    async def _collect_user_attachment_keys(self, session_id: str) -> list[str]:
        """Collect unique attachment keys from persisted user messages in a session."""
        # 扫描会话内的用户消息事件，汇总其附件对象存储 key（去重）
        events = await self.trace_storage.get_session_events(
            session_id,
            event_types=["user:message"],
            completed_only=False,
            max_events=SESSION_ATTACHMENT_EVENT_SCAN_LIMIT,
        )
        keys: set[str] = set()
        for event in events:
            if event.get("event_type") != "user:message":
                continue
            data = event.get("data", {})
            for attachment in data.get("attachments") or []:
                key = str(attachment.get("key", "")).strip()
                if key:
                    keys.add(key)
        return sorted(keys)

    async def _cleanup_unreferenced_files(self, keys: list[str]) -> int:
        """Delete backing files and records for keys whose references reached zero."""
        # 删除引用计数归零的附件：同时清对象存储实体与文件记录
        if not keys:
            return 0

        # 仅在启用 S3 时初始化对象存储客户端
        storage = await get_or_init_storage() if get_s3_enabled() else None
        deleted = 0
        for key in keys:
            record = await self._file_record_storage.find_by_key(key)
            # 仍被其他会话引用（reference_count>0）或记录不存在则跳过，避免误删共享文件
            if record is None or record.get("reference_count", 0) > 0:
                continue

            if storage is not None:
                await storage.delete_file(key)
            await self._file_record_storage.delete_by_key(key)
            deleted += 1

        return deleted

    async def clear_session_messages(self, session_id: str) -> int:
        """Release attachment references and remove all traces for a session."""
        # 清空会话消息：先释放附件引用并回收无引用文件，再删除全部 trace
        attachment_keys = await self._collect_user_attachment_keys(session_id)
        await self._file_record_storage.release_references(attachment_keys)
        await self._cleanup_unreferenced_files(attachment_keys)
        await self.trace_storage.delete_session_traces(session_id)
        return len(attachment_keys)

    async def delete_session(self, session_id: str) -> bool:
        """删除会话（同时删除关联的 traces）"""
        # 先清理消息/附件/trace
        await self.clear_session_messages(session_id)
        # Clean up revealed file index
        # 清理该会话产生的 revealed file 索引（失败不阻断删除主流程）
        try:
            from src.infra.revealed_file.storage import get_revealed_file_storage

            revealed_storage = get_revealed_file_storage()
            deleted = await revealed_storage.delete_by_session(session_id)
            if deleted:
                logger.info(f"Deleted {deleted} revealed file records for session {session_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup revealed files for session {session_id}: {e}")
        # 再删除 session
        deleted = await self.storage.delete(session_id)
        # 会话删除成功后清理其 LangGraph 检查点（以 session_id 作为 thread_id）
        if deleted:
            try:
                await delete_checkpoints_for_thread(session_id)
            except Exception as e:
                logger.warning(f"Failed to cleanup checkpoints for session {session_id}: {e}")
        return deleted

    async def list_sessions(
        self,
        user_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
        is_active: Optional[bool] = None,
        project_id: Optional[str] = None,
        search: Optional[str] = None,
        favorites_only: bool = False,
        favorites_project_id: str | None = None,
    ) -> tuple[list[Session], int]:
        """列出会话，返回 (sessions, total_count)"""
        return await self.storage.list_sessions(
            user_id,
            skip,
            limit,
            is_active,
            project_id,
            search,
            favorites_only,
            favorites_project_id,
        )

    async def increment_unread_count(self, session_id: str) -> bool:
        """递增会话未读计数"""
        return await self.storage.increment_unread_count(session_id)

    async def mark_read(self, session_id: str) -> bool:
        """将会话标记为已读"""
        return await self.storage.mark_read(session_id)

    async def mark_all_read(
        self,
        user_id: str,
        project_id: str | None = None,
        scheduled_task_id: str | None = None,
    ) -> int:
        """批量将会话标记为已读，支持按项目或定时任务过滤。"""
        return await self.storage.mark_all_read(user_id, project_id, scheduled_task_id)

    async def deactivate_session(self, session_id: str) -> Optional[Session]:
        """停用会话"""
        # 通过 metadata.is_active=False 软停用会话（不物理删除）
        return await self.storage.update(
            session_id,
            SessionUpdate(metadata={"is_active": False}),
        )

    async def create_message_checkpoint(
        self,
        session_id: str,
        message_id: str,
        *,
        user_id: str,
        name: str | None = None,
    ) -> dict:
        """Create a named checkpoint for a message within a session."""
        # 为会话中某条消息创建命名检查点（记录锚点消息与其来源 run/trace）
        session = await self.get_session(session_id)
        # 校验会话存在且属于当前用户
        if not session or session.user_id != user_id:
            raise NotFoundError("session_not_found")

        # 解析该消息对应的 fork 锚点（run_id/trace_id 等）
        target = await self._resolve_fork_target(session_id, message_id)
        checkpoint = SessionCheckpoint(
            id=f"checkpoint_{uuid.uuid4().hex}",
            message_id=message_id,
            name=(name or "Checkpoint").strip() or "Checkpoint",
            source_run_id=target["run_id"],
            source_trace_id=target.get("trace_id"),
        )
        # 追加到会话已有检查点列表并写回 metadata
        checkpoints = self._load_session_checkpoints(session)
        checkpoints.append(checkpoint)

        updated_session = await self.update_session(
            session_id,
            SessionUpdate(
                metadata={"checkpoints": [item.model_dump(mode="json") for item in checkpoints]}
            ),
        )
        return {
            "checkpoint": checkpoint.model_dump(mode="json"),
            "session": updated_session,
        }

    async def fork_session_from_checkpoint(
        self,
        session_id: str,
        checkpoint_id: str,
        *,
        user_id: str,
    ) -> dict:
        """Fork a new session from a stored checkpoint."""
        # 从已保存的检查点 fork 出新会话
        session = await self.get_session(session_id)
        if not session or session.user_id != user_id:
            raise NotFoundError("session_not_found")

        # 定位目标检查点
        checkpoints = self._load_session_checkpoints(session)
        checkpoint = next((item for item in checkpoints if item.id == checkpoint_id), None)
        if checkpoint is None:
            raise NotFoundError("checkpoint_not_found")

        # 复用按消息 fork 的逻辑，带上检查点来源元数据
        result = await self.fork_session_from_message(
            session_id,
            checkpoint.message_id,
            user_id,
            fork_metadata={
                "fork_type": "checkpoint",
                "checkpoint_id": checkpoint.id,
                "checkpoint_name": checkpoint.name,
            },
        )
        result["checkpoint_id"] = checkpoint.id
        return result

    async def fork_session_from_message(
        self,
        session_id: str,
        message_id: str,
        user_id: str,
        fork_metadata: dict | None = None,
    ) -> dict:
        """Fork a new session from a specific message anchor."""
        # 从指定消息锚点 fork 新会话：复制锚点之前的历史 trace + 检查点
        source_session = await self.get_session(session_id)
        if not source_session or source_session.user_id != user_id:
            raise NotFoundError("session_not_found")

        # 解析锚点位置（用户消息 or 助手 run），拿到 run_id/turn_index 等
        target = await self._resolve_fork_target(session_id, message_id)
        # 克隆源会话 metadata 并追加 fork 溯源信息
        new_metadata = clone_session_metadata(source_session.metadata)
        new_metadata.update(
            {
                "forked_from_session_id": session_id,
                "forked_from_message_id": message_id,
                "forked_at": utc_now_iso(),
                **(fork_metadata or {}),
            }
        )
        if target.get("run_id"):
            new_metadata["current_run_id"] = target["run_id"]

        # 创建新会话（名称带 (Fork) 后缀）
        new_session = await self.create_session(
            SessionCreate(
                name=self._build_fork_session_name(source_session.name),
                metadata=new_metadata,
            ),
            user_id=user_id,
        )

        # 优先直接克隆 LangGraph 检查点；失败则记录错误，后续用消息种入兜底
        copied_checkpoint_count = 0
        checkpoint_clone_error: Exception | None = None
        try:
            copied_checkpoint_count = await clone_checkpoints_for_fork(
                source_session.id,
                new_session.id,
                turn_index=target["turn_index"],
                target_type=target["target_type"],
            )
        except Exception as exc:
            checkpoint_clone_error = exc
            logger.warning(
                "Failed to clone fork checkpoints: source_session=%s target_session=%s message=%s error=%s",
                source_session.id,
                new_session.id,
                message_id,
                exc,
            )

        try:
            # 仅当检查点克隆失败且无副本时，才在克隆历史时顺便收集消息用于种入检查点
            needs_checkpoint_seed = (
                copied_checkpoint_count == 0 and checkpoint_clone_error is not None
            )
            clone_result = await self._clone_history_to_session(
                source_session=source_session,
                target_session=new_session,
                target=target,
                user_id=user_id,
                collect_checkpoint_messages=needs_checkpoint_seed,
            )
            # 兜底：用重建出的消息为新会话种入初始检查点
            if needs_checkpoint_seed:
                copied_checkpoint_count = await seed_checkpoint_from_messages(
                    new_session.id,
                    clone_result.checkpoint_messages,
                )
            # 重建新会话搜索索引，使复制来的历史可被搜索
            await self.storage.rebuild_search_index(new_session.id)
            return {
                "session": new_session,
                "source_session_id": source_session.id,
                "source_message_id": message_id,
                "copied_trace_count": clone_result.copied_trace_count,
                "copied_checkpoint_count": copied_checkpoint_count,
            }
        except Exception as exc:
            # 复制过程失败：回滚删除半成品新会话，避免留下脏数据
            await self.delete_session(new_session.id)
            raise SessionError(f"fork_checkpoint_copy_failed: {exc}") from exc

    async def _clone_history_to_session(
        self,
        *,
        source_session: Session,
        target_session: Session,
        target: dict,
        user_id: str,
        collect_checkpoint_messages: bool = False,
    ) -> SessionForkCloneResult:
        # 把源会话锚点之前的 trace 复制到新会话；分批插入减少往返
        async def _flush_batch() -> None:
            if batch:
                await self.trace_storage.collection.insert_many(list(batch))
                batch.clear()

        # 按时间升序遍历源会话的所有 trace
        cursor = self.trace_storage.collection.find(
            {"session_id": source_session.id},
            {"_id": 0},
        ).sort("started_at", 1)
        result = SessionForkCloneResult()
        batch: list[dict] = []
        async for trace in cursor:
            run_id = trace.get("run_id")
            if not run_id:
                continue
            cloned_doc = None
            # 命中锚点所在 run：按锚点类型决定复制范围
            if run_id == target["run_id"]:
                if target["target_type"] == "user":
                    # 用户消息锚点：只复制到该用户消息为止（部分 trace）
                    cloned_doc = await run_blocking_io(
                        self._build_partial_user_trace_doc,
                        trace,
                        target["user_event"],
                        target_session.id,
                        user_id,
                    )
                elif target["target_type"] == "assistant":
                    # 助手锚点：整条 trace 完整复制
                    cloned_doc = await run_blocking_io(
                        self._build_cloned_trace_doc,
                        trace,
                        target_session.id,
                        user_id,
                    )
            elif target.get("completed_run_ids") is not None:
                # 指定了已完成 run 集合：仅复制其中的 run
                if run_id in target["completed_run_ids"]:
                    cloned_doc = await run_blocking_io(
                        self._build_cloned_trace_doc,
                        trace,
                        target_session.id,
                        user_id,
                    )
            else:
                # 默认：锚点之前的 run 全部完整复制
                cloned_doc = await run_blocking_io(
                    self._build_cloned_trace_doc,
                    trace,
                    target_session.id,
                    user_id,
                )

            if cloned_doc is not None:
                result.copied_trace_count += 1
                # 需要时由复制的 trace 重建消息，供种入检查点兜底
                if collect_checkpoint_messages:
                    checkpoint_messages = await run_blocking_io(
                        build_messages_from_trace_events,
                        [cloned_doc],
                    )
                    result.checkpoint_messages.extend(checkpoint_messages)
                batch.append(cloned_doc)
                if len(batch) >= SESSION_FORK_TRACE_INSERT_BATCH_SIZE:
                    await _flush_batch()

            # 到达锚点 run（用户/助手）即停止，不复制其后的历史
            if run_id == target["run_id"] and target["target_type"] in {"user", "assistant"}:
                break

        await _flush_batch()
        return result

    async def _resolve_fork_target(self, session_id: str, message_id: str) -> dict:
        # 根据 message_id 在会话 trace 中定位 fork 锚点，返回类型/run/turn 等上下文
        cursor = self.trace_storage.collection.find(
            {"session_id": session_id},
            {
                "_id": 0,
                "trace_id": 1,
                "run_id": 1,
                "events.event_type": 1,
                "events.data": 1,
            },
        ).sort("started_at", 1)
        # 记录已遍历完成的 run 数，用于推算对话轮次 turn_index
        completed_run_count = 0

        async for trace in cursor:
            run_id = trace.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                continue
            turn_index = completed_run_count + 1

            # 情形一：锚点是某条用户消息
            for event in trace.get("events", []):
                if event.get("event_type") != "user:message":
                    continue
                data = event.get("data") or {}
                current_message_id = self._resolve_user_message_id(run_id, data)
                if current_message_id == message_id:
                    return {
                        "target_type": "user",
                        "run_id": run_id,
                        "trace_id": trace.get("trace_id"),
                        "user_event": event,
                        "completed_run_count": completed_run_count,
                        "turn_index": turn_index,
                    }

            # 情形二：锚点是助手回复（message_id 即 run_id）
            if run_id == message_id:
                return {
                    "target_type": "assistant",
                    "run_id": run_id,
                    "trace_id": trace.get("trace_id"),
                    "completed_run_count": completed_run_count + 1,
                    "turn_index": turn_index,
                }

            completed_run_count += 1

        # 未找到锚点消息
        raise NotFoundError("message_not_found")

    @staticmethod
    def _resolve_user_message_id(run_id: str, data: dict) -> str:
        # 计算用户消息的稳定 id：优先用显式 message_id，缺失则回退 "{run_id}:user"
        message_id = str(data.get("message_id") or "").strip()
        if message_id:
            return message_id
        return f"{run_id}:user"

    @staticmethod
    def _build_cloned_trace_doc(trace: dict, session_id: str, user_id: str) -> dict:
        # 深拷贝整条 trace，换上新的 trace_id/session_id/user_id 作为副本
        cloned = deepcopy(trace)
        cloned.pop("_id", None)
        cloned["trace_id"] = f"trace_{uuid.uuid4().hex}"
        cloned["session_id"] = session_id
        cloned["user_id"] = user_id
        return cloned

    def _build_partial_user_trace_doc(
        self,
        trace: dict,
        user_event: dict,
        session_id: str,
        user_id: str,
    ) -> dict:
        # 构造"只含该用户消息"的部分 trace 副本（用户消息锚点时使用），状态直接置完成
        timestamp = user_event.get("timestamp") or utc_now()
        return {
            "trace_id": f"trace_{uuid.uuid4().hex}",
            "session_id": session_id,
            "run_id": trace.get("run_id"),
            "agent_id": trace.get("agent_id"),
            "user_id": user_id,
            "events": [deepcopy(user_event)],
            "event_count": 1,
            "started_at": timestamp,
            "updated_at": timestamp,
            "completed_at": timestamp,
            "status": "completed",
            "metadata": deepcopy(trace.get("metadata") or {}),
        }

    @staticmethod
    def _build_fork_session_name(name: str | None) -> str:
        # 生成 fork 会话名：追加 (Fork) 后缀，已带则不重复添加
        base = (name or "New Chat").strip() or "New Chat"
        if base.endswith(" (Fork)"):
            return base
        return f"{base} (Fork)"

    @staticmethod
    def _load_session_checkpoints(session: Session) -> list[SessionCheckpoint]:
        # 从会话 metadata.checkpoints 反序列化出检查点列表（容错非法结构）
        raw_items = session.metadata.get("checkpoints") if session.metadata else []
        if not isinstance(raw_items, list):
            return []
        checkpoints: list[SessionCheckpoint] = []
        for item in raw_items:
            if isinstance(item, dict):
                checkpoints.append(SessionCheckpoint(**item))
        return checkpoints

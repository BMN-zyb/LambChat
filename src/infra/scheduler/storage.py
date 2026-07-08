"""MongoDB storage for scheduled tasks and run records."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError

from src.infra.logging import get_logger
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.scheduled_task import (
    RunStatus,
    ScheduledTask,
    ScheduledTaskStatus,
    TaskRunRecord,
)

logger = get_logger(__name__)

# 三个 collection 的职责划分：
#   scheduled_tasks         存任务定义本身（cron/interval/date 等触发配置、启用状态等）
#   task_run_records        存每一次实际执行留下的运行记录
#   scheduled_task_metadata 存少量元信息，目前只用来维护下面这个"定义版本号"
_COLL_TASKS = "scheduled_tasks"
_COLL_RUNS = "task_run_records"
_COLL_METADATA = "scheduled_task_metadata"
# 对应 scheduled_task_metadata 里的一个文档：维护一个单调递增的 revision。
# 任务定义发生增/删/改时都会 $inc 一次（见 _bump_scheduler_definition_revision）。
# 其他进程/协程只需比较这个数字是否变化，就能以 O(1) 判断"任务定义有没有被别的实例改过"，
# 而不必每次都拉取全部任务列表来 diff —— 这是多实例部署下调度器判断是否需要重新加载任务的关键机制。
_SCHEDULER_DEFINITION_REVISION_ID = "scheduler_definition_revision"

# 执行一个任务时只需要这些字段（不需要历史统计等完整字段），
# 用投影减少从 Mongo 读取和网络传输的数据量——这是调度触发的高频路径
_TASK_EXECUTION_PROJECTION = {
    "_id": 1,
    "name": 1,
    "agent_id": 1,
    "trigger_type": 1,
    "trigger_config": 1,
    "input_payload": 1,
    "status": 1,
    "enabled": 1,
    "run_on_start": 1,
    "max_retries": 1,
    "timeout_seconds": 1,
    "owner_id": 1,
    "delivery": 1,
    "last_run_at": 1,
    "total_runs": 1,
    "created_at": 1,
}


class ScheduledTaskStorage:
    """MongoDB CRUD for scheduled task definitions and run records."""

    def __init__(self) -> None:
        self._collections: dict[str, Any] = {}

    def _get_collection(self, name: str):
        """Lazy-load a MongoDB collection."""
        if name not in self._collections:
            from src.infra.storage.mongodb import get_mongo_client

            # 复用全局共享的 motor 客户端，不为定时任务存储单独开连接
            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collections[name] = db[name]
        return self._collections[name]

    async def ensure_indexes(self) -> None:
        """Create indexes for scheduled task and run record collections."""
        c_tasks = self._get_collection(_COLL_TASKS)
        # 按创建者过滤任务列表
        await c_tasks.create_index("owner_id")
        # 按状态过滤（活跃/暂停/已删除等）
        await c_tasks.create_index("status")
        # 复合索引：加速调度器启动时"只加载活跃且启用"的查询（见 list_active_tasks）
        await c_tasks.create_index([("status", 1), ("enabled", 1)])
        # 复合索引：加速"某会话下、某状态、按创建时间倒序"的分页查询
        await c_tasks.create_index(
            [
                ("owner_id", 1),
                ("source_session_id", 1),
                ("status", 1),
                ("created_at", -1),
            ],
            name="owner_source_session_status_created_idx",
        )

        c_runs = self._get_collection(_COLL_RUNS)
        # 按任务查运行记录
        await c_runs.create_index("task_id")
        # 复合索引：加速"某任务的历史运行记录按时间倒序分页"（见 list_runs）
        await c_runs.create_index([("task_id", 1), ("created_at", -1)])
        await c_runs.create_index("session_id")
        await c_runs.create_index("status")
        await c_runs.create_index("started_at")
        logger.info("[ScheduledTaskStorage] indexes created")

    # ── Task CRUD ──────────────────────────────────

    async def create_task(self, task: ScheduledTask) -> ScheduledTask:
        doc = task.model_dump(by_alias=True)
        collection = self._get_collection(_COLL_TASKS)
        try:
            await collection.insert_one(doc)
        except DuplicateKeyError:
            # 冲突可能是因为同名任务此前被"软删除"（status=DELETED），但唯一索引仍占用着这个名字。
            # 先尝试清理这类历史软删除记录，成功清理后重试一次插入；
            # 如果冲突的并不是软删除记录（比如真的撞上了另一个活跃的同名任务），则原样重新抛出。
            if not await self._delete_deleted_name_collision(task):
                raise
            await collection.insert_one(doc)
        # 创建成功后 bump 一次定义版本号，通知其他实例"任务定义发生了变化"
        await self._bump_scheduler_definition_revision()
        return task

    async def _delete_deleted_name_collision(self, task: ScheduledTask) -> bool:
        """Remove a historical soft-deleted same-name task so unique indexes stop blocking create."""
        # 只删除同一 owner 下、同名、且状态为 DELETED 的记录；
        # 返回值供 create_task 判断这次唯一索引冲突是否已经被清理掉
        collection = self._get_collection(_COLL_TASKS)
        result = await collection.delete_one(
            {
                "owner_id": task.owner_id,
                "name": task.name,
                "status": ScheduledTaskStatus.DELETED,
            }
        )
        return result.deleted_count > 0

    async def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        doc = await self._get_collection(_COLL_TASKS).find_one({"_id": task_id})
        if not doc:
            return None
        return ScheduledTask(**doc)

    async def get_task_for_execution(self, task_id: str) -> Optional[ScheduledTask]:
        """Read only fields needed to execute a scheduled task."""
        # 使用 _TASK_EXECUTION_PROJECTION 精简字段，减少这条高频（每次触发都要查）路径的读取开销
        doc = await self._get_collection(_COLL_TASKS).find_one(
            {"_id": task_id},
            _TASK_EXECUTION_PROJECTION,
        )
        if not doc:
            return None
        return ScheduledTask(**doc)

    async def list_tasks(
        self,
        owner_id: Optional[str] = None,
        status: Optional[ScheduledTaskStatus] = None,
    ) -> list[ScheduledTask]:
        query: dict[str, Any] = {}
        if owner_id:
            query["owner_id"] = owner_id
        if status:
            query["status"] = status
        else:
            # 不显式指定状态时，默认排除已软删除的任务——这是最常用的"看得见的任务列表"语义
            query["status"] = {"$ne": ScheduledTaskStatus.DELETED}
        cursor = self._get_collection(_COLL_TASKS).find(query).sort("created_at", -1)
        return [ScheduledTask(**doc) async for doc in cursor]

    async def list_tasks_paginated(
        self,
        owner_id: str,
        status: Optional[ScheduledTaskStatus] = None,
        source_session_id: Optional[str] = None,
        created_by: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[ScheduledTask], int]:
        """List tasks with pagination, scoped by owner_id."""
        query: dict[str, Any] = {"owner_id": owner_id}
        if status:
            query["status"] = status
        else:
            query["status"] = {"$ne": ScheduledTaskStatus.DELETED}
        if source_session_id:
            query["source_session_id"] = source_session_id
        if created_by:
            query["created_by"] = created_by
        collection = self._get_collection(_COLL_TASKS)

        async def _fetch_tasks() -> list[ScheduledTask]:
            cursor = collection.find(query).sort("created_at", -1).skip(skip).limit(limit)
            return [ScheduledTask(**doc) async for doc in cursor]

        # 分页数据查询与总数统计并发执行，减少一次往返等待
        tasks, total = await asyncio.gather(
            _fetch_tasks(),
            collection.count_documents(query),
        )
        return tasks, total

    async def list_active_tasks(self) -> list[ScheduledTask]:
        """List all active and enabled tasks (used at startup to reload scheduler)."""
        # 只有同时满足 status=ACTIVE 且 enabled=True 才会被重新装载进 APScheduler；
        # 处于 PAUSED/DELETED 或被显式禁用的任务都不会出现在这里
        cursor = self._get_collection(_COLL_TASKS).find(
            {"status": ScheduledTaskStatus.ACTIVE, "enabled": True}
        )
        return [ScheduledTask(**doc) async for doc in cursor]

    async def get_active_tasks_marker(self) -> int:
        """Return an O(1) scheduler-definition revision marker."""
        # 读取 _bump_scheduler_definition_revision 维护的那个单调递增计数器；
        # 调用方（调度器 runner）可周期性地比较这个数字是否变化，据此判断是否需要重新拉取
        # list_active_tasks，而不必每次都真正查一遍任务列表做 diff
        doc = await self._get_collection(_COLL_METADATA).find_one(
            {"_id": _SCHEDULER_DEFINITION_REVISION_ID}
        )
        if not doc:
            # 计数文档尚不存在，视为版本 0（从未发生过变更）
            return 0
        return int(doc.get("revision") or 0)

    async def update_task(self, task_id: str, updates: dict[str, Any]) -> bool:
        # 更新任务定义的任意字段（updates 会作为 $set 的键值对），并自动补上 updated_at；
        # 只有真的有字段被修改（modified_count > 0）才 bump 定义版本号，
        # 避免无实际变化的更新也触发其他实例重新加载任务列表
        updates["updated_at"] = utc_now()
        result = await self._get_collection(_COLL_TASKS).update_one(
            {"_id": task_id},
            {"$set": updates},
        )
        if result.modified_count > 0:
            await self._bump_scheduler_definition_revision()
        return result.modified_count > 0

    async def delete_task(self, task_id: str) -> bool:
        """Physically remove a task document so its name is freed immediately."""
        # 与"软删除"（把 status 置为 DELETED）不同，这里是物理删除文档，
        # 目的是让同名任务可以立即被重新创建，无需再走 _delete_deleted_name_collision 的兼容路径
        result = await self._get_collection(_COLL_TASKS).delete_one({"_id": task_id})
        if result.deleted_count > 0:
            await self._bump_scheduler_definition_revision()
        return result.deleted_count > 0

    async def update_task_run_stats(self, task_id: str, run_id: str, run_status: RunStatus) -> None:
        """Update task-level run statistics after execution completes."""
        # 用 $inc 原子递增 total_runs，避免"先读旧值再写回"在并发执行时产生竞态导致计数丢失
        now = utc_now()
        await self._get_collection(_COLL_TASKS).update_one(
            {"_id": task_id},
            {
                "$set": {
                    "last_run_at": now,
                    "last_run_status": run_status,
                    "last_run_id": run_id,
                    "updated_at": now,
                },
                "$inc": {"total_runs": 1},
            },
        )

    # ── Run Records ────────────────────────────────

    async def create_run(self, record: TaskRunRecord) -> TaskRunRecord:
        # 写入一条运行记录：每次任务被触发执行（无论成功与否）都会创建一条
        doc = record.model_dump(by_alias=True)
        await self._get_collection(_COLL_RUNS).insert_one(doc)
        return record

    async def get_run(self, run_id: str) -> Optional[TaskRunRecord]:
        doc = await self._get_collection(_COLL_RUNS).find_one({"_id": run_id})
        if not doc:
            return None
        return TaskRunRecord(**doc)

    async def update_run(self, run_id: str, updates: dict[str, Any]) -> bool:
        result = await self._get_collection(_COLL_RUNS).update_one(
            {"_id": run_id},
            {"$set": updates},
        )
        return result.modified_count > 0

    async def list_runs(
        self,
        task_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[TaskRunRecord], int]:
        query: dict[str, Any] = {"task_id": task_id}
        collection = self._get_collection(_COLL_RUNS)

        async def _fetch_records() -> list[TaskRunRecord]:
            cursor = collection.find(query).sort("created_at", -1).skip(offset).limit(limit)
            return [TaskRunRecord(**doc) async for doc in cursor]

        # 同样把分页查询和总数统计并发执行
        records, total = await asyncio.gather(
            _fetch_records(),
            collection.count_documents(query),
        )
        return records, total

    async def _bump_scheduler_definition_revision(self) -> None:
        # 对 _SCHEDULER_DEFINITION_REVISION_ID 文档执行 $inc revision + 更新 updated_at；
        # upsert=True 保证第一次调用时也能自动创建出这个计数文档。
        # 这是多实例场景下让其他进程感知"任务定义发生了变化"的核心机制
        # （读取端见上面的 get_active_tasks_marker）。
        await self._get_collection(_COLL_METADATA).update_one(
            {"_id": _SCHEDULER_DEFINITION_REVISION_ID},
            {
                "$inc": {"revision": 1},
                "$set": {"updated_at": utc_now()},
            },
            upsert=True,
        )


# ── Singleton ──────────────────────────────────────

# 进程级单例：一个进程内只需要一份定时任务存储实例
_storage: Optional[ScheduledTaskStorage] = None


def get_scheduled_task_storage() -> ScheduledTaskStorage:
    """Get the module-level ScheduledTaskStorage singleton."""
    global _storage
    if _storage is None:
        _storage = ScheduledTaskStorage()
    return _storage


def close_scheduled_task_storage() -> None:
    """Release the module-level ScheduledTaskStorage without creating it."""
    global _storage
    _storage = None

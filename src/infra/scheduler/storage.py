"""MongoDB storage for scheduled tasks and run records."""

from __future__ import annotations

from typing import Any, Optional

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

_COLL_TASKS = "scheduled_tasks"
_COLL_RUNS = "task_run_records"


class ScheduledTaskStorage:
    """MongoDB CRUD for scheduled task definitions and run records."""

    def __init__(self) -> None:
        self._collections: dict[str, Any] = {}

    def _get_collection(self, name: str):
        """Lazy-load a MongoDB collection."""
        if name not in self._collections:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collections[name] = db[name]
        return self._collections[name]

    async def ensure_indexes(self) -> None:
        """Create indexes for scheduled task and run record collections."""
        c_tasks = self._get_collection(_COLL_TASKS)
        await c_tasks.create_index("owner_id")
        await c_tasks.create_index("status")
        await c_tasks.create_index([("status", 1), ("enabled", 1)])
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
        await c_runs.create_index("task_id")
        await c_runs.create_index([("task_id", 1), ("created_at", -1)])
        await c_runs.create_index("session_id")
        await c_runs.create_index("status")
        await c_runs.create_index("started_at")
        logger.info("[ScheduledTaskStorage] indexes created")

    # ── Task CRUD ──────────────────────────────────

    async def create_task(self, task: ScheduledTask) -> ScheduledTask:
        doc = task.model_dump(by_alias=True)
        await self._get_collection(_COLL_TASKS).insert_one(doc)
        return task

    async def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        doc = await self._get_collection(_COLL_TASKS).find_one({"_id": task_id})
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
        total = await self._get_collection(_COLL_TASKS).count_documents(query)
        cursor = (
            self._get_collection(_COLL_TASKS)
            .find(query)
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        tasks = [ScheduledTask(**doc) async for doc in cursor]
        return tasks, total

    async def list_active_tasks(self) -> list[ScheduledTask]:
        """List all active and enabled tasks (used at startup to reload scheduler)."""
        cursor = self._get_collection(_COLL_TASKS).find(
            {"status": ScheduledTaskStatus.ACTIVE, "enabled": True}
        )
        return [ScheduledTask(**doc) async for doc in cursor]

    async def update_task(self, task_id: str, updates: dict[str, Any]) -> bool:
        updates["updated_at"] = utc_now()
        result = await self._get_collection(_COLL_TASKS).update_one(
            {"_id": task_id},
            {"$set": updates},
        )
        return result.modified_count > 0

    async def delete_task(self, task_id: str) -> bool:
        """Soft-delete a task by setting status to deleted."""
        result = await self._get_collection(_COLL_TASKS).update_one(
            {"_id": task_id},
            {"$set": {"status": ScheduledTaskStatus.DELETED, "updated_at": utc_now()}},
        )
        return result.modified_count > 0

    async def update_task_run_stats(self, task_id: str, run_id: str, run_status: RunStatus) -> None:
        """Update task-level run statistics after execution completes."""
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
        total = await self._get_collection(_COLL_RUNS).count_documents(query)
        cursor = (
            self._get_collection(_COLL_RUNS)
            .find(query)
            .sort("created_at", -1)
            .skip(offset)
            .limit(limit)
        )
        records = [TaskRunRecord(**doc) async for doc in cursor]
        return records, total


# ── Singleton ──────────────────────────────────────

_storage: Optional[ScheduledTaskStorage] = None


def get_scheduled_task_storage() -> ScheduledTaskStorage:
    """Get the module-level ScheduledTaskStorage singleton."""
    global _storage
    if _storage is None:
        _storage = ScheduledTaskStorage()
    return _storage

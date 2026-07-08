"""
技能文件存储层

以单一集合 skill_files 保存所有技能文件，(skill_name, user_id, file_path) 唯一。
约定：
- file_path == "__meta__" 的特殊文档保存技能元信息（安装来源等）；
- 二进制文件实体存于 S3/本地，MongoDB 只存其 JSON 引用（见 binary.py）；
- 启用/禁用状态不落在本集合，而是记于用户 metadata.disabled_skills；
- 生效技能结果带 Redis 缓存，写操作后需失效缓存。
"""

import json
import re
from typing import TYPE_CHECKING, Any, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.skill.binary import (
    BINARY_REF_MARKER,
    SkillBinaryRef,
    build_binary_ref_content,
    build_storage_key,
    guess_mime_type,
    parse_binary_ref_async,
)
from src.infra.skill.constants import SKILL_FILES_COLLECTION
from src.infra.skill.storage_helpers import (
    SKILL_BATCH_FILE_LOOKUP_LIMIT,
    SKILL_EFFECTIVE_LOAD_LIMIT,
    SKILL_FILES_PER_SKILL_LIMIT,
    SKILL_MD_SCAN_LIMIT,
    SKILL_METADATA_LIST_LIMIT,
    normalize_skill_file_path,
    normalize_skill_files,
    normalize_skill_name_list,
)
from src.infra.skill.types import InstalledFrom, SkillMeta
from src.infra.storage.mongodb import get_mongo_client
from src.infra.utils.datetime import utc_now_iso
from src.kernel.config import settings

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

logger = get_logger(__name__)


async def _parse_skill_md_offload(content: str) -> tuple[Optional[str], str, list[str]]:
    # 将 SKILL.md 解析放线程池执行，避免阻塞事件循环
    from src.infra.skill.parser import parse_skill_md

    return await run_blocking_io(parse_skill_md, content)


class SkillStorage:
    def __init__(self):
        # 客户端与集合延迟初始化
        self._client: Optional["AsyncIOMotorClient"] = None
        self._files_collection: Optional["AsyncIOMotorCollection"] = None

    # 用户置顶/收藏技能的数量上限
    MAX_PINNED = 10
    MAX_FAVORITES = 100

    def _get_files_collection(self) -> "AsyncIOMotorCollection":
        # 惰性获取 skill_files 集合
        if self._files_collection is None:
            self._client = get_mongo_client()
            db = self._client[settings.MONGODB_DB]
            self._files_collection = db[SKILL_FILES_COLLECTION]
        return self._files_collection

    async def ensure_indexes(self) -> None:
        """创建索引"""
        # (skill_name, user_id, file_path) 唯一复合索引，保证同一文件不重复
        files = self._get_files_collection()
        await files.create_index(
            [("skill_name", 1), ("user_id", 1), ("file_path", 1)],
            unique=True,
            background=True,
        )

    # ==========================================
    # 文件操作
    # ==========================================

    async def get_skill_files(self, skill_name: str, user_id: str) -> dict[str, str]:
        """获取用户某个 Skill 的所有文件（排除 __meta__）"""
        collection = self._get_files_collection()
        files: dict[str, str] = {}
        # 多取 1 条用于判断是否超限；实际仍按上限截断
        cursor = collection.find({"skill_name": skill_name, "user_id": user_id}).limit(
            SKILL_FILES_PER_SKILL_LIMIT + 1
        )
        async for doc in cursor:
            # __meta__ 是元信息文档，不作为技能内容返回
            if doc["file_path"] != "__meta__":
                files[normalize_skill_file_path(doc["file_path"])] = doc["content"]
                if len(files) >= SKILL_FILES_PER_SKILL_LIMIT:
                    break
        return files

    async def get_skill_file(self, skill_name: str, file_path: str, user_id: str) -> Optional[str]:
        """获取用户某个 Skill 的单个文件"""
        collection = self._get_files_collection()
        file_path = normalize_skill_file_path(file_path)
        doc = await collection.find_one(
            {
                "skill_name": skill_name,
                "user_id": user_id,
                "file_path": file_path,
            }
        )
        # 兼容旧数据：找不到标准 SKILL.md 时，用大小写不敏感正则回退匹配 skill.md 等变体
        if not doc and (file_path.endswith("/SKILL.md") or file_path == "SKILL.md"):
            legacy_path_pattern = re.escape(file_path[:-8]) + r"skill\.md"
            doc = await collection.find_one(
                {
                    "skill_name": skill_name,
                    "user_id": user_id,
                    "file_path": {"$regex": f"^{legacy_path_pattern}$", "$options": "i"},
                }
            )
        return doc["content"] if doc else None

    async def set_skill_file(
        self, skill_name: str, file_path: str, content: str, user_id: str
    ) -> None:
        """原子 upsert 单个文件（文本内容）"""
        collection = self._get_files_collection()
        file_path = normalize_skill_file_path(file_path)
        now = utc_now_iso()
        # upsert：存在则更新内容与 updated_at，不存在则插入并记录 created_at
        await collection.update_one(
            {"skill_name": skill_name, "user_id": user_id, "file_path": file_path},
            {
                "$set": {"content": content, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    async def set_skill_binary_file(
        self,
        skill_name: str,
        file_path: str,
        data: bytes,
        user_id: str,
        mime_type: Optional[str] = None,
    ) -> SkillBinaryRef:
        """上传二进制文件到 S3/本地存储，并在 MongoDB 存储引用。"""
        from src.infra.storage.s3.service import get_or_init_storage

        # 未显式指定 MIME 时按文件名猜测
        if not mime_type:
            mime_type = guess_mime_type(file_path)

        # 生成对象存储 key 并上传实体
        storage_key = build_storage_key(user_id, skill_name, file_path)
        storage_service = await get_or_init_storage()

        # 上传到 S3/本地存储
        await storage_service.upload_to_key(
            data=data,
            key=storage_key,
            content_type=mime_type,
            skip_size_limit=True,  # size already validated at API layer
        )

        # 构建引用并存入 MongoDB
        # MongoDB 只存指向对象存储的引用（含 key/类型/大小），不存二进制本体
        ref_content = build_binary_ref_content(storage_key, mime_type, len(data))
        await self.set_skill_file(skill_name, file_path, ref_content, user_id)

        return SkillBinaryRef(
            storage_key=storage_key,
            mime_type=mime_type,
            size=len(data),
        )

    async def update_skill_file_cas(
        self,
        skill_name: str,
        file_path: str,
        expected_content: str,
        new_content: str,
        user_id: str,
    ) -> bool:
        """
        Compare-and-swap: 仅当当前内容匹配 expected_content 时才更新。
        用于防止并发编辑丢失更新。

        Returns:
            True 如果更新成功，False 如果内容已被其他人修改
        """
        collection = self._get_files_collection()
        file_path = normalize_skill_file_path(file_path)
        now = utc_now_iso()
        # 把 expected_content 放入过滤条件：内容不匹配则匹配不到文档，更新数为 0
        result = await collection.update_one(
            {
                "skill_name": skill_name,
                "user_id": user_id,
                "file_path": file_path,
                "content": expected_content,
            },
            {
                "$set": {"content": new_content, "updated_at": now},
            },
        )
        return result.modified_count > 0

    async def delete_skill_file(self, skill_name: str, file_path: str, user_id: str) -> None:
        """删除单个文件（如果是二进制引用，同时删除 S3 对象）"""
        collection = self._get_files_collection()
        file_path = normalize_skill_file_path(file_path)
        doc = await collection.find_one(
            {"skill_name": skill_name, "user_id": user_id, "file_path": file_path},
        )
        if doc:
            # 检查是否为二进制引用，如果是则删除 S3 对象
            # 避免只删 MongoDB 引用而遗留 S3 孤儿对象
            binary_ref = await parse_binary_ref_async(doc.get("content", ""))
            if binary_ref:
                await self._delete_s3_object(binary_ref.storage_key)
            await collection.delete_one(
                {"skill_name": skill_name, "user_id": user_id, "file_path": file_path},
            )

    async def _delete_s3_object(self, storage_key: str) -> None:
        """删除 S3/本地存储中的文件"""
        # 清理对象存储失败不致命，仅告警（下次仍可重试或由清理任务处理）
        try:
            from src.infra.storage.s3.service import get_or_init_storage

            storage_service = await get_or_init_storage()
            await storage_service.delete_file(storage_key)
        except Exception as e:
            logger.warning(f"Failed to delete S3 object {storage_key}: {e}")

    async def sync_skill_files(self, skill_name: str, files: dict[str, str], user_id: str) -> None:
        """批量同步文件（替换所有，但保留 __meta__）。支持文本和二进制引用。"""
        # 归一化路径；空则不操作（避免误删）
        files = normalize_skill_files(files)
        if not files:
            return
        # 文件数超限直接拒绝，防止单技能膨胀
        if len(files) > SKILL_FILES_PER_SKILL_LIMIT:
            raise ValueError(f"Skill contains too many files (max {SKILL_FILES_PER_SKILL_LIMIT})")
        collection = self._get_files_collection()
        now = utc_now_iso()

        from pymongo import UpdateOne

        operations: list = []

        # 需删除的文件 = 既非 __meta__、也不在本次传入集合中的旧文件
        removed_query = {
            "skill_name": skill_name,
            "user_id": user_id,
            "file_path": {"$ne": "__meta__", "$nin": list(files.keys())},
        }

        # Only scan removed binary references for S3 cleanup; Mongo handles row deletion.
        # 删除前，先扫出这些旧文件里的二进制引用，收集待清理的 S3 key
        s3_keys_to_delete: list[str] = []
        removed_binary_cursor = collection.find(
            {
                **removed_query,
                "content": {"$regex": BINARY_REF_MARKER},
            },
            {"content": 1},
        ).limit(SKILL_FILES_PER_SKILL_LIMIT)
        async for doc in removed_binary_cursor:
            binary_ref = await parse_binary_ref_async(doc.get("content", ""))
            if binary_ref:
                s3_keys_to_delete.append(binary_ref.storage_key)

        # 删除旧文件行
        await collection.delete_many(removed_query)

        # 对本次传入的每个文件做 upsert
        for file_path, content in files.items():
            operations.append(
                UpdateOne(
                    {"skill_name": skill_name, "user_id": user_id, "file_path": file_path},
                    {
                        "$set": {"content": content, "updated_at": now},
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                )
            )

        # 批量写入，减少往返
        if operations:
            await collection.bulk_write(operations, ordered=True)

        # 批量删除 S3 对象
        # 行删除后再清理对象存储，保证引用先失效
        for s3_key in s3_keys_to_delete:
            await self._delete_s3_object(s3_key)

    async def upsert_skill_files_batch(
        self,
        skill_name: str,
        files: dict[str, str],
        user_id: str,
    ) -> int:
        """Upsert a bounded batch of text skill files without deleting other paths."""
        # 与 sync 不同：只新增/更新给定文件，不删除其他既有文件
        files = normalize_skill_files(files)
        if not files:
            return 0

        from pymongo import UpdateOne

        collection = self._get_files_collection()
        now = utc_now_iso()
        operations = [
            UpdateOne(
                {"skill_name": skill_name, "user_id": user_id, "file_path": file_path},
                {
                    "$set": {"content": content, "updated_at": now},
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            for file_path, content in files.items()
        ]
        await collection.bulk_write(operations, ordered=True)
        return len(operations)

    async def delete_skill_files(self, skill_name: str, user_id: str) -> None:
        """删除用户某个 Skill 的所有文件（包括 S3 二进制清理）"""
        collection = self._get_files_collection()

        # 先收集所有二进制引用，清理 S3
        # 遍历非 __meta__ 文件，删除其对应的 S3 对象
        async for doc in collection.find(
            {"skill_name": skill_name, "user_id": user_id, "file_path": {"$ne": "__meta__"}},
            {"content": 1},
        ):
            binary_ref = await parse_binary_ref_async(doc.get("content", ""))
            if binary_ref:
                await self._delete_s3_object(binary_ref.storage_key)

        # 注意：此处保留 __meta__ 文档（仅删内容文件）
        await collection.delete_many(
            {
                "skill_name": skill_name,
                "user_id": user_id,
            }
        )

    async def list_skill_file_paths(self, skill_name: str, user_id: str) -> list[str]:
        """列出用户某个 Skill 的所有文件路径（排除 __meta__）"""
        collection = self._get_files_collection()
        paths = []
        # 投影只取 file_path，减少数据传输
        cursor = collection.find(
            {"skill_name": skill_name, "user_id": user_id, "file_path": {"$ne": "__meta__"}},
            {"file_path": 1},
        ).limit(SKILL_FILES_PER_SKILL_LIMIT)
        async for doc in cursor:
            paths.append(normalize_skill_file_path(doc["file_path"]))
        return paths

    async def get_skill_file_stats(self, skill_name: str, user_id: str) -> dict[str, Any]:
        """获取单个 Skill 的文件统计信息（created_at/updated_at 来自文件聚合，排除 __meta__）"""
        collection = self._get_files_collection()
        # 聚合：统计文件数，并取最早创建/最晚更新时间作为技能的时间戳
        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "skill_name": skill_name,
                    "user_id": user_id,
                    "file_path": {"$ne": "__meta__"},
                }
            },
            {
                "$group": {
                    "_id": "$skill_name",
                    "file_count": {"$sum": 1},
                    "created_at": {"$min": "$created_at"},
                    "updated_at": {"$max": "$updated_at"},
                }
            },
        ]
        async for doc in collection.aggregate(pipeline):  # type: ignore[arg-type]
            return {
                "file_count": doc["file_count"],
                "created_at": doc.get("created_at"),
                "updated_at": doc.get("updated_at"),
            }
        # 无文件时返回零值默认
        return {"file_count": 0, "created_at": None, "updated_at": None}

    async def list_user_skills(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 100,
        disabled_skills: Optional[list[str]] = None,
        pinned_skill_names: Optional[list[str]] = None,
        favorite_skill_names: Optional[list[str]] = None,
        q: str | None = None,
        tags: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """列出用户所有 Skill（带文件信息）

        Args:
            user_id: 用户 ID
            skip: 分页跳过数量
            limit: 分页限制
            disabled_skills: 从用户 metadata 中获取的 disabled_skills 列表
        """
        # 参数缺省归一化为一致的空列表
        if disabled_skills is None:
            disabled_skills = []
        if pinned_skill_names is None:
            pinned_skill_names = []
        if favorite_skill_names is None:
            favorite_skill_names = []
        # 去重截断，防止超长输入
        disabled_skills = normalize_skill_name_list(disabled_skills)
        pinned_skill_names = normalize_skill_name_list(pinned_skill_names)
        favorite_skill_names = normalize_skill_name_list(favorite_skill_names)
        disabled_set = set(disabled_skills)
        pinned_set = set(pinned_skill_names)
        favorite_set = set(favorite_skill_names)
        # 是否需要按“置顶/收藏”排序（影响分页在聚合还是内存中进行）
        has_preferences = bool(pinned_set or favorite_set)

        collection = self._get_files_collection()
        paged_skill_names: list[str] | None = None
        # 有搜索/标签过滤时，先算出匹配的技能名集合
        if q or tags:
            matching_skill_names = await self.list_matching_skill_names(user_id, q=q, tags=tags)
            # 有偏好排序时先取全集（后续在聚合里排序分页），否则此处直接切片分页
            paged_skill_names = (
                matching_skill_names
                if has_preferences
                else matching_skill_names[skip : skip + limit]
            )
            if not paged_skill_names:
                return []

        # 使用 aggregation 一次获取所有 skill 的统计信息 + 文件路径（排除 __meta__）
        # 按 skill_name 分组，聚合文件数、路径列表与时间戳
        match: dict[str, Any] = {"user_id": user_id, "file_path": {"$ne": "__meta__"}}
        if paged_skill_names is not None:
            match["skill_name"] = {"$in": paged_skill_names}

        pipeline: list[dict[str, Any]] = [
            {"$match": match},
            {
                "$group": {
                    "_id": "$skill_name",
                    "file_count": {"$sum": 1},
                    "file_paths": {"$push": "$file_path"},
                    "created_at": {"$min": "$created_at"},
                    "updated_at": {"$max": "$updated_at"},
                }
            },
        ]
        if has_preferences:
            # 有置顶/收藏偏好：在聚合阶段打标并按 置顶 > 收藏 > 更新时间 排序后分页
            pipeline.extend(
                [
                    {
                        "$addFields": {
                            "_is_pinned": {"$in": ["$_id", pinned_skill_names]},
                            "_is_favorite": {"$in": ["$_id", favorite_skill_names]},
                            "_updated_sort": {"$ifNull": ["$updated_at", "$created_at"]},
                            "_created_sort": {"$ifNull": ["$created_at", "$updated_at"]},
                        }
                    },
                    {
                        "$sort": {
                            "_is_pinned": -1,
                            "_is_favorite": -1,
                            "_updated_sort": -1,
                            "_created_sort": -1,
                            "_id": 1,
                        }
                    },
                    {"$skip": skip},
                    {"$limit": limit},
                ]
            )
        else:
            # 无偏好：按名称排序；若未在前面按过滤名切片，则此处做分页
            pipeline.append({"$sort": {"_id": 1}})
            if paged_skill_names is None:
                pipeline.extend([{"$skip": skip}, {"$limit": limit}])
        skill_stats: dict[str, dict] = {}
        async for doc in collection.aggregate(pipeline):  # type: ignore[arg-type]
            skill_stats[doc["_id"]] = {
                "file_count": doc["file_count"],
                "file_paths": doc.get("file_paths", []),
                "created_at": doc.get("created_at"),
                "updated_at": doc.get("updated_at"),
            }

        # 批量获取所有 __meta__ 文档
        # 一次性取回本页技能的元信息，避免逐个查询
        skill_names = list(skill_stats.keys())
        meta_map: dict[str, SkillMeta] = {}
        if skill_names:
            async for doc in collection.find(
                {"skill_name": {"$in": skill_names}, "user_id": user_id, "file_path": "__meta__"},
                {"skill_name": 1, "content": 1},
            ):
                try:
                    data = await run_blocking_io(json.loads, doc["content"])
                    meta_map[doc["skill_name"]] = SkillMeta(**data)
                except Exception:
                    # 单个 __meta__ 解析失败不影响整体
                    pass

        # 组装结果
        # 决定输出顺序：过滤且无偏好时沿用过滤名的顺序，否则用聚合结果顺序
        result = []
        ordered_names = list(skill_stats.keys())
        if paged_skill_names is not None and not has_preferences:
            ordered_names = paged_skill_names
        for skill_name in ordered_names:
            if skill_name not in skill_stats:
                continue
            stats = skill_stats[skill_name]
            meta = meta_map.get(skill_name)
            # enabled 由是否在禁用集合反推
            enabled = skill_name not in disabled_set

            result.append(
                {
                    "skill_name": skill_name,
                    "enabled": enabled,
                    "file_count": stats["file_count"],
                    "file_paths": stats.get("file_paths", []),
                    "installed_from": meta.installed_from.value if meta else None,
                    "published_marketplace_name": meta.published_marketplace_name if meta else None,
                    "created_at": stats.get("created_at"),
                    "updated_at": stats.get("updated_at"),
                    "is_pinned": skill_name in pinned_set,
                    "is_favorite": skill_name in favorite_set,
                }
            )

        return result

    async def _get_user_skill_preference(self, user_id: str) -> dict[str, list[str]]:
        # 置顶/收藏偏好同样存于用户 metadata，读取时按各自上限归一化
        from src.infra.user.storage import UserStorage

        user_doc = await UserStorage().get_by_id(user_id)
        metadata = (user_doc.metadata if user_doc else None) or {}
        return {
            "pinned": normalize_skill_name_list(
                metadata.get("pinned_skill_names"),
                self.MAX_PINNED,
            ),
            "favorite": normalize_skill_name_list(
                metadata.get("favorite_skill_names"),
                self.MAX_FAVORITES,
            ),
        }

    async def update_user_preference(
        self,
        *,
        user_id: str,
        skill_name: str,
        update: dict[str, Any],
    ) -> dict[str, Any]:
        """Update the current user's favorite/pinned state for a skill."""
        from src.infra.user.storage import UserStorage

        # 读现有偏好并复制成可变列表
        pref = await self._get_user_skill_preference(user_id)
        pinned: list[str] = list(pref["pinned"])
        favorite: list[str] = list(pref["favorite"])

        # 处理置顶：置顶且超上限则直接返回（不置顶），取消置顶则移除
        if update.get("is_pinned") is not None:
            if update["is_pinned"] and skill_name not in pinned:
                if len(pinned) >= self.MAX_PINNED:
                    return {
                        "is_favorite": skill_name in favorite,
                        "is_pinned": False,
                    }
                pinned.append(skill_name)
            elif not update["is_pinned"] and skill_name in pinned:
                pinned.remove(skill_name)

        # 处理收藏：逻辑同置顶，受 MAX_FAVORITES 限制
        if update.get("is_favorite") is not None:
            if update["is_favorite"] and skill_name not in favorite:
                if len(favorite) >= self.MAX_FAVORITES:
                    return {
                        "is_favorite": False,
                        "is_pinned": skill_name in pinned,
                    }
                favorite.append(skill_name)
            elif not update["is_favorite"] and skill_name in favorite:
                favorite.remove(skill_name)

        # 写回用户 metadata
        await UserStorage().update_metadata(
            user_id,
            {
                "pinned_skill_names": pinned,
                "favorite_skill_names": favorite,
            },
        )
        return {
            "is_favorite": skill_name in favorite,
            "is_pinned": skill_name in pinned,
        }

    async def remove_user_skill_preference(self, user_id: str, skill_names: list[str]) -> None:
        """Remove deleted skill names from the current user's preference lists."""
        # 技能被删除后，清理其在置顶/收藏列表中的残留项
        from src.infra.user.storage import UserStorage

        remove_names = set(skill_names)
        if not remove_names:
            return

        pref = await self._get_user_skill_preference(user_id)
        pinned = [name for name in pref["pinned"] if name not in remove_names]
        favorite = [name for name in pref["favorite"] if name not in remove_names]
        # 无变化则跳过写入，减少无谓更新
        if pinned == pref["pinned"] and favorite == pref["favorite"]:
            return
        await UserStorage().update_metadata(
            user_id,
            {
                "pinned_skill_names": pinned,
                "favorite_skill_names": favorite,
            },
        )

    async def count_user_skills(
        self,
        user_id: str,
        q: str | None = None,
        tags: Optional[list[str]] = None,
    ) -> int:
        """Count user skills matching an optional name search."""
        # 有搜索/标签过滤时直接用匹配名个数（需解析 SKILL.md）
        if q or tags:
            return len(await self.list_matching_skill_names(user_id, q=q, tags=tags))
        collection = self._get_files_collection()
        match: dict[str, Any] = {"user_id": user_id, "file_path": {"$ne": "__meta__"}}
        if q:
            match["skill_name"] = {"$regex": q, "$options": "i"}
        # 按技能名去重后计数
        pipeline: list[dict[str, Any]] = [
            {"$match": match},
            {"$group": {"_id": "$skill_name"}},
            {"$count": "total"},
        ]
        async for doc in collection.aggregate(pipeline):  # type: ignore[arg-type]
            return int(doc.get("total", 0))
        return 0

    async def count_disabled_user_skills(
        self,
        user_id: str,
        disabled_skills: list[str],
        q: str | None = None,
        tags: Optional[list[str]] = None,
    ) -> int:
        """Count disabled skills that exist in the current list filters."""
        # 统计“当前过滤条件下”实际存在且被禁用的技能数（用于前端展示禁用计数）
        disabled_skills = normalize_skill_name_list(disabled_skills)
        disabled_set = set(disabled_skills)
        if not disabled_set:
            return 0

        # 有过滤时，取禁用集合与匹配集合的交集大小
        if q or tags:
            matching_names = await self.list_matching_skill_names(user_id, q=q, tags=tags)
            return len(disabled_set.intersection(matching_names))

        collection = self._get_files_collection()
        # 无过滤：统计禁用集合中确实存在文件的技能数
        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "user_id": user_id,
                    "file_path": {"$ne": "__meta__"},
                    "skill_name": {"$in": list(disabled_set)},
                }
            },
            {"$group": {"_id": "$skill_name"}},
            {"$count": "total"},
        ]
        async for doc in collection.aggregate(pipeline):  # type: ignore[arg-type]
            return int(doc.get("total", 0))
        return 0

    async def list_user_skill_tags(self, user_id: str) -> list[str]:
        """List all tags used by a user's skills."""
        # 复用扫描逻辑，只取其返回的标签集合
        _, tags = await self._list_matching_skill_names_and_tags(user_id)
        return tags

    async def list_matching_skill_names(
        self,
        user_id: str,
        q: str | None = None,
        tags: Optional[list[str]] = None,
    ) -> list[str]:
        """List skill names matching search text and all selected tags."""
        names, _ = await self._list_matching_skill_names_and_tags(user_id, q=q, tags=tags)
        return names

    async def _list_matching_skill_names_and_tags(
        self,
        user_id: str,
        q: str | None = None,
        tags: Optional[list[str]] = None,
    ) -> tuple[list[str], list[str]]:
        # 通过扫描各技能的 SKILL.md，解析出描述与标签，做搜索/标签过滤；
        # 同时收集所有出现过的标签供前端筛选面板使用
        collection = self._get_files_collection()
        q_lower = q.lower() if q else None
        selected_tags = set(tags or [])
        matching_names: list[str] = []
        available_tags: set[str] = set()

        # 限制扫描条数，避免技能数量极大时性能失控
        cursor = collection.find(
            {"user_id": user_id, "file_path": "SKILL.md"},
            {"skill_name": 1, "content": 1},
        ).limit(SKILL_MD_SCAN_LIMIT)
        async for doc in cursor:
            skill_name = doc["skill_name"]
            _, description, parsed_tags = await _parse_skill_md_offload(doc.get("content", ""))
            tag_set = set(parsed_tags)
            available_tags.update(tag_set)

            # 搜索词需命中 名称/描述/任一标签，否则跳过
            if q_lower and (
                q_lower not in skill_name.lower()
                and q_lower not in (description or "").lower()
                and not any(q_lower in tag.lower() for tag in parsed_tags)
            ):
                continue
            # 标签过滤为“与”语义：所选标签必须全部包含
            if selected_tags and not selected_tags.issubset(tag_set):
                continue
            matching_names.append(skill_name)

        # 名称与标签都排序，保证结果稳定
        return sorted(matching_names), sorted(available_tags)

    async def batch_get_skill_md_contents(
        self, skill_names: list[str], user_id: str
    ) -> dict[str, str]:
        """批量获取多个 skill 的 SKILL.md 内容"""
        skill_names = normalize_skill_name_list(skill_names, SKILL_METADATA_LIST_LIMIT)
        if not skill_names:
            return {}
        collection = self._get_files_collection()
        docs = {}
        # 一次性用 $in 拉取多个技能的主文件内容
        async for doc in collection.find(
            {"skill_name": {"$in": skill_names}, "user_id": user_id, "file_path": "SKILL.md"},
            {"skill_name": 1, "content": 1},
        ):
            docs[doc["skill_name"]] = doc.get("content", "")
        return docs

    async def batch_get_skill_files(
        self, skill_keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, str]]:
        """批量获取多个 Skill 的文件"""
        if not skill_keys:
            return {}

        collection = self._get_files_collection()

        # 去重
        # (skill_name, user_id) 去重并截断，构造查询子句
        seen: set[tuple[str, str]] = set()
        or_clauses = []
        for skill_name, user_id in skill_keys:
            key = (skill_name, user_id)
            if key not in seen:
                seen.add(key)
                or_clauses.append({"skill_name": skill_name, "user_id": user_id})
                if len(or_clauses) >= SKILL_BATCH_FILE_LOOKUP_LIMIT:
                    break

        # 逐个技能查询其文件（排除 __meta__），按 (name,user) 归组返回
        result: dict[tuple[str, str], dict[str, str]] = {}
        for clause in or_clauses:
            key = (clause["skill_name"], clause["user_id"])
            result[key] = {}
            cursor = collection.find(
                {
                    **clause,
                    "file_path": {"$ne": "__meta__"},
                }
            ).limit(SKILL_FILES_PER_SKILL_LIMIT)
            async for doc in cursor:
                result[key][normalize_skill_file_path(doc["file_path"])] = doc["content"]

        return result

    # ==========================================
    # Skill 元数据操作（存储在 __meta__ 文档中）
    # ==========================================

    async def get_skill_meta(self, skill_name: str, user_id: str) -> Optional[SkillMeta]:
        """获取 skill 元数据（从 __meta__ 文档）"""
        collection = self._get_files_collection()
        doc = await collection.find_one(
            {"skill_name": skill_name, "user_id": user_id, "file_path": "__meta__"}
        )
        if not doc:
            return None
        # __meta__ 内容是 JSON 字符串，解析为 SkillMeta
        try:
            data = await run_blocking_io(json.loads, doc["content"])
            return SkillMeta(**data)
        except Exception:
            return None

    async def set_skill_meta(
        self,
        skill_name: str,
        user_id: str,
        installed_from: InstalledFrom = InstalledFrom.MANUAL,
        published_marketplace_name: Optional[str] = None,
    ) -> None:
        """设置 skill 元数据（存储为 __meta__ 文档）"""
        collection = self._get_files_collection()
        now = utc_now_iso()
        # 组装并序列化元信息，作为特殊文件 __meta__ upsert
        meta = SkillMeta(
            installed_from=installed_from,
            published_marketplace_name=published_marketplace_name,
            created_at=now,
            updated_at=now,
        )
        content = await run_blocking_io(json.dumps, meta.model_dump())
        await collection.update_one(
            {"skill_name": skill_name, "user_id": user_id, "file_path": "__meta__"},
            {
                "$set": {"content": content, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    async def delete_skill_meta(self, skill_name: str, user_id: str) -> None:
        """删除 skill __meta__ 文档"""
        # 仅删除元信息文档，不动技能内容文件
        collection = self._get_files_collection()
        await collection.delete_one(
            {"skill_name": skill_name, "user_id": user_id, "file_path": "__meta__"}
        )

    async def delete_skill_and_meta(self, skill_name: str, user_id: str) -> None:
        """删除 Skill 所有文件（包括 __meta__ 和 S3 二进制文件）"""
        collection = self._get_files_collection()

        # 先收集所有二进制引用，清理 S3
        # 彻底删除：先清 S3 对象，再删除该技能的全部文档（含 __meta__）
        async for doc in collection.find(
            {"skill_name": skill_name, "user_id": user_id, "file_path": {"$ne": "__meta__"}},
            {"content": 1},
        ):
            binary_ref = await parse_binary_ref_async(doc.get("content", ""))
            if binary_ref:
                await self._delete_s3_object(binary_ref.storage_key)

        await collection.delete_many({"skill_name": skill_name, "user_id": user_id})

    # ==========================================
    # 生效 Skills（供 DeepAgent 使用）
    # ==========================================

    async def get_effective_skills(
        self, user_id: str, disabled_skills: Optional[list[str]] = None
    ) -> dict[str, dict[str, Any]]:
        """
        获取用户生效的 Skills（已启用 + 有文件）

        Args:
            user_id: 用户 ID
            disabled_skills: 从用户 metadata 中获取的 disabled_skills 列表

        Returns:
            {
                "skills": {
                    "skill_name": {
                        "files": {file_path: content},
                        "enabled": True,
                    }
                }
            }
        """
        from src.infra.skill.constants import SKILLS_CACHE_KEY_PREFIX, SKILLS_CACHE_TTL

        # 每个用户一个缓存 key
        cache_key = f"{SKILLS_CACHE_KEY_PREFIX}{user_id}"

        # 尝试从 Redis 缓存获取
        # 命中缓存直接返回，避免重复聚合与文件读取（技能加载在对话链路上较热）
        try:
            from src.infra.storage.redis import get_redis_client

            redis_client = get_redis_client()
            cached = await redis_client.get(cache_key)
            if cached:
                return await run_blocking_io(json.loads, cached)
        except Exception as e:
            logger.warning(f"[Skills Cache] Redis get failed: {e}")

        # 未传禁用列表时从用户 metadata 读取（保证缓存未命中路径行为一致）
        if disabled_skills is None:
            disabled_skills = await self._get_user_disabled_skills(user_id)
        disabled_skills = normalize_skill_name_list(disabled_skills)

        # 取“启用”的技能名（排除禁用项），并限制加载上限
        enabled_names = await self.get_all_user_skill_names(
            user_id,
            exclude_skill_names=disabled_skills,
            limit=SKILL_EFFECTIVE_LOAD_LIMIT,
        )

        if not enabled_names:
            return {"skills": {}}

        # 批量获取文件
        skill_keys = [(name, user_id) for name in enabled_names]
        files_map = await self.batch_get_skill_files(skill_keys)

        result: dict[str, Any] = {"skills": {}}
        for name in enabled_names:
            files = files_map.get((name, user_id), {})
            if files:  # 只包含有文件的 skill
                # 从 SKILL.md frontmatter 解析 description
                # 描述用于构建技能提示；解析失败则回退为通用描述
                description = ""
                if "SKILL.md" in files:
                    try:
                        _, parsed_desc, _ = await _parse_skill_md_offload(files["SKILL.md"])
                        if parsed_desc:
                            description = parsed_desc
                    except Exception:
                        pass

                result["skills"][name] = {
                    "name": name,
                    "description": description or f"Skill: {name}",
                    "files": files,
                    "enabled": True,
                }

        # 缓存
        # 写入 Redis 供后续复用；写操作会通过 invalidate_user_cache 失效此缓存
        try:
            from src.infra.storage.redis import get_redis_client

            redis_client = get_redis_client()

            serialized = await run_blocking_io(json.dumps, result)
            await redis_client.set(cache_key, serialized, ex=SKILLS_CACHE_TTL)
        except Exception as e:
            logger.warning(f"[Skills Cache] Redis set failed: {e}")

        return result

    async def _get_user_disabled_skills(self, user_id: str) -> list[str]:
        """Load disabled skills from user metadata for cache-safe default behavior."""
        # 从用户 metadata 读取禁用技能；异常时按无禁用处理
        try:
            from src.infra.user.storage import UserStorage

            user_storage = UserStorage()
            user_doc = await user_storage.get_by_id(user_id)
            if user_doc and user_doc.metadata:
                return normalize_skill_name_list(user_doc.metadata.get("disabled_skills", []))
        except Exception as e:
            logger.warning(f"Failed to load disabled_skills for user {user_id}: {e}")
        return []

    async def get_all_user_skill_names(
        self,
        user_id: str,
        exclude_skill_names: Optional[list[str]] = None,
        limit: Optional[int] = None,
    ) -> list[str]:
        """获取用户所有 skill 名称（无论 enabled/disabled，排除 __meta__）"""
        collection = self._get_files_collection()
        match: dict[str, Any] = {"user_id": user_id, "file_path": {"$ne": "__meta__"}}
        # 可选排除某些技能（如禁用项）
        excluded = normalize_skill_name_list(exclude_skill_names or [])
        if excluded:
            match["skill_name"] = {"$nin": excluded}
        pipeline: list[dict[str, Any]] = [
            {"$match": match},
            {"$group": {"_id": "$skill_name"}},
        ]
        # limit 同样被夹逼到全局上限，防止一次加载过多
        effective_limit = SKILL_EFFECTIVE_LOAD_LIMIT if limit is None else limit
        bounded_limit = max(0, min(int(effective_limit), SKILL_EFFECTIVE_LOAD_LIMIT))
        pipeline.extend([{"$sort": {"_id": 1}}, {"$limit": bounded_limit}])
        return [doc["_id"] async for doc in collection.aggregate(pipeline)]

    async def invalidate_user_cache(self, user_id: str) -> None:
        """失效用户缓存"""
        # 任何写操作后调用，删除该用户的生效技能缓存
        from src.infra.skill.constants import SKILLS_CACHE_KEY_PREFIX

        cache_key = f"{SKILLS_CACHE_KEY_PREFIX}{user_id}"
        try:
            from src.infra.storage.redis import get_redis_client

            redis_client = get_redis_client()
            await redis_client.delete(cache_key)
        except Exception as e:
            logger.warning(f"[Skills Cache] Redis delete failed: {e}")

    async def create_user_skill(
        self,
        skill_name: str,
        files: dict[str, str],
        user_id: str,
        installed_from: InstalledFrom = InstalledFrom.MANUAL,
        enabled: bool = True,
        binary_files: Optional[dict[str, bytes]] = None,
    ) -> None:
        """
        Create a complete user skill: sync files + upload binaries + create __meta__ + invalidate cache.

        This is the single entry point for all skill creation paths:
        - MarketplacePanel direct create (installed_from=MARKETPLACE)
        - SkillsPanel manual create (installed_from=MANUAL)
        - GitHub import (installed_from=MANUAL)
        - ZIP upload (installed_from=MANUAL)

        Args:
            files: 文本文件 {file_path: text_content}
            binary_files: 二进制文件 {file_path: binary_data}，上传到 S3/local
            user_id: 用户 ID
            skill_name: Skill 名称
            installed_from: 安装来源
            enabled: 是否启用

        Note: The `enabled` parameter is kept for API compatibility but the actual
        enabled/disabled state is managed in user.metadata.disabled_skills.
        """
        # 创建技能的统一入口：至少要有一个文件
        if not files and not binary_files:
            raise ValueError("Skill must have at least one file")

        # 1) 同步文本文件
        files = normalize_skill_files(files)
        await self.sync_skill_files(skill_name, files, user_id)

        # 上传二进制文件
        # 2) 逐个上传二进制文件到对象存储（并写入引用）
        if binary_files:
            for file_path, data in binary_files.items():
                await self.set_skill_binary_file(skill_name, file_path, data, user_id)

        # 3) 写入 __meta__ 元信息；4) 失效缓存使新技能立即生效
        await self.set_skill_meta(skill_name, user_id, installed_from=installed_from)
        await self.invalidate_user_cache(user_id)

    async def close(self):
        """关闭连接（仅清理本地引用，不关闭全局 MongoDB 客户端）"""
        # 全局 Mongo 客户端由框架统一管理，这里只释放本实例的引用
        self._client = None
        self._files_collection = None

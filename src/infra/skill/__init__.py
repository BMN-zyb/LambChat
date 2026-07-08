"""
Skills 管理模块 - 简化架构
"""

import json
from typing import Any

from src.infra.async_utils import run_blocking_io
# load_skill_files 供 DeepAgent 运行时加载技能文件；其余为管理/存储/市场门面
from src.infra.skill.loader import load_skill_files
from src.infra.skill.manager import SkillManager
from src.infra.skill.marketplace import MarketplaceStorage
from src.infra.skill.storage import SkillStorage

# 对外暴露技能子系统的四个核心入口
__all__ = [
    "SkillManager",
    "SkillStorage",
    "MarketplaceStorage",
    "load_skill_files",
]

# 旧数据迁移时的分批参数：每批从游标取多少条
LEGACY_SKILL_MIGRATION_BATCH_SIZE = 100
# 累积多少个待更新用户就批量刷一次 disabled_skills，避免内存无限增长
LEGACY_SKILL_MIGRATION_USER_FLUSH_SIZE = 100


def _legacy_cursor(cursor):
    # 若驱动支持，设置游标批大小以控制内存/往返开销；不支持则原样返回
    batch_size = getattr(cursor, "batch_size", None)
    if callable(batch_size):
        return batch_size(LEGACY_SKILL_MIGRATION_BATCH_SIZE)
    return cursor


async def _json_dumps_legacy_meta(data: dict[str, Any]) -> str:
    # JSON 序列化放到线程池，避免阻塞事件循环
    return await run_blocking_io(json.dumps, data)


async def _flush_disabled_skill_updates(
    users_collection,
    pending: dict[str, set[str]],
) -> int:
    # 将累积的“用户 -> 被禁用技能集合”批量写入用户表的 metadata.disabled_skills
    updated = 0
    for user_id, disabled_skills in list(pending.items()):
        disabled_list = sorted(disabled_skills)
        # 先读取用户现有 metadata，保证与已有禁用项合并而非覆盖
        current = await users_collection.find_one({"id": user_id})
        current_meta = (current.get("metadata") or {}) if current else {}
        current_disabled = current_meta.get("disabled_skills", [])

        # 合并（不覆盖已有的）
        merged_disabled = sorted(set(current_disabled) | set(disabled_list))
        await users_collection.update_one(
            {"id": user_id},
            {"$set": {"metadata": {**current_meta, "disabled_skills": merged_disabled}}},
        )
        updated += 1
    # 刷盘后清空，供下一批复用
    pending.clear()
    return updated


async def init_skill_indexes() -> None:
    """初始化索引 + 旧版数据迁移（应用启动时调用一次，幂等）"""
    from src.infra.logging import get_logger

    logger = get_logger(__name__)

    # 分别为技能文件存储与技能市场创建索引
    storage = SkillStorage()
    marketplace = MarketplaceStorage()

    await storage.ensure_indexes()
    await marketplace.ensure_indexes()

    # 建索引用完即关闭各自连接
    await storage.close()
    await marketplace.close()

    # 旧版数据迁移（幂等，旧集合为空时直接跳过）
    # 迁移失败不阻断启动，仅告警
    try:
        await _migrate_legacy_skills(logger)
    except Exception as e:
        logger.warning(f"Legacy skill migration failed (non-fatal): {e}")


async def _migrate_legacy_skills(logger) -> None:
    """将旧版 system_skills / user_skills / user_skill_preferences 迁移到新架构

    新架构不再使用 skill_toggles 集合：
    - enabled/disabled → 用户表 metadata.disabled_skills
    - 元数据（installed_from 等）→ skill_files 中的 __meta__ 文档
    """
    from src.infra.storage.mongodb import get_mongo_client
    from src.kernel.config import settings

    client = get_mongo_client()
    db = client[settings.MONGODB_DB]

    # 旧架构涉及的集合
    old_system = db["system_skills"]
    old_user = db["user_skills"]
    old_files = db["skill_files"]
    old_prefs = db["user_skill_preferences"]
    old_toggles = db["skill_toggles"]  # 旧版 toggle 集合（可能存在）

    # 新架构涉及的集合
    new_marketplace = db["skill_marketplace"]
    new_marketplace_files = db["skill_marketplace_files"]
    new_files = db["skill_files"]
    users_collection = db["users"]

    # 检查是否有旧数据需要迁移
    # 四个旧集合都为空则说明是全新库或已迁移，直接返回（保证幂等）
    old_system_count = await old_system.count_documents({})
    old_prefs_count = await old_prefs.count_documents({})
    old_user_count = await old_user.count_documents({})
    old_toggles_count = await old_toggles.count_documents({})

    if (
        old_system_count == 0
        and old_prefs_count == 0
        and old_user_count == 0
        and old_toggles_count == 0
    ):
        return

    logger.info(
        f"Legacy skill data found: system_skills={old_system_count}, "
        f"user_skill_preferences={old_prefs_count}, user_skills={old_user_count}, "
        f"skill_toggles={old_toggles_count}. Starting migration..."
    )

    # 1. system_skills -> skill_marketplace + skill_marketplace_files
    # 旧“系统技能”即新“技能市场”条目：迁移市场元数据与其文件
    migrated_count = 0
    async for doc in _legacy_cursor(old_system.find({})):
        skill_name = doc["name"]

        # 市场已存在同名技能：仅补齐缺失的文件（$setOnInsert 不覆盖已有内容）
        existing = await new_marketplace.find_one({"skill_name": skill_name})
        if existing:
            file_cursor = _legacy_cursor(
                old_files.find({"skill_name": skill_name, "user_id": "system"})
            )
            async for file_doc in file_cursor:
                await new_marketplace_files.update_one(
                    {"skill_name": skill_name, "file_path": file_doc["file_path"]},
                    {
                        "$setOnInsert": {
                            "content": file_doc["content"],
                            "created_at": file_doc.get("created_at"),
                            "updated_at": file_doc.get("updated_at"),
                        },
                    },
                    upsert=True,
                )
            continue

        # 市场不存在：新建市场条目
        now = doc.get("updated_at") or doc.get("created_at")
        await new_marketplace.insert_one(
            {
                "skill_name": skill_name,
                "description": doc.get("description", ""),
                "tags": doc.get("tags", []),
                "version": doc.get("version", "1.0.0"),
                "created_at": doc.get("created_at"),
                "updated_at": now,
                "created_by": "system",
                "is_active": True,
            }
        )

        # 再迁移该技能的系统文件到市场文件集合
        file_cursor = _legacy_cursor(
            old_files.find({"skill_name": skill_name, "user_id": "system"})
        )
        async for file_doc in file_cursor:
            await new_marketplace_files.update_one(
                {"skill_name": skill_name, "file_path": file_doc["file_path"]},
                {
                    "$set": {
                        "content": file_doc["content"],
                        "updated_at": file_doc.get("updated_at"),
                    },
                    "$setOnInsert": {
                        "created_at": file_doc.get("created_at"),
                    },
                },
                upsert=True,
            )
        migrated_count += 1

    # 2. 迁移 skill_toggles → __meta__ 文档 + metadata.disabled_skills
    # 新架构用每个技能下的 __meta__ 文档保存安装来源等元信息，
    # 用用户表 metadata.disabled_skills 保存禁用状态
    migrated_toggles = 0
    users_to_update: dict[str, set[str]] = {}  # user_id -> set of disabled skill names
    disabled_updated = 0

    async for toggle_doc in _legacy_cursor(old_toggles.find({})):
        user_id = toggle_doc["user_id"]
        skill_name = toggle_doc["skill_name"]
        enabled = toggle_doc.get("enabled", True)
        installed_from = toggle_doc.get("installed_from", "manual")
        published_marketplace_name = toggle_doc.get("published_marketplace_name")

        # 创建 __meta__ 文档（如果不存在）
        # 幂等：已存在则跳过，避免重复迁移覆盖
        existing_meta = await new_files.find_one(
            {"skill_name": skill_name, "user_id": user_id, "file_path": "__meta__"}
        )
        if not existing_meta:
            meta_data = {
                "installed_from": installed_from,
                "published_marketplace_name": published_marketplace_name,
                "created_at": toggle_doc.get("created_at"),
                "updated_at": toggle_doc.get("updated_at"),
            }
            await new_files.insert_one(
                {
                    "skill_name": skill_name,
                    "user_id": user_id,
                    "file_path": "__meta__",
                    "content": await _json_dumps_legacy_meta(meta_data),
                    "created_at": toggle_doc.get("created_at"),
                    "updated_at": toggle_doc.get("updated_at"),
                }
            )
        migrated_toggles += 1

        # 收集 disabled skills
        # 禁用项累积到内存，达到阈值即批量刷盘
        if not enabled:
            if user_id not in users_to_update:
                users_to_update[user_id] = set()
            users_to_update[user_id].add(skill_name)
            if len(users_to_update) >= LEGACY_SKILL_MIGRATION_USER_FLUSH_SIZE:
                disabled_updated += await _flush_disabled_skill_updates(
                    users_collection,
                    users_to_update,
                )

    # 3. user_skill_preferences → __meta__ 文档 + metadata.disabled_skills
    # 旧“用户技能偏好”同样迁为 __meta__ + 禁用集合
    migrated_prefs = 0
    async for doc in _legacy_cursor(old_prefs.find({})):
        user_id = doc["user_id"]
        skill_name = doc["skill_name"]
        enabled = doc.get("enabled", True)

        existing_meta = await new_files.find_one(
            {"skill_name": skill_name, "user_id": user_id, "file_path": "__meta__"}
        )
        if not existing_meta:
            # 若市场中存在该技能，来源判定为 marketplace，否则 manual
            marketplace_doc = await new_marketplace.find_one({"skill_name": skill_name})
            installed_from = "marketplace" if marketplace_doc else "manual"
            meta_data = {
                "installed_from": installed_from,
                "created_at": doc.get("created_at"),
                "updated_at": doc.get("updated_at"),
            }
            await new_files.insert_one(
                {
                    "skill_name": skill_name,
                    "user_id": user_id,
                    "file_path": "__meta__",
                    "content": await _json_dumps_legacy_meta(meta_data),
                    "created_at": doc.get("created_at"),
                    "updated_at": doc.get("updated_at"),
                }
            )
        migrated_prefs += 1

        if not enabled:
            if user_id not in users_to_update:
                users_to_update[user_id] = set()
            users_to_update[user_id].add(skill_name)
            if len(users_to_update) >= LEGACY_SKILL_MIGRATION_USER_FLUSH_SIZE:
                disabled_updated += await _flush_disabled_skill_updates(
                    users_collection,
                    users_to_update,
                )

    # 4. user_skills → __meta__ 文档
    # 旧“用户技能”记录：补齐 __meta__（安装来源固定为 manual）
    async for doc in _legacy_cursor(old_user.find({})):
        user_id = doc["user_id"]
        skill_name = doc["name"]
        enabled = doc.get("enabled", True)

        existing_meta = await new_files.find_one(
            {"skill_name": skill_name, "user_id": user_id, "file_path": "__meta__"}
        )
        if not existing_meta:
            meta_data = {
                "installed_from": "manual",
                "created_at": doc.get("created_at"),
                "updated_at": doc.get("updated_at"),
            }
            await new_files.insert_one(
                {
                    "skill_name": skill_name,
                    "user_id": user_id,
                    "file_path": "__meta__",
                    "content": await _json_dumps_legacy_meta(meta_data),
                    "created_at": doc.get("created_at"),
                    "updated_at": doc.get("updated_at"),
                }
            )

        if not enabled:
            if user_id not in users_to_update:
                users_to_update[user_id] = set()
            users_to_update[user_id].add(skill_name)
            if len(users_to_update) >= LEGACY_SKILL_MIGRATION_USER_FLUSH_SIZE:
                disabled_updated += await _flush_disabled_skill_updates(
                    users_collection,
                    users_to_update,
                )

    # 5. 批量更新用户 metadata.disabled_skills
    # 循环结束后把剩余未刷盘的禁用项一次性写入
    disabled_updated += await _flush_disabled_skill_updates(
        users_collection,
        users_to_update,
    )

    # 6. 为旧版用户复制 marketplace 文件到 skill_files
    # 旧数据可能只有 __meta__ 而无实际技能文件；从市场复制一份到用户空间
    installed_count = 0
    async for meta_doc in _legacy_cursor(new_files.find({"file_path": "__meta__"})):
        user_id = meta_doc["user_id"]
        skill_name = meta_doc["skill_name"]

        # 用户已有该技能的实际文件（非 __meta__）则跳过，避免覆盖用户改动
        user_file_count = await new_files.count_documents(
            {"skill_name": skill_name, "user_id": user_id, "file_path": {"$ne": "__meta__"}}
        )
        if user_file_count > 0:
            continue

        # 从市场文件逐个复制到该用户名下
        files_copied = 0
        mp_file_cursor = _legacy_cursor(new_marketplace_files.find({"skill_name": skill_name}))
        async for mp_file in mp_file_cursor:
            await new_files.update_one(
                {"skill_name": skill_name, "user_id": user_id, "file_path": mp_file["file_path"]},
                {
                    "$set": {
                        "content": mp_file["content"],
                        "updated_at": mp_file.get("updated_at"),
                    },
                    "$setOnInsert": {
                        "created_at": mp_file.get("created_at"),
                    },
                },
                upsert=True,
            )
            files_copied += 1

        if files_copied > 0:
            installed_count += 1

    logger.info(
        f"Legacy skill migration done: "
        f"marketplace={migrated_count}, toggles={migrated_toggles}, "
        f"prefs={migrated_prefs}, disabled_users={disabled_updated}, "
        f"installations={installed_count}"
    )

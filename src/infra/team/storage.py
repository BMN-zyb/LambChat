"""Team storage."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 团队的 MongoDB 持久化层，服务于 TeamManager。核心职责与难点：
#   - 成员为整体替换语义：更新 members 时不做逐字段增量，而是整体规整重写，
#     并重新生成 member_id、重新校正 default_member_id（默认发言成员）是否仍有效。
#   - 写入前规整：成员数、starter_prompts、tags 都会去重/裁剪到各自上限。
#   - 用户偏好：收藏/置顶存于 users.metadata（非单独表），设数量上限，超限静默拒绝；
#     删除团队时会级联从所有用户的偏好列表中清除该 team_id，避免悬空引用。
#   - 列表排序：用聚合管道在数据库端标记 is_pinned/is_favorite 并按
#     置顶 > 收藏 > 更新时间 > 创建时间 排序后再分页；总数与偏好读取用 gather 并发。
#   - 越权保护：get/update/delete 均带上 owner_user_id 作为查询条件，天然隔离他人团队。
# ============================================================================

import asyncio
import re
import uuid
from typing import TYPE_CHECKING, Any, Optional

from bson import ObjectId

from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.persona_preset import PersonaStarterPrompt
from src.kernel.schemas.team import (
    TEAM_MEMBERS_MAX,
    TEAM_STARTER_PROMPTS_MAX,
    TEAM_TAGS_MAX,
    TeamMemberResponse,
    TeamResponse,
    TeamVisibility,
)

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection

# 团队列表分页查询单页最多返回的条数上限
TEAM_LIST_LIMIT_MAX = 200


def _bounded_list_limit(limit: int) -> int:
    # 收敛 limit 到 [1, TEAM_LIST_LIMIT_MAX] 区间，防御非法输入
    return min(max(int(limit), 1), TEAM_LIST_LIMIT_MAX)


class TeamStorage:
    """MongoDB storage for teams."""

    def __init__(self) -> None:
        self._collection: "AsyncIOMotorCollection[Any] | None" = None
        self._user_collection: "AsyncIOMotorCollection[Any] | None" = None

    @property
    def collection(self) -> "AsyncIOMotorCollection[Any]":
        """Lazy MongoDB collection."""
        # 团队主集合：存放团队本身（成员组成、指令、可见性等）
        if self._collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db["teams"]
        return self._collection

    @property
    def user_collection(self) -> "AsyncIOMotorCollection[Any]":
        """Lazy MongoDB users collection for per-user team preferences."""
        # 复用全局 users 集合：用户对团队的"置顶/收藏"偏好存在 users.metadata 里，
        # 而不是单独开一张偏好表，减少一次集合关联
        if self._user_collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._user_collection = db["users"]
        return self._user_collection

    # 单个用户最多可置顶 / 收藏的团队数量，避免偏好列表无限增长
    MAX_PINNED = 10
    MAX_FAVORITES = 100

    @staticmethod
    def _user_query_id(user_id: str) -> ObjectId | str:
        # users 集合的 _id 通常是 ObjectId，但兼容历史/测试数据里可能是字符串的情况：
        # 转换失败时原样返回字符串，让查询自然匹配不到（而不是抛异常中断流程）
        try:
            return ObjectId(user_id)
        except Exception:
            return user_id

    @staticmethod
    def _bounded_unique_ids(values: Any, limit: int) -> list[str]:
        # 将任意输入规整为"去重、去空白、裁剪到 limit 条"的字符串 id 列表，
        # 用于清洗从 Mongo 读出的 pinned_team_ids / favorite_team_ids 原始数据
        result: list[str] = []
        seen: set[str] = set()
        if not isinstance(values, list):
            return result
        for value in values:
            clean = str(value).strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            result.append(clean)
            if len(result) >= limit:
                break
        return result

    # ── Document conversion ──

    @staticmethod
    def _member_doc(member: dict[str, Any]) -> dict[str, Any]:
        """Convert client/member data to a storage member document."""
        # 若未提供 member_id 则生成一个短随机 id（m- 前缀 + 12 位 hex），
        # 保证团队内每个成员都有稳定唯一的标识，供 default_member_id 等引用
        return {
            "member_id": member.get("member_id") or f"m-{uuid.uuid4().hex[:12]}",
            "persona_preset_id": member.get("persona_preset_id", ""),
            "agent_id": member.get("agent_id"),
            "model_id": member.get("model_id"),
            "role_name": member.get("role_name", ""),
            "role_avatar": member.get("role_avatar"),
            "role_tags": member.get("role_tags", []),
            "role_instructions": member.get("role_instructions", ""),
            "position": member.get("position", 0),
            "enabled": member.get("enabled", True),
        }

    @staticmethod
    def _starter_prompt_docs(prompts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Normalize team starter prompts before persistence."""
        # 裁剪到最大条数上限，并用 PersonaStarterPrompt 校验/规整每条 prompt 的结构
        result: list[dict[str, Any]] = []
        for prompt in (prompts or [])[:TEAM_STARTER_PROMPTS_MAX]:
            result.append(PersonaStarterPrompt(**prompt).model_dump(mode="json"))
        return result

    @staticmethod
    def _tags_doc(tags: list[str] | None) -> list[str]:
        """Normalize team tags before persistence."""
        # 去重、去空白，并裁剪到 TEAM_TAGS_MAX，保持写入前的标签列表干净可控
        seen: set[str] = set()
        result: list[str] = []
        for tag in tags or []:
            item = str(tag).strip()
            if not item or item in seen:
                continue
            seen.add(item)
            result.append(item)
            if len(result) >= TEAM_TAGS_MAX:
                break
        return result

    @staticmethod
    def _member_docs(members: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Normalize team members before persistence."""
        # 裁剪到 TEAM_MEMBERS_MAX 条，逐个走 _member_doc 规整
        return [TeamStorage._member_doc(member) for member in (members or [])[:TEAM_MEMBERS_MAX]]

    @staticmethod
    def _resolve_default_member_id(
        members: list[dict[str, Any]],
        requested_default_member_id: str | None,
    ) -> str | None:
        """Keep a valid default member id, defaulting to the first member."""
        # 校验期望的默认成员 id 是否仍然存在于当前成员列表中（成员可能已被移除/替换），
        # 若不存在或未指定，则兜底取第一个成员作为默认发言人
        if not members:
            return None

        member_ids = {member["member_id"] for member in members}
        if requested_default_member_id in member_ids:
            return requested_default_member_id
        return members[0]["member_id"]

    @staticmethod
    def _doc_to_response(doc: dict[str, Any]) -> TeamResponse:
        """Convert a MongoDB document to a TeamResponse."""
        result = dict(doc)
        if "_id" in result:
            result["id"] = str(result.pop("_id"))

        members_raw = result.pop("members", [])
        members = []
        for m in members_raw:
            members.append(
                TeamMemberResponse(
                    member_id=m.get("member_id", ""),
                    persona_preset_id=m.get("persona_preset_id", ""),
                    agent_id=m.get("agent_id"),
                    model_id=m.get("model_id"),
                    role_name=m.get("role_name", ""),
                    role_avatar=m.get("role_avatar"),
                    role_tags=m.get("role_tags", []),
                    role_instructions=m.get("role_instructions", ""),
                    position=m.get("position", 0),
                    enabled=m.get("enabled", True),
                )
            )
        result["members"] = members
        # 对历史数据中可能缺失的字段挨个兜底默认值，保证老团队文档也能正常构造出响应模型
        result.setdefault("description", "")
        result.setdefault("avatar", None)
        result.setdefault("tags", [])
        result.setdefault("default_member_id", None)
        result.setdefault("team_instructions", "")
        result.setdefault("starter_prompts", [])
        result.setdefault("visibility", TeamVisibility.PRIVATE.value)
        result.setdefault("is_favorite", False)
        result.setdefault("is_pinned", False)
        result.setdefault("last_used_at", None)
        return TeamResponse(**result)

    # ── User preference helpers (stored in user metadata) ──

    async def _get_user_team_preference(self, user_id: str) -> dict[str, list[str]]:
        # 只投影出偏好相关的两个字段，避免把整个用户文档都拉回来
        doc = await self.user_collection.find_one(
            {"_id": self._user_query_id(user_id)},
            {"metadata.pinned_team_ids": 1, "metadata.favorite_team_ids": 1},
        )
        metadata = (doc or {}).get("metadata") or {}
        return {
            "pinned": self._bounded_unique_ids(
                metadata.get("pinned_team_ids"),
                self.MAX_PINNED,
            ),
            "favorite": self._bounded_unique_ids(
                metadata.get("favorite_team_ids"),
                self.MAX_FAVORITES,
            ),
        }

    async def _set_user_team_preference(
        self,
        user_id: str,
        pref: dict[str, list[str]],
    ) -> None:
        # 整体覆盖写入 pinned/favorite 两个列表（调用方需先读出旧值再修改再整体写回）
        await self.user_collection.update_one(
            {"_id": self._user_query_id(user_id)},
            {
                "$set": {
                    "metadata.pinned_team_ids": pref["pinned"],
                    "metadata.favorite_team_ids": pref["favorite"],
                    "updated_at": utc_now(),
                }
            },
        )

    async def update_user_preference(
        self,
        *,
        user_id: str,
        team_id: str,
        update: dict[str, Any],
    ) -> dict[str, Any]:
        """Update the current user's favorite/pinned state for a team."""
        # 读-改-写模式：先取出该用户当前的置顶/收藏列表，按需增删 team_id 后整体写回
        pref = await self._get_user_team_preference(user_id)
        pinned: list[str] = list(pref["pinned"])
        favorite: list[str] = list(pref["favorite"])

        if update.get("is_pinned") is not None:
            if update["is_pinned"] and team_id not in pinned:
                if len(pinned) >= self.MAX_PINNED:
                    # 已达置顶数量上限：静默拒绝本次置顶请求，返回当前实际状态
                    # （is_pinned 保持 False）而不是抛异常打断整个请求
                    return {
                        "is_favorite": team_id in favorite,
                        "is_pinned": False,
                        "last_used_at": None,
                    }
                pinned.append(team_id)
            elif not update["is_pinned"] and team_id in pinned:
                pinned.remove(team_id)

        if update.get("is_favorite") is not None:
            if update["is_favorite"] and team_id not in favorite:
                if len(favorite) >= self.MAX_FAVORITES:
                    # 同上，收藏数量达上限时同样静默拒绝，不影响置顶状态的返回
                    return {
                        "is_favorite": False,
                        "is_pinned": team_id in pinned,
                        "last_used_at": None,
                    }
                favorite.append(team_id)
            elif not update["is_favorite"] and team_id in favorite:
                favorite.remove(team_id)

        await self._set_user_team_preference(user_id, {"pinned": pinned, "favorite": favorite})
        return {
            "is_favorite": team_id in favorite,
            "is_pinned": team_id in pinned,
            "last_used_at": None,
        }

    async def _remove_team_from_all_user_preferences(self, team_id: str) -> None:
        """Remove a deleted team id from user preference lists."""
        # 团队被删除后，需要在全体用户的置顶/收藏列表里清除对它的引用，
        # 否则会出现"偏好里存在但团队已不存在"的悬空 id
        await self.user_collection.update_many(
            {},
            {
                "$pull": {
                    "metadata.pinned_team_ids": team_id,
                    "metadata.favorite_team_ids": team_id,
                }
            },
        )

    async def _apply_user_preferences(
        self,
        user_id: str,
        teams: list[TeamResponse],
    ) -> list[TeamResponse]:
        # 把该用户的置顶/收藏偏好回填到一批团队响应对象上（用于非聚合查询路径的场景）
        if not teams:
            return teams
        pref = await self._get_user_team_preference(user_id)
        pinned_set = set(pref["pinned"])
        favorite_set = set(pref["favorite"])
        return [
            team.model_copy(
                update={
                    "is_pinned": team.id in pinned_set,
                    "is_favorite": team.id in favorite_set,
                    "last_used_at": None,
                }
            )
            for team in teams
        ]

    @staticmethod
    def _preference_sort_key(team: TeamResponse) -> tuple:
        # 排序优先级：置顶 > 收藏 > 更新时间新 > 创建时间新（数值越小越靠前）
        updated = team.updated_at
        created = team.created_at
        return (
            0 if team.is_pinned else 1,
            0 if team.is_favorite else 1,
            -(updated.timestamp() if updated else 0),
            -(created.timestamp() if created else 0),
        )

    # ── CRUD ──

    async def create_team(
        self,
        *,
        owner_user_id: str,
        name: str,
        description: str = "",
        avatar: str | None = None,
        tags: list[str] | None = None,
        members: list[dict[str, Any]] | None = None,
        default_member_id: str | None = None,
        team_instructions: str = "",
        starter_prompts: list[dict[str, Any]] | None = None,
    ) -> TeamResponse:
        """Create a new team."""
        # 新团队默认可见性为 PRIVATE（仅创建者可见），置顶/收藏等偏好属于用户维度，
        # 不在团队文档里存储，因此创建时不涉及 user_collection
        now = utc_now()
        members_docs = self._member_docs(members)
        doc: dict[str, Any] = {
            "owner_user_id": owner_user_id,
            "name": name,
            "description": description,
            "avatar": avatar,
            "tags": self._tags_doc(tags),
            "members": members_docs,
            "default_member_id": self._resolve_default_member_id(
                members_docs,
                default_member_id,
            ),
            "team_instructions": team_instructions,
            "starter_prompts": self._starter_prompt_docs(starter_prompts),
            "visibility": TeamVisibility.PRIVATE.value,
            "created_at": now,
            "updated_at": now,
        }
        insert_result = await self.collection.insert_one(doc)
        doc["_id"] = insert_result.inserted_id
        return self._doc_to_response(doc)

    async def get_team(
        self,
        team_id: str,
        *,
        owner_user_id: str,
    ) -> Optional[TeamResponse]:
        """Get a team by ID, scoped to owner."""
        # team_id 格式非法（无法转换为 ObjectId）时直接返回 None，不抛异常
        try:
            query_id = ObjectId(team_id)
        except Exception:
            return None
        # 查询条件带上 owner_user_id，天然做到"用户只能查到自己拥有的团队"的越权保护
        doc = await self.collection.find_one({"_id": query_id, "owner_user_id": owner_user_id})
        if not doc:
            return None
        return self._doc_to_response(doc)

    async def list_teams(
        self,
        *,
        owner_user_id: str,
        skip: int = 0,
        limit: int = 100,
        favorite: bool | None = None,
        pinned: bool | None = None,
        q: str | None = None,
        tag: str | None = None,
    ) -> tuple[list[TeamResponse], int]:
        """List teams for an owner, paginated. Returns (teams, total)."""
        limit = _bounded_list_limit(limit)
        query: dict[str, Any] = {"owner_user_id": owner_user_id}
        if favorite is True or pinned is True:
            # 若请求要求"只看收藏"或"只看置顶"，先取出该用户的偏好 id 集合，
            # 再把查询范围收窄到这些 id；两者任一为真就取并集（该团队被置顶或收藏均命中）
            pref = await self._get_user_team_preference(owner_user_id)
            target_ids: set[str] = set()
            if pinned:
                target_ids.update(pref["pinned"])
            if favorite:
                target_ids.update(pref["favorite"])
            if not target_ids:
                return [], 0
            try:
                query["_id"] = {"$in": [ObjectId(team_id) for team_id in target_ids]}
            except Exception:
                return [], 0

        if q:
            # 关键字搜索命中名称/描述/标签/成员角色名/成员角色标签，且忽略大小写
            needle = q.strip().lower()
            if needle:
                pattern = re.escape(needle)
                query["$or"] = [
                    {"name": {"$regex": pattern, "$options": "i"}},
                    {"description": {"$regex": pattern, "$options": "i"}},
                    {"tags": {"$elemMatch": {"$regex": pattern, "$options": "i"}}},
                    {"members.role_name": {"$regex": pattern, "$options": "i"}},
                    {"members.role_tags": {"$elemMatch": {"$regex": pattern, "$options": "i"}}},
                ]
        if tag:
            tag_filter = tag.strip()
            if tag_filter:
                query["tags"] = tag_filter

        # 总数统计与用户偏好读取互不依赖，用 gather 并发执行以减少一次往返等待
        total, pref = await asyncio.gather(
            self.collection.count_documents(query),
            self._get_user_team_preference(owner_user_id),
        )
        if total == 0:
            return [], 0

        pinned_ids = pref["pinned"]
        favorite_ids = pref["favorite"]
        # 用聚合管道在数据库层面直接算出每条团队是否置顶/收藏并参与排序，
        # 避免先取回全部数据再在应用层排序（尤其是分页场景下排序必须先于 skip/limit）
        pipeline: list[dict[str, Any]] = [
            {"$match": query},
            {
                "$addFields": {
                    "is_pinned": {"$in": [{"$toString": "$_id"}, pinned_ids]},
                    "is_favorite": {"$in": [{"$toString": "$_id"}, favorite_ids]},
                }
            },
            {
                "$sort": {
                    "is_pinned": -1,
                    "is_favorite": -1,
                    "updated_at": -1,
                    "created_at": -1,
                }
            },
            {"$skip": skip},
            {"$limit": limit},
        ]
        docs = [doc async for doc in self.collection.aggregate(pipeline)]
        teams = [self._doc_to_response(doc) for doc in docs]
        return teams, total

    async def update_team(
        self,
        team_id: str,
        *,
        owner_user_id: str,
        update: dict[str, Any],
    ) -> Optional[TeamResponse]:
        """Update a team by ID, scoped to owner."""
        try:
            query_id = ObjectId(team_id)
        except Exception:
            return None

        # 过滤掉值为 None 的字段，表示"调用方未提供该字段"，避免误将其清空
        update = {k: v for k, v in update.items() if v is not None}
        update["updated_at"] = utc_now()

        # Full replacement semantics for members: regenerate member_ids
        # 成员列表是整体替换语义（不支持局部增删单个成员字段），
        # 所以每次更新成员都要重新走一遍规整流程，并重新校验默认成员 id 是否仍然有效
        if "members" in update:
            new_members = self._member_docs(update["members"])
            update["members"] = new_members
            update["default_member_id"] = self._resolve_default_member_id(
                new_members,
                update.get("default_member_id"),
            )
        if "starter_prompts" in update:
            update["starter_prompts"] = self._starter_prompt_docs(
                update["starter_prompts"],
            )
        if "tags" in update:
            update["tags"] = self._tags_doc(update["tags"])

        if not update:
            # 只剩 updated_at 一个字段，说明本次调用没有实际要更新的内容，
            # 直接返回当前团队数据即可，不必发起一次空更新
            return await self.get_team(team_id, owner_user_id=owner_user_id)

        doc = await self.collection.find_one_and_update(
            {"_id": query_id, "owner_user_id": owner_user_id},
            {"$set": update},
            return_document=True,
        )
        if not doc:
            return None
        return self._doc_to_response(doc)

    async def delete_team(
        self,
        team_id: str,
        *,
        owner_user_id: str,
    ) -> bool:
        """Delete a team by ID, scoped to owner."""
        try:
            query_id = ObjectId(team_id)
        except Exception:
            return False
        result = await self.collection.delete_one({"_id": query_id, "owner_user_id": owner_user_id})
        if result.deleted_count > 0:
            # 团队确实被删除后，联动清理全体用户偏好列表里对该团队的引用
            await self._remove_team_from_all_user_preferences(team_id)
        return result.deleted_count > 0

    async def clone_team(
        self,
        team_id: str,
        *,
        owner_user_id: str,
        new_name: str | None = None,
    ) -> Optional[TeamResponse]:
        """Clone a team for the same owner."""
        original = await self.get_team(team_id, owner_user_id=owner_user_id)
        if not original:
            return None

        # Serialize members back to dicts with new member_ids
        # 克隆时不带 member_id，交给 create_team -> _member_docs 重新生成，
        # 避免克隆出的新团队与原团队共享同一批 member_id
        members_data = []
        for m in original.members:
            members_data.append(
                {
                    "persona_preset_id": m.persona_preset_id,
                    "agent_id": m.agent_id,
                    "model_id": m.model_id,
                    "role_name": m.role_name,
                    "role_avatar": m.role_avatar,
                    "role_tags": m.role_tags,
                    "role_instructions": m.role_instructions,
                    "position": m.position,
                    "enabled": m.enabled,
                }
            )

        return await self.create_team(
            owner_user_id=owner_user_id,
            name=new_name or f"{original.name} (copy)",
            description=original.description,
            avatar=original.avatar,
            tags=original.tags,
            members=members_data,
            default_member_id=None,
            team_instructions=original.team_instructions,
            starter_prompts=[prompt.model_dump(mode="json") for prompt in original.starter_prompts],
        )

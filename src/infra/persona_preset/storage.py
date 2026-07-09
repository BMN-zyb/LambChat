"""Persona preset storage."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 人设预设的 MongoDB 持久化层，服务于 PersonaPresetManager。核心职责与难点：
#   - 可见性查询：_build_visible_query 统一构造"该用户能看到哪些预设"的条件，
#     list_visible / count_visible 共用它，保证"列表"与"总数"口径一致。
#   - 搜索分词：_build_persona_search_terms 对中英文分别处理，中文额外做二字
#     滑窗子串但不对单字做过宽匹配，避免误召回；多分词之间是"与"关系。
#   - 用户偏好：收藏/置顶不单独建表，而是存在 users.metadata 里，并对数量设上限
#     （MAX_PINNED/MAX_FAVORITES），超限时静默拒绝并返回当前真实状态而非报错。
#   - 排序：用聚合管道在数据库端为每条预设打上 is_pinned/is_favorite 标记，
#     再按 置顶 > 收藏 > 更新时间 > 创建时间 > 使用次数 排序后分页。
#   - 历史数据兼容：_to_model_dict 会为旧文档缺失的字段补默认值。
# ============================================================================

import re
from typing import Any, Optional

from bson import ObjectId

from src.infra.utils.datetime import utc_now
from src.kernel.config import settings

# 分词正则：连续的英文字母数字下划线算作一个词元，连续的中文汉字（含扩展A区、兼容区）也算作一个词元。
_SEARCH_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+")
# 用于判断字符串里是否包含“字面字符”（字母数字或中文汉字），排除掉纯符号的输入。
_SEARCH_HAS_LITERAL_RE = re.compile(r"[A-Za-z0-9_\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
# 预设列表单页最多返回的条数上限，防止外部传入过大的 limit 拖垮查询。
PERSONA_PRESET_LIST_LIMIT = 200


def _build_persona_search_terms(text: str | None) -> list[str]:
    """Build role-search terms without broad single-character CJK matches."""
    if not text:
        return []

    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        # 统一转小写并去重后才收录，避免生成重复的正则匹配条件。
        clean = term.strip().lower()
        if not clean or clean in seen:
            return
        seen.add(clean)
        terms.append(re.escape(clean))

    stripped = text.strip()
    # 若整段输入同时包含字面字符与非单词符号（例如带标点的短语），把整段原文也当作一个搜索词，
    # 以支持带符号的精确短语匹配。
    if _SEARCH_HAS_LITERAL_RE.search(stripped) and re.search(r"[^\w\s]", stripped):
        add(stripped)

    for match in _SEARCH_TOKEN_RE.finditer(text):
        token = match.group(0)
        if token.isascii():
            add(token)
            continue

        # 中文词元：整词与二字滑窗子串都加入候选，既能匹配子串搜索场景，
        # 又通过限定滑窗长度 > 2 才拆分，避免对单字 CJK 做过宽泛匹配造成误召回。
        add(token)
        if len(token) > 2:
            for index in range(len(token) - 1):
                add(token[index : index + 2])

    # 限制搜索词数量上限，避免生成的 Mongo 查询过于复杂拖慢性能。
    return terms[:32]


class PersonaPresetStorage:
    """MongoDB storage for persona presets."""

    def __init__(self):
        # 预设集合与用户集合都延迟初始化，首次访问对应属性时才建立连接。
        self._collection = None
        self._user_collection = None

    @property
    def collection(self):
        """Lazy MongoDB collection."""
        if self._collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db["persona_presets"]
        return self._collection

    @property
    def user_collection(self):
        # 用户的收藏/置顶偏好保存在 users 集合的 metadata 字段中，因此需要单独持有用户集合引用。
        if self._user_collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._user_collection = db["users"]
        return self._user_collection

    # 历史文档可能缺失部分字段（后续新增字段时旧数据没有该键），这里定义补全用的默认值。
    _REQUIRED_DEFAULTS: dict[str, Any] = {
        "name": "Untitled",
        "description": "",
        "tags": [],
        "system_prompt": "You are a helpful assistant.",
        "starter_prompts": [],
        "skill_names": [],
        "visibility": "private",
        "status": "draft",
    }

    @classmethod
    def _to_model_dict(cls, doc: dict[str, Any]) -> dict[str, Any]:
        # 把 Mongo 文档整理成可直接传给 Pydantic 模型的字典：_id 转为字符串 id，缺失字段补默认值。
        result = dict(doc)
        if "_id" in result:
            result["id"] = str(result.pop("_id"))
        for key, default in cls._REQUIRED_DEFAULTS.items():
            if result.get(key) is None:
                result[key] = default
        return result

    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        # 插入新文档；created_at/updated_at 若调用方已显式传入则沿用，否则用当前时间填充。
        now = utc_now()
        doc = {
            **data,
            "created_at": data.get("created_at") or now,
            "updated_at": data.get("updated_at") or now,
        }
        result = await self.collection.insert_one(doc)
        doc["id"] = str(result.inserted_id)
        return doc

    async def insert_many(self, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # 批量插入：先统一补齐时间戳默认值，再一次性写入以减少往返次数。
        now = utc_now()
        for doc in docs:
            doc.setdefault("created_at", now)
            doc.setdefault("updated_at", now)
        result = await self.collection.insert_many(docs)
        for doc, inserted_id in zip(docs, result.inserted_ids):
            doc["id"] = str(inserted_id)
        return docs

    async def get_by_id(self, preset_id: str) -> Optional[dict[str, Any]]:
        # preset_id 不是合法 ObjectId 格式时直接返回 None，而不是让异常向上抛出。
        try:
            query_id = ObjectId(preset_id)
        except Exception:
            return None
        doc = await self.collection.find_one({"_id": query_id})
        return self._to_model_dict(doc) if doc else None

    # ── User preference helpers (stored in user metadata) ──

    # 每个用户最多可置顶/收藏的预设数量上限，避免列表无限增长拖慢用户文档的读写。
    MAX_PINNED = 10
    MAX_FAVORITES = 100

    @staticmethod
    def _bounded_unique_ids(values: Any, limit: int) -> list[str]:
        # 从任意输入中提炼出去重、非空、且不超过 limit 条数的 ID 列表；非 list 类型直接视为空列表。
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

    async def _get_user_preset_preference(self, user_id: str) -> dict[str, list[str]]:
        # 只投影出需要的两个 metadata 字段，减少查询数据量。
        doc = await self.user_collection.find_one(
            {"_id": ObjectId(user_id)},
            {"metadata.pinned_preset_ids": 1, "metadata.favorite_preset_ids": 1},
        )
        metadata = (doc or {}).get("metadata") or {}
        return {
            "pinned": self._bounded_unique_ids(
                metadata.get("pinned_preset_ids"),
                self.MAX_PINNED,
            ),
            "favorite": self._bounded_unique_ids(
                metadata.get("favorite_preset_ids"),
                self.MAX_FAVORITES,
            ),
        }

    async def _set_user_preset_preference(self, user_id: str, pref: dict[str, list[str]]) -> None:
        # 直接整体覆盖写回 pinned/favorite 两个数组；实现简单，但依赖调用方自行保证并发安全。
        await self.user_collection.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$set": {
                    "metadata.pinned_preset_ids": pref["pinned"],
                    "metadata.favorite_preset_ids": pref["favorite"],
                    "updated_at": utc_now(),
                }
            },
        )

    async def update_user_preference(
        self,
        *,
        user_id: str,
        preset_id: str,
        update: dict[str, Any],
    ) -> dict[str, Any]:
        # 读取现有偏好列表，按需增删 preset_id；一旦超出数量上限就放弃本次操作，
        # 并原样返回当前实际状态（而不是报错），让上层据此提示用户。
        pref = await self._get_user_preset_preference(user_id)
        pinned: list[str] = list(pref["pinned"])
        favorite: list[str] = list(pref["favorite"])

        if update.get("is_pinned") is not None:
            if update["is_pinned"] and preset_id not in pinned:
                if len(pinned) >= self.MAX_PINNED:
                    return {
                        "is_favorite": preset_id in favorite,
                        "is_pinned": False,
                        "last_used_at": None,
                    }
                pinned.append(preset_id)
            elif not update["is_pinned"] and preset_id in pinned:
                pinned.remove(preset_id)

        if update.get("is_favorite") is not None:
            if update["is_favorite"] and preset_id not in favorite:
                if len(favorite) >= self.MAX_FAVORITES:
                    return {
                        "is_favorite": False,
                        "is_pinned": preset_id in pinned,
                        "last_used_at": None,
                    }
                favorite.append(preset_id)
            elif not update["is_favorite"] and preset_id in favorite:
                favorite.remove(preset_id)

        await self._set_user_preset_preference(user_id, {"pinned": pinned, "favorite": favorite})
        return {
            "is_favorite": preset_id in favorite,
            "is_pinned": preset_id in pinned,
            "last_used_at": None,
        }

    async def touch_user_preference(self, **_: Any) -> dict[str, Any]:
        # 当前未记录“最近使用时间”，仅作为占位实现，保持返回结构与 update_user_preference 一致。
        return {"is_favorite": False, "is_pinned": False, "last_used_at": None}

    # ── List / Count ──

    async def list_visible(
        self,
        *,
        user_id: str,
        include_admin: bool = False,
        scope: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        q: str | None = None,
        favorite: bool | None = None,
        pinned: bool | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        # skip/limit 做防御性收敛：skip 不允许为负数，limit 被限制在 [1, PERSONA_PRESET_LIST_LIMIT] 之间。
        skip = max(int(skip or 0), 0)
        limit = min(max(int(limit or 1), 1), PERSONA_PRESET_LIST_LIMIT)
        query = self._build_visible_query(
            user_id=user_id,
            include_admin=include_admin,
            scope=scope,
            status=status,
            tag=tag,
            q=q,
        )

        pref: dict[str, list[str]] | None = None
        if favorite is not None or pinned is not None:
            # 若指定了 favorite/pinned 过滤，先取出用户偏好里的目标 ID 集合，
            # 再把查询收窄为“_id 属于该集合”；集合为空时直接短路返回空列表。
            pref = await self._get_user_preset_preference(user_id)
            target_ids: set[str] = set()
            if pinned:
                target_ids.update(pref["pinned"])
            if favorite:
                target_ids.update(pref["favorite"])
            if not target_ids:
                return []
            try:
                object_ids = [ObjectId(pid) for pid in target_ids]
            except Exception:
                return []
            query["_id"] = {"$in": object_ids}

        if pref is None:
            pref = await self._get_user_preset_preference(user_id)
        pinned_ids = pref["pinned"]
        favorite_ids = pref["favorite"]
        # 用聚合管道在数据库端一次性完成：匹配可见性条件 -> 附加 is_pinned/is_favorite 标记 ->
        # 按“置顶优先、收藏优先、更新时间新优先、创建时间新优先、使用次数多优先”排序 -> 分页。
        pipeline: list[dict[str, Any]] = [
            {"$match": query},
            {
                "$addFields": {
                    "is_pinned": {"$in": [{"$toString": "$_id"}, pinned_ids]},
                    "is_favorite": {"$in": [{"$toString": "$_id"}, favorite_ids]},
                    "last_used_at": None,
                }
            },
            {
                "$sort": {
                    "is_pinned": -1,
                    "is_favorite": -1,
                    "updated_at": -1,
                    "created_at": -1,
                    "usage_count": -1,
                }
            },
            {"$skip": skip},
            {"$limit": limit},
        ]
        return [self._to_model_dict(doc) async for doc in self.collection.aggregate(pipeline)]

    async def count_visible(
        self,
        *,
        user_id: str,
        include_admin: bool = False,
        scope: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        q: str | None = None,
        favorite: bool | None = None,
        pinned: bool | None = None,
    ) -> int:
        # 与 list_visible 共用同一套可见性查询条件构造逻辑，确保“总数”与“列表”结果集一致。
        query = self._build_visible_query(
            user_id=user_id,
            include_admin=include_admin,
            scope=scope,
            status=status,
            tag=tag,
            q=q,
        )

        if favorite is not None or pinned is not None:
            pref = await self._get_user_preset_preference(user_id)
            target_ids: set[str] = set()
            if pinned:
                target_ids.update(pref["pinned"])
            if favorite:
                target_ids.update(pref["favorite"])
            if not target_ids:
                return 0
            try:
                object_ids = [ObjectId(pid) for pid in target_ids]
            except Exception:
                return 0
            query["_id"] = {"$in": object_ids}

        return await self.collection.count_documents(query)

    async def update(self, preset_id: str, update: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            query_id = ObjectId(preset_id)
        except Exception:
            return None
        # 过滤掉值为 None 的字段，避免把“调用方未传该字段”误当成“显式置空”写入数据库。
        update = {k: v for k, v in update.items() if v is not None}
        update["updated_at"] = utc_now()
        if not update:
            return await self.get_by_id(preset_id)
        doc = await self.collection.find_one_and_update(
            {"_id": query_id},
            {"$set": update},
            return_document=True,
        )
        return self._to_model_dict(doc) if doc else None

    async def delete(self, preset_id: str) -> bool:
        # preset_id 非法（无法转为 ObjectId）时直接返回 False，不向上抛异常
        try:
            query_id = ObjectId(preset_id)
        except Exception:
            return False
        result = await self.collection.delete_one({"_id": query_id})
        return result.deleted_count > 0

    async def increment_usage(self, preset_id: str) -> None:
        # 使用次数自增，用于列表排序权重；ID 非法时静默忽略，不影响调用方主流程。
        try:
            query_id = ObjectId(preset_id)
        except Exception:
            return
        await self.collection.update_one({"_id": query_id}, {"$inc": {"usage_count": 1}})

    # ── Internal helpers ──

    async def _apply_user_preferences(
        self,
        user_id: str,
        docs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        # 备用的“在内存中合并用户偏好”辅助方法（当前主查询路径已改用聚合管道在数据库端完成，
        # 此方法保留供其他调用场景按需复用）：为每个文档补上 is_pinned/is_favorite 标记。
        if not docs:
            return docs
        pref = await self._get_user_preset_preference(user_id)
        pinned_set = set(pref["pinned"])
        favorite_set = set(pref["favorite"])
        for doc in docs:
            doc["is_pinned"] = doc["id"] in pinned_set
            doc["is_favorite"] = doc["id"] in favorite_set
            doc["last_used_at"] = None
        return docs

    @staticmethod
    def _preference_sort_key(doc: dict[str, Any]) -> tuple:
        # 与聚合管道里的 $sort 语义保持一致的 Python 端排序 key（供内存排序场景复用）：
        # 置顶优先 > 收藏优先 > 更新时间新优先 > 创建时间新优先 > 使用次数多优先。
        updated = doc.get("updated_at")
        created = doc.get("created_at")
        return (
            0 if doc.get("is_pinned") else 1,
            0 if doc.get("is_favorite") else 1,
            -(updated.timestamp() if updated else 0),
            -(created.timestamp() if created else 0),
            -int(doc.get("usage_count", 0) or 0),
        )

    @staticmethod
    def _build_visible_query(
        *,
        user_id: str,
        include_admin: bool = False,
        scope: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        # 构造“该用户能看到哪些预设”的核心查询条件：
        # - 自己创建的 USER 范围预设始终可见；
        # - 管理员额外能看到全部 GLOBAL 范围预设（不论发布状态/可见性）；
        # - 普通用户只能看到已公开且已发布的 GLOBAL 范围预设。
        query: dict[str, Any] = {}
        if include_admin:
            query["$or"] = [
                {"scope": "user", "owner_user_id": user_id},
                {"scope": "global"},
            ]
        else:
            query["$or"] = [
                {"scope": "user", "owner_user_id": user_id},
                {
                    "scope": "global",
                    "visibility": "public",
                    "status": "published",
                },
            ]
        if scope:
            query["scope"] = scope
        if status:
            query["status"] = status
        if tag:
            query["tags"] = tag
        if q:
            # 关键字搜索：对 name/description/tags/skill_names 做不区分大小写的正则匹配；
            # 多个分词之间是“与”关系（$and 数组中每个分词各自要求命中任一字段），以提升长句搜索的精确度。
            query["$and"] = query.get("$and", [])
            search_terms = _build_persona_search_terms(q)
            if not search_terms:
                query["$and"].append({"_id": {"$in": []}})
            else:
                query["$and"].append(
                    {
                        "$or": [
                            {
                                "$or": [
                                    {"name": {"$regex": term, "$options": "i"}},
                                    {"description": {"$regex": term, "$options": "i"}},
                                    {"tags": {"$elemMatch": {"$regex": term, "$options": "i"}}},
                                    {
                                        "skill_names": {
                                            "$elemMatch": {"$regex": term, "$options": "i"}
                                        }
                                    },
                                ]
                            }
                            for term in search_terms
                        ]
                    }
                )
        return query

    async def close(self) -> None:
        # 释放集合引用，交由 GC 回收（Mongo 客户端本身的连接池由全局单例统一管理，此处不关闭连接）。
        self._collection = None
        self._user_collection = None

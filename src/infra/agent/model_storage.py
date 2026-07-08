"""
Model 配置存储层

提供 Model 配置的数据库操作：
- 模型的 CRUD
- 存储在 MongoDB
"""

from typing import Any, Optional

from pymongo import UpdateOne

from src.infra.async_utils import run_blocking_io
from src.infra.mcp.encryption import decrypt_value, encrypt_value
from src.infra.utils.datetime import utc_now, utc_now_iso
from src.kernel.config import settings
from src.kernel.schemas.model import ModelConfig

# MongoDB 集合名称
_COLL_MODELS = "model_configs"
# 明文 key 迁移时的批量大小；bulk_write 的批大小；受限查询/普通查询的数量上限
_PLAINTEXT_KEY_MIGRATION_BATCH_SIZE = 100
MODEL_BULK_WRITE_BATCH_SIZE = 100
MODEL_RESTRICTED_LIST_LIMIT = 100
MODEL_LIST_LIMIT = 500


async def _bulk_write_in_batches(collection: Any, operations: list[Any]) -> None:
    # 把批量写操作切成 MODEL_BULK_WRITE_BATCH_SIZE 大小分批提交，避免单次请求过大
    for start in range(0, len(operations), MODEL_BULK_WRITE_BATCH_SIZE):
        batch = operations[start : start + MODEL_BULK_WRITE_BATCH_SIZE]
        if batch:
            await collection.bulk_write(batch)


class ModelStorage:
    """
    Model 配置存储类

    使用 MongoDB 存储配置数据：
    - 模型配置 (collection: model_configs)
    """

    def __init__(self):
        # 集合句柄缓存，首次使用时才建立连接
        self._collection: Optional[Any] = None

    def _get_collection(self):
        """延迟加载 MongoDB 集合"""
        # 惰性连接，避免模块导入即依赖数据库
        if self._collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db[_COLL_MODELS]
        return self._collection

    async def ensure_indexes(self):
        """创建必要的 MongoDB 索引"""
        # id 唯一索引
        await self._get_collection().create_index("id", unique=True)
        # value 不再唯一：同一模型可来自不同渠道（如直连 OpenAI、Azure、代理服务等）
        # 迁移：删除旧的唯一索引（如存在），然后创建非唯一索引
        existing = await self._get_collection().index_information()
        # 旧的 value 唯一索引存在则先删除
        if "value_1" in existing and existing["value_1"].get("unique"):
            await self._get_collection().drop_index("value_1")
        # 缺失或仍为唯一时，建立非唯一的 value 索引
        if "value_1" not in existing or existing["value_1"].get("unique"):
            await self._get_collection().create_index("value")
        # 真正的唯一约束改为 (value, provider, api_base) 复合键：同名模型不同渠道可共存
        await self._get_collection().create_index(
            [("value", 1), ("provider", 1), ("api_base", 1)],
            unique=True,
            name="value_provider_api_base_unique",
        )
        # 常用过滤/排序字段索引
        await self._get_collection().create_index("enabled")
        await self._get_collection().create_index("order")

    # ── 加密辅助 ──────────────────────────────────────────────────

    @staticmethod
    async def _encrypt_api_key(key: str | None) -> dict | None:
        """加密 API Key（包装为 dict 后加密）"""
        # None 不加密；加密属 CPU 密集操作，放线程池执行
        if key is None:
            return None
        # 包装成 {"v": key} 再加密，便于解密后区分结构化值与历史明文
        return await run_blocking_io(encrypt_value, {"v": key})

    @staticmethod
    async def _decrypt_api_key(encrypted: Any) -> str | None:
        """解密 API Key"""
        if encrypted is None:
            return None
        result = await run_blocking_io(decrypt_value, encrypted)
        # 正常解密结果是 {"v": key}，取回原始 key
        if isinstance(result, dict):
            return result.get("v")
        # 向后兼容：如果值是明文字符串，直接返回
        return str(result) if result else None

    @staticmethod
    def _is_encrypted(value: Any) -> bool:
        """检查值是否已加密"""
        # 加密值统一带 __encrypted__ 标记，用于区分明文 key（迁移判定用）
        return isinstance(value, dict) and "__encrypted__" in value

    async def _decrypt_doc(self, doc: dict) -> dict:
        """解密文档中的 api_key（纯读取，无副作用）。"""
        # 就地把 doc 里的密文 api_key 替换为明文，供构造 ModelConfig 返回
        if doc.get("api_key") is not None:
            doc["api_key"] = await self._decrypt_api_key(doc["api_key"])
        return doc

    # ── 批量迁移 ──────────────────────────────────────────────────

    async def migrate_plaintext_keys(self) -> int:
        """一次性批量加密所有明文 API Key。

        Returns:
            加密的文档数量
        """
        # 查找所有 api_key 不为空且未加密的文档
        cursor = self._get_collection().find({"api_key": {"$ne": None}})
        operations = []
        modified_count = 0
        async for doc in cursor:
            key = doc.get("api_key")
            # 仅对尚未加密的明文 key 生成加密更新操作
            if key and not self._is_encrypted(key):
                operations.append(
                    UpdateOne(
                        {"id": doc["id"]},
                        {
                            "$set": {
                                "api_key": await self._encrypt_api_key(str(key)),
                                "updated_at": utc_now_iso(),
                            }
                        },
                    )
                )
                # 攒满一批就提交一次，累加受影响数并清空缓冲
                if len(operations) >= _PLAINTEXT_KEY_MIGRATION_BATCH_SIZE:
                    result = await self._get_collection().bulk_write(operations)
                    modified_count += result.modified_count
                    operations.clear()

        # 提交最后不足一批的剩余操作
        if operations:
            result = await self._get_collection().bulk_write(operations)
            modified_count += result.modified_count

        # 无迁移则静默返回
        if modified_count == 0:
            return 0

        # 有迁移时记录日志（延迟导入 logger，避免顶层依赖）
        from src.infra.logging import get_logger

        get_logger(__name__).info(
            f"[ModelStorage] Migrated {modified_count} plaintext API keys to encrypted"
        )
        return modified_count

    # ============================================
    # CRUD Operations
    # ============================================

    async def list_models(self, include_disabled: bool = False) -> list[ModelConfig]:
        """获取所有模型配置

        Args:
            include_disabled: 是否包含已禁用的模型

        Returns:
            模型配置列表，按 order 排序
        """
        # 默认只列启用的；按 order 升序、带上限拉取
        query = {} if include_disabled else {"enabled": True}
        cursor = self._get_collection().find(query).sort("order", 1).limit(MODEL_LIST_LIMIT)
        models = []
        async for doc in cursor:
            # 去掉 Mongo 内部 _id，并解密 api_key 后再构造模型对象
            doc.pop("_id", None)
            doc = await self._decrypt_doc(doc)
            models.append(ModelConfig(**doc))
        return models

    async def list_enabled_by_ids_or_values(
        self,
        model_ids_or_values: list[str],
    ) -> list[ModelConfig]:
        """List enabled models matching a bounded set of model IDs or values."""
        # 供角色模型访问控制使用：按给定的 id/value 集合过滤启用模型
        if not model_ids_or_values:
            return []

        # 去重并截断到上限，避免超大 $in 查询
        bounded_values = []
        seen_values = set()
        for value in model_ids_or_values:
            if value in seen_values:
                continue
            seen_values.add(value)
            bounded_values.append(value)
            if len(bounded_values) >= MODEL_RESTRICTED_LIST_LIMIT:
                break

        # id 或 value 命中任一即可（前端可能传 id，也可能传 value）
        cursor = (
            self._get_collection()
            .find(
                {
                    "enabled": True,
                    "$or": [
                        {"id": {"$in": bounded_values}},
                        {"value": {"$in": bounded_values}},
                    ],
                }
            )
            .sort("order", 1)
            .limit(MODEL_RESTRICTED_LIST_LIMIT)
        )
        models = []
        async for doc in cursor:
            doc.pop("_id", None)
            doc = await self._decrypt_doc(doc)
            models.append(ModelConfig(**doc))
        return models

    async def get(self, model_id: str) -> Optional[ModelConfig]:
        """根据 ID 获取模型配置

        Args:
            model_id: 模型 ID

        Returns:
            模型配置，不存在返回 None
        """
        doc = await self._get_collection().find_one({"id": model_id})
        if not doc:
            return None
        # 去内部 _id + 解密后返回
        doc.pop("_id", None)
        doc = await self._decrypt_doc(doc)
        return ModelConfig(**doc)

    async def get_by_value(self, value: str) -> Optional[ModelConfig]:
        """根据 value (model identifier) 获取模型配置

        优先返回 enabled 的模型。当同一 value 存在多条记录时
        （不同渠道/提供商），选择第一个启用的。

        Args:
            value: 模型标识符

        Returns:
            模型配置，不存在返回 None
        """
        # 优先查找 enabled 的
        # 同一 value 可能有多条（多渠道），按 order 取第一条启用的
        doc = await self._get_collection().find_one(
            {"value": value, "enabled": True},
            sort=[("order", 1)],
        )
        if not doc:
            return None
        doc.pop("_id", None)
        doc = await self._decrypt_doc(doc)
        return ModelConfig(**doc)

    async def create(self, model: ModelConfig) -> ModelConfig:
        """创建模型配置

        Args:
            model: 模型配置

        Returns:
            创建的模型配置
        """
        now = utc_now()
        model_dict = model.model_dump()

        # 如果没有提供 id，生成一个
        if not model_dict.get("id"):
            import uuid

            model_dict["id"] = str(uuid.uuid4())

        model_dict["created_at"] = now.isoformat()
        model_dict["updated_at"] = now.isoformat()

        # 加密 api_key
        # 落库前加密 api_key（明文绝不入库）
        if model_dict.get("api_key"):
            model_dict["api_key"] = await self._encrypt_api_key(model_dict["api_key"])

        await self._get_collection().insert_one(model_dict)
        # 返回前解密 api_key
        # 返回给调用方前再解密回明文，保持接口对上层透明
        model_dict = await self._decrypt_doc(model_dict)
        return ModelConfig(**model_dict)

    async def update(self, model_id: str, update: dict[str, Any]) -> Optional[ModelConfig]:
        """更新模型配置

        Args:
            model_id: 模型 ID
            update: 更新字段

        Returns:
            更新后的模型配置，不存在返回 None
        """
        update["updated_at"] = utc_now_iso()

        # 加密 api_key（如果更新中包含）
        # 仅当更新包含非空 api_key 时才加密（None 表示不改动/清空由上层控制）
        if "api_key" in update and update["api_key"] is not None:
            update["api_key"] = await self._encrypt_api_key(update["api_key"])

        # find_one_and_update + return_document=True 直接拿到更新后的文档
        result = await self._get_collection().find_one_and_update(
            {"id": model_id},
            {"$set": update},
            return_document=True,
        )
        if not result:
            return None
        result.pop("_id", None)
        result = await self._decrypt_doc(result)
        return ModelConfig(**result)

    async def delete(self, model_id: str) -> bool:
        """删除模型配置

        Args:
            model_id: 模型 ID

        Returns:
            是否删除成功
        """
        # 返回是否真的删除了记录
        result = await self._get_collection().delete_one({"id": model_id})
        return result.deleted_count > 0

    async def exists(self, value: str) -> bool:
        """检查模型 value 是否已存在

        Args:
            value: 模型标识符

        Returns:
            是否存在
        """
        # 仅判断存在性（不区分渠道/是否启用）
        doc = await self._get_collection().find_one({"value": value})
        return doc is not None

    @staticmethod
    def _upsert_identity_filter(model: ModelConfig) -> dict[str, Any]:
        # upsert 的身份键 = (value, provider, api_base)，与唯一复合索引保持一致
        return {
            "value": model.value,
            "provider": model.provider,
            "api_base": model.api_base,
        }

    async def count(self, include_disabled: bool = False) -> dict[str, int]:
        """统计模型数量

        Args:
            include_disabled: 是否包含已禁用的模型

        Returns:
            {"total": int, "enabled": int}
        """
        pipeline = [
            {
                "$facet": {
                    "total": [{"$count": "count"}],
                    "enabled": [{"$match": {"enabled": True}}, {"$count": "count"}],
                }
            }
        ]
        # 用 $facet 一次聚合同时算出总数与启用数
        result = await self._get_collection().aggregate(pipeline).to_list(length=1)
        if result:
            # facet 结果为空数组时对应计数为 0
            facet = result[0]
            total = facet["total"][0]["count"] if facet["total"] else 0
            enabled = facet["enabled"][0]["count"] if facet["enabled"] else 0
        else:
            total = 0
            enabled = 0
        return {"total": total, "enabled": enabled}

    async def toggle(self, model_id: str, enabled: bool) -> Optional[ModelConfig]:
        """启用/禁用模型

        Args:
            model_id: 模型 ID
            enabled: 是否启用

        Returns:
            更新后的模型配置
        """
        # 复用 update，仅改 enabled 字段
        return await self.update(model_id, {"enabled": enabled})

    async def reorder(self, model_ids: list[str]) -> list[ModelConfig]:
        """批量更新模型顺序

        Args:
            model_ids: 模型 ID 列表（按新顺序排列）

        Returns:
            更新后的所有模型
        """
        # 用列表下标作为新的 order 值，一次批量写入
        now = utc_now_iso()
        operations = [
            UpdateOne(
                {"id": model_id},
                {"$set": {"order": order, "updated_at": now}},
            )
            for order, model_id in enumerate(model_ids)
        ]
        if operations:
            await _bulk_write_in_batches(self._get_collection(), operations)
        return await self.list_models()

    async def upsert_by_value(self, model: ModelConfig) -> tuple[ModelConfig, bool]:
        """根据 value 插入或更新模型

        Args:
            model: 模型配置

        Returns:
            (模型配置, 是否为新创建)
        """
        # 按身份键 (value/provider/api_base) 判断是更新还是新建
        identity_filter = self._upsert_identity_filter(model)
        existing_doc = await self._get_collection().find_one(identity_filter)
        existing = None
        if existing_doc:
            existing_doc.pop("_id", None)
            existing_doc = await self._decrypt_doc(existing_doc)
            existing = ModelConfig(**existing_doc)
        if existing:
            # 更新时排除身份键与 id/created_at（这些不允许被覆盖）
            update_data = model.model_dump(
                exclude={"id", "value", "provider", "api_base", "created_at"}
            )
            update_data["updated_at"] = utc_now_iso()

            # 加密 api_key
            if update_data.get("api_key"):
                update_data["api_key"] = await self._encrypt_api_key(update_data["api_key"])

            updated = await self._get_collection().find_one_and_update(
                identity_filter,
                {"$set": update_data},
                return_document=True,
            )
            updated.pop("_id", None)
            updated = await self._decrypt_doc(updated)
            return ModelConfig(**updated), False
        else:
            # 不存在则走 create 新建
            created = await self.create(model)
            return created, True

    async def bulk_upsert_by_value(self, models: list[ModelConfig]) -> list[ModelConfig]:
        """批量根据 value 插入或更新模型（单次 bulk_write）

        Args:
            models: 模型配置列表

        Returns:
            创建/更新后的模型配置列表
        """
        now = utc_now_iso()
        import uuid

        operations = []
        identity_filters = []
        for model in models:
            model_dict = model.model_dump()
            identity_filter = self._upsert_identity_filter(model)
            identity_filters.append(identity_filter)
            # 加密 api_key
            if model_dict.get("api_key"):
                model_dict["api_key"] = await self._encrypt_api_key(model_dict["api_key"])

            # 生成 ID 如果没有
            if not model_dict.get("id"):
                model_dict["id"] = str(uuid.uuid4())

            # $set 只更新非身份键字段（身份键与 created_at 由 $setOnInsert 保护）
            update_fields = {
                k: v
                for k, v in model_dict.items()
                if k not in ("id", "value", "provider", "api_base", "created_at")
            }
            update_fields["updated_at"] = now

            operations.append(
                UpdateOne(
                    identity_filter,
                    {
                        "$set": update_fields,
                        # $setOnInsert：仅在新建时写入 id/身份键/created_at，更新时不动
                        "$setOnInsert": {
                            "id": model_dict["id"],
                            "value": model.value,
                            "provider": model.provider,
                            "api_base": model.api_base,
                            "created_at": now,
                        },
                    },
                    upsert=True,
                )
            )

        if operations:
            await _bulk_write_in_batches(self._get_collection(), operations)

        # 返回更新后的模型列表
        # 用身份键分批 $or 查询回读结果（分批规避超大查询）
        result = []
        for start in range(0, len(identity_filters), MODEL_RESTRICTED_LIST_LIMIT):
            batch_filters = identity_filters[start : start + MODEL_RESTRICTED_LIST_LIMIT]
            if not batch_filters:
                continue
            cursor = self._get_collection().find({"$or": batch_filters})
            async for doc in cursor:
                doc.pop("_id", None)
                doc = await self._decrypt_doc(doc)
                result.append(ModelConfig(**doc))
        return result

    async def delete_all(self) -> int:
        """删除所有模型配置

        Returns:
            删除的数量
        """
        # 清空整个集合，返回删除条数
        result = await self._get_collection().delete_many({})
        return result.deleted_count


# 全局单例
_model_storage: Optional[ModelStorage] = None


def get_model_storage() -> ModelStorage:
    """获取 Model 配置存储单例"""
    # 首次调用时创建并缓存单例
    global _model_storage
    if _model_storage is None:
        _model_storage = ModelStorage()
    return _model_storage

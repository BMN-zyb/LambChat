"""
Agent 配置存储层

提供 Agent 配置的数据库操作：
- 全局 Agent 启用/禁用配置
- 角色可用的 Agents 映射
- 用户默认 Agent 设置
"""

from typing import Any, Optional

from src.infra.agent.model_access import ROLE_MODEL_ACCESS_LIMIT
from src.infra.utils.datetime import utc_now, utc_now_iso
from src.kernel.config import settings
from src.kernel.schemas.agent import AgentCatalogConfig, AgentConfig, UserAgentPreference

# MongoDB 集合名称
_COLL_AGENT_CONFIG = "agent_config"
_COLL_AGENT_CATALOG_CONFIG = "agent_catalog_config"
_COLL_ROLE_AGENTS = "role_agents"
_COLL_ROLE_MODELS = "role_models"
_COLL_USER_PREFERENCES = "user_agent_preferences"
# 各类查询/映射的数量上限，防止越权配置或超大文档拖垮查询
ROLE_AGENT_ACCESS_LIMIT = 100
AGENT_CATALOG_LIST_LIMIT = 100
ROLE_AGENT_MAPPING_LIST_LIMIT = 500
ROLE_MODEL_MAPPING_LIST_LIMIT = 500


def _bounded_unique(values: list[str], limit: int) -> list[str]:
    # 按插入顺序去重并截断到 limit 个：保证映射有序、无重复且有界
    bounded = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        bounded.append(value)
        if len(bounded) >= limit:
            break
    return bounded


class AgentConfigStorage:
    """
    Agent 配置存储类

    使用 MongoDB 存储配置数据：
    - 全局 agent 配置 (collection: agent_config)
    - 角色-agents 映射 (collection: role_agents)
    - 用户默认 agent (collection: user_agent_preferences)
    """

    def __init__(self):
        # 集合句柄缓存：首次使用时才连接，见 _get_collection
        self._collections: dict[str, Any] = {}

    def _get_collection(self, name: str):
        """延迟加载 MongoDB 集合"""
        # 惰性获取并缓存集合句柄，避免模块导入期就建立数据库连接
        if name not in self._collections:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collections[name] = db[name]
        return self._collections[name]

    async def ensure_indexes(self):
        """创建必要的 MongoDB 索引"""
        # 各集合的唯一键索引，保证 upsert 语义正确、查询走索引
        await self._get_collection(_COLL_AGENT_CONFIG).create_index("type", unique=True)
        await self._get_collection(_COLL_AGENT_CATALOG_CONFIG).create_index("agent_id", unique=True)
        await self._get_collection(_COLL_ROLE_AGENTS).create_index("role_id", unique=True)
        await self._get_collection(_COLL_ROLE_MODELS).create_index("role_id", unique=True)
        await self._get_collection(_COLL_USER_PREFERENCES).create_index("user_id", unique=True)

    # ============================================
    # 全局 Agent 配置
    # ============================================

    async def get_global_config(self) -> list[AgentConfig]:
        """获取全局 Agent 配置"""
        # 全局配置存于单文档（type=global），其 agents 字段是配置数组
        doc = await self._get_collection(_COLL_AGENT_CONFIG).find_one({"type": "global"})
        if not doc:
            return []
        return [AgentConfig(**agent) for agent in doc.get("agents", [])]

    async def set_global_config(self, agents: list[AgentConfig]) -> list[AgentConfig]:
        """设置全局 Agent 配置"""
        # upsert 覆盖式写入整个 agents 数组
        now = utc_now()
        await self._get_collection(_COLL_AGENT_CONFIG).update_one(
            {"type": "global"},
            {
                "$set": {
                    "agents": [agent.model_dump() for agent in agents],
                    "updated_at": now.isoformat(),
                }
            },
            upsert=True,
        )
        return agents

    async def get_enabled_agent_ids(self) -> list[str]:
        """获取全局启用的 Agent ID 列表"""
        # 优先使用新的展示目录（catalog）；目录为空时回退到旧的全局配置
        catalog = await self.get_catalog_config()
        if catalog:
            return [a.id for a in catalog if a.enabled]
        agents = await self.get_global_config()
        return [a.id for a in agents if a.enabled]

    async def is_agent_enabled(self, agent_id: str) -> bool:
        """Check whether one agent is globally enabled without loading the whole catalog."""
        # 三级判定，避免为查单个 agent 而加载整个目录：
        # 1) 目录中存在该 agent 且未被禁用 -> 启用
        catalog_collection = self._get_collection(_COLL_AGENT_CATALOG_CONFIG)
        catalog_doc = await catalog_collection.find_one(
            {
                "$or": [{"agent_id": agent_id}, {"id": agent_id}],
                "enabled": {"$ne": False},
            },
            {"_id": 1},
        )
        if catalog_doc:
            return True

        # 2) 目录存在但没匹配到该 agent -> 视为未启用（目录是权威来源）
        catalog_exists = await catalog_collection.find_one({}, {"_id": 1})
        if catalog_exists:
            return False

        # 3) 目录为空时回退查旧全局配置里该 agent 是否 enabled
        global_doc = await self._get_collection(_COLL_AGENT_CONFIG).find_one(
            {
                "type": "global",
                "agents": {
                    "$elemMatch": {
                        "id": agent_id,
                        "enabled": True,
                    }
                },
            },
            {"_id": 1},
        )
        return bool(global_doc)

    # ============================================
    # Agent 展示目录配置
    # ============================================

    async def get_catalog_config(self) -> list[AgentCatalogConfig]:
        """获取可配置 Agent 展示目录。"""
        # 拉取目录（带上限），在应用层按 sort_order + agent_id 排序保证稳定顺序
        cursor = (
            self._get_collection(_COLL_AGENT_CATALOG_CONFIG).find().limit(AGENT_CATALOG_LIST_LIMIT)
        )
        docs = [doc async for doc in cursor]
        docs.sort(key=lambda doc: (doc.get("sort_order", 100), doc.get("agent_id", "")))
        # 兼容新旧字段：id 优先取 agent_id，无 id 的脏数据被过滤掉
        return [
            AgentCatalogConfig(
                id=doc.get("agent_id") or doc.get("id"),
                name=doc.get("name", ""),
                description=doc.get("description", ""),
                enabled=doc.get("enabled", True),
                icon=doc.get("icon") or "Bot",
                sort_order=doc.get("sort_order", 100),
                labels=doc.get("labels", {}),
            )
            for doc in docs
            if doc.get("agent_id") or doc.get("id")
        ]

    async def set_catalog_config(
        self,
        agents: list[AgentCatalogConfig],
    ) -> list[AgentCatalogConfig]:
        """设置可配置 Agent 展示目录。"""
        now = utc_now_iso()
        collection = self._get_collection(_COLL_AGENT_CATALOG_CONFIG)

        # 逐个按 agent_id upsert，保留其余字段并刷新更新时间
        for agent in agents:
            payload = agent.model_dump()
            agent_id = payload.pop("id")
            await collection.update_one(
                {"agent_id": agent_id},
                {
                    "$set": {
                        **payload,
                        "agent_id": agent_id,
                        "updated_at": now,
                    }
                },
                upsert=True,
            )

        # 删除本次未提交的旧记录，使目录与传入列表严格一致
        registered_ids = [agent.id for agent in agents]
        if registered_ids:
            await collection.delete_many({"agent_id": {"$nin": registered_ids}})

        return agents

    # ============================================
    # 角色 Agents 映射
    # ============================================

    async def get_role_agents(self, role_id: str) -> Optional[list[str]]:
        """
        获取角色的可用 Agents

        Returns:
            可用的 Agent ID 列表，None 表示未配置
        """
        # 用 $slice 在库端截断数组，避免拉回超大文档
        doc = await self._get_collection(_COLL_ROLE_AGENTS).find_one(
            {"role_id": role_id},
            {"allowed_agents": {"$slice": ROLE_AGENT_ACCESS_LIMIT}},
        )
        # 无文档 -> 未配置（None）；有文档但列表为空也按未配置处理
        if not doc:
            return None
        allowed_agents = doc.get("allowed_agents")
        if not allowed_agents:
            return None
        return allowed_agents[:ROLE_AGENT_ACCESS_LIMIT]

    async def set_role_agents(
        self, role_id: str, role_name: str, agent_ids: list[str]
    ) -> list[str]:
        """设置角色的可用 Agents"""
        # 去重截断后 upsert；返回实际落库的有界列表
        now = utc_now()
        bounded_agent_ids = _bounded_unique(agent_ids, ROLE_AGENT_ACCESS_LIMIT)
        await self._get_collection(_COLL_ROLE_AGENTS).update_one(
            {"role_id": role_id},
            {
                "$set": {
                    "role_name": role_name,
                    "allowed_agents": bounded_agent_ids,
                    "updated_at": now.isoformat(),
                }
            },
            upsert=True,
        )
        return bounded_agent_ids

    async def delete_role_agents(self, role_id: str) -> bool:
        """删除角色的 Agents 配置"""
        # 返回是否真的删掉了记录
        result = await self._get_collection(_COLL_ROLE_AGENTS).delete_one({"role_id": role_id})
        return result.deleted_count > 0

    async def get_all_role_agents(self) -> list[dict]:
        """获取所有角色的 Agents 配置"""
        # 遍历全部映射（带上限），每条都做去重截断
        cursor = self._get_collection(_COLL_ROLE_AGENTS).find().limit(ROLE_AGENT_MAPPING_LIST_LIMIT)
        return [
            {
                "role_id": doc["role_id"],
                "role_name": doc.get("role_name", ""),
                "allowed_agents": _bounded_unique(
                    doc.get("allowed_agents", []),
                    ROLE_AGENT_ACCESS_LIMIT,
                ),
            }
            async for doc in cursor
        ]

    # ============================================
    # 角色 Models 映射
    # ============================================

    async def get_role_models(self, role_id: str) -> Optional[list[str]]:
        """
        获取角色的可用 Models

        Returns:
            可用的 Model value 列表，None 表示未配置（不限制）
        """
        doc = await self._get_collection(_COLL_ROLE_MODELS).find_one(
            {"role_id": role_id},
            {"allowed_models": {"$slice": ROLE_MODEL_ACCESS_LIMIT}},
        )
        if not doc:
            return None
        # 注意与 role_agents 的差异：这里只在字段为 None 时才视为未配置；
        # 空列表 [] 是有效配置，表示"该角色不允许任何模型"
        allowed_models = doc.get("allowed_models")
        if allowed_models is None:
            return None
        return allowed_models[:ROLE_MODEL_ACCESS_LIMIT]

    async def set_role_models(
        self, role_id: str, role_name: str, model_values: list[str]
    ) -> list[str]:
        """设置角色的可用 Models"""
        # 去重截断后 upsert
        now = utc_now()
        bounded_model_values = _bounded_unique(model_values, ROLE_MODEL_ACCESS_LIMIT)
        await self._get_collection(_COLL_ROLE_MODELS).update_one(
            {"role_id": role_id},
            {
                "$set": {
                    "role_name": role_name,
                    "allowed_models": bounded_model_values,
                    "updated_at": now.isoformat(),
                }
            },
            upsert=True,
        )
        return bounded_model_values

    async def delete_role_models(self, role_id: str) -> bool:
        """删除角色的 Models 配置"""
        result = await self._get_collection(_COLL_ROLE_MODELS).delete_one({"role_id": role_id})
        return result.deleted_count > 0

    async def get_all_role_models(self) -> list[dict]:
        """获取所有角色的 Models 配置"""
        # 遍历全部映射（带上限），逐条去重截断
        cursor = self._get_collection(_COLL_ROLE_MODELS).find().limit(ROLE_MODEL_MAPPING_LIST_LIMIT)
        return [
            {
                "role_id": doc["role_id"],
                "role_name": doc.get("role_name", ""),
                "allowed_models": _bounded_unique(
                    doc.get("allowed_models", []),
                    ROLE_MODEL_ACCESS_LIMIT,
                ),
            }
            async for doc in cursor
        ]

    async def remove_model_from_all_roles(self, model_value: str) -> int:
        """从所有角色的 allowed_models 中移除指定模型（单次批量操作）。

        Returns:
            受影响的文档数量
        """
        # 用 $pull 一次性从所有匹配角色的数组中删除该模型（删除模型时同步清理映射）
        now = utc_now_iso()
        result = await self._get_collection(_COLL_ROLE_MODELS).update_many(
            {"allowed_models": model_value},
            {
                "$pull": {"allowed_models": model_value},
                "$set": {"updated_at": now},
            },
        )
        return result.modified_count

    async def clear_all_role_models(self) -> int:
        """清空所有角色的 allowed_models（单次批量操作）。

        Returns:
            受影响的文档数量
        """
        # 仅对非空数组（allowed_models.0 存在）执行清空，避免无谓写入
        now = utc_now_iso()
        result = await self._get_collection(_COLL_ROLE_MODELS).update_many(
            {"allowed_models.0": {"$exists": True}},
            {"$set": {"allowed_models": [], "updated_at": now}},
        )
        return result.modified_count

    # ============================================
    # 用户默认 Agent
    # ============================================

    async def get_user_preference(self, user_id: str) -> Optional[UserAgentPreference]:
        """获取用户的默认 Agent 设置"""
        # 无记录返回 None，表示用户未设置默认 agent
        doc = await self._get_collection(_COLL_USER_PREFERENCES).find_one({"user_id": user_id})
        if not doc:
            return None
        return UserAgentPreference(default_agent_id=doc.get("default_agent_id"))

    async def set_user_preference(self, user_id: str, agent_id: str) -> UserAgentPreference:
        """设置用户的默认 Agent"""
        # 按 user_id upsert 用户默认 agent
        now = utc_now()
        await self._get_collection(_COLL_USER_PREFERENCES).update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "default_agent_id": agent_id,
                    "updated_at": now.isoformat(),
                }
            },
            upsert=True,
        )
        return UserAgentPreference(default_agent_id=agent_id)

    async def delete_user_preference(self, user_id: str) -> bool:
        """删除用户的默认 Agent 设置"""
        result = await self._get_collection(_COLL_USER_PREFERENCES).delete_one({"user_id": user_id})
        return result.deleted_count > 0


# 全局单例
_agent_config_storage: Optional[AgentConfigStorage] = None


def get_agent_config_storage() -> AgentConfigStorage:
    """获取 Agent 配置存储单例"""
    # 首次调用时创建并缓存单例，后续复用同一实例（含集合句柄缓存）
    global _agent_config_storage
    if _agent_config_storage is None:
        _agent_config_storage = AgentConfigStorage()
    return _agent_config_storage

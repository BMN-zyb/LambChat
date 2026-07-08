"""
用户环境变量存储

存储用户的环境变量（加密），用于注入到沙箱中。
- MongoDB 集合: user_env_vars
- 每条记录: {user_id, key, value(加密), created_at, updated_at}
- 唯一索引: (user_id, key)
- 复用 MCP 加密模块 encrypt_value / decrypt_value
"""

import asyncio
from typing import Any, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.mcp.encryption import decrypt_value, encrypt_value
from src.infra.utils.datetime import utc_now_iso
from src.kernel.config import settings
from src.kernel.schemas.envvar import EnvVarResponse

logger = get_logger(__name__)

# MongoDB 集合名
COLLECTION_NAME = "user_env_vars"

# 每用户最大环境变量数量
MAX_ENV_VARS_PER_USER = 50
# 单个环境变量值的最大字符数，避免用户塞入超大文本拖慢加密/存储/沙箱注入
ENV_VAR_MAX_VALUE_CHARS = 16_000
# 单个用户所有环境变量值的字符数总和上限，防止绕过单值限制后用"数量 x 大小"叠加撑爆
ENV_VAR_MAX_TOTAL_VALUE_CHARS = 64_000


def _validate_env_var_value_size(value: str) -> None:
    # 校验单个值是否超过单值长度上限，超限直接抛错，交由上层转成 4xx 响应
    if len(str(value)) > ENV_VAR_MAX_VALUE_CHARS:
        raise ValueError(f"Environment variable value too large (max {ENV_VAR_MAX_VALUE_CHARS})")


def _validate_env_var_bulk_value_size(variables: dict[str, str]) -> None:
    # 批量设置场景下，既要逐个校验单值上限，也要累加校验总字符数上限
    total_chars = 0
    for value in variables.values():
        text = str(value)
        if len(text) > ENV_VAR_MAX_VALUE_CHARS:
            raise ValueError(
                f"Environment variable value too large (max {ENV_VAR_MAX_VALUE_CHARS})"
            )
        total_chars += len(text)
        if total_chars > ENV_VAR_MAX_TOTAL_VALUE_CHARS:
            raise ValueError(
                "Environment variable values too large "
                f"(max {ENV_VAR_MAX_TOTAL_VALUE_CHARS} total characters)"
            )


class EnvVarStorage:
    """用户环境变量存储（加密）"""

    # 类级别状态，跨实例共享：确保唯一索引只被调度一次，避免每次新建
    # EnvVarStorage 实例都重复触发一次 create_index
    _index_task: asyncio.Task[None] | None = None
    _index_ensured = False

    def __init__(self):
        self._collection: Any = None

    @property
    def _coll(self):
        """延迟加载 MongoDB 集合"""
        if self._collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db[COLLECTION_NAME]
            # 首次拿到集合时顺带异步调度索引创建，无需阻塞调用方
            self._schedule_index()
        return self._collection

    def _schedule_index(self) -> None:
        # 幂等调度：已确保过索引，或已有一个未完成的建索引任务，都直接跳过
        cls = type(self)
        if cls._index_ensured:
            return
        task = cls._index_task
        if task is not None and not task.done():
            return
        try:
            task = asyncio.create_task(self._ensure_index())
        except RuntimeError:
            # 当前没有运行中的事件循环时（例如同步上下文调用），放弃调度，
            # 索引会在下一次有事件循环的调用中再尝试
            return
        # 挂一个空操作的 done_callback 只是为了「消费」掉异常，
        # 防止任务失败时产生 "Task exception was never retrieved" 的警告日志
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        cls._index_task = task
        cls._index_ensured = True

    async def _ensure_index(self):
        """创建唯一索引 (user_id + key)"""
        try:
            await self._coll.create_index(
                [("user_id", 1), ("key", 1)],
                unique=True,
                name="user_id_key_unique_idx",
                background=True,
            )
        except Exception as e:
            # 建索引失败不影响正常读写功能，仅记录警告
            logger.warning(f"Failed to create index on {COLLECTION_NAME}: {e}")

    # ── 加密辅助 ──────────────────────────────────────────────────

    @staticmethod
    async def _encrypt_value(value: str) -> dict:
        """加密单个值（包装为 dict 后加密）"""
        # encrypt_value 是同步阻塞的加密调用（CPU 密集型），
        # 用 run_blocking_io 丢到线程池执行，避免阻塞事件循环
        return await run_blocking_io(encrypt_value, {"v": value})

    @staticmethod
    async def _decrypt_value(encrypted: Any) -> str:
        """解密单个值"""
        result = await run_blocking_io(decrypt_value, encrypted)
        if isinstance(result, dict):
            return result.get("v", "")
        return str(result) if result else ""

    # ── CRUD ──────────────────────────────────────────────────────

    async def list_vars(self, user_id: str) -> list[EnvVarResponse]:
        """列出用户所有环境变量（value 掩码）"""
        # 投影中排除 _id 和 user_id，只取展示所需字段；列表接口不返回明文，
        # 而是统一用 "***" 掩码，避免敏感值在列表页被批量泄露
        cursor = (
            self._coll.find(
                {"user_id": user_id},
                {"_id": 0, "user_id": 0},
            )
            .sort("key", 1)
            .limit(MAX_ENV_VARS_PER_USER)
        )
        results = []
        async for doc in cursor:
            results.append(
                EnvVarResponse(
                    key=doc["key"],
                    value="***",  # 掩码
                    created_at=doc.get("created_at"),
                    updated_at=doc.get("updated_at"),
                )
            )
        return results

    async def get_var(self, user_id: str, key: str) -> Optional[EnvVarResponse]:
        """获取单个环境变量（明文）"""
        # 与 list_vars 不同，单条查询会真正解密返回明文，
        # 用于用户主动查看/编辑某一个变量的场景
        doc = await self._coll.find_one(
            {"user_id": user_id, "key": key},
            {"_id": 0, "user_id": 0},
        )
        if not doc:
            return None
        return EnvVarResponse(
            key=doc["key"],
            value=await self._decrypt_value(doc.get("value")),
            created_at=doc.get("created_at"),
            updated_at=doc.get("updated_at"),
        )

    async def get_decrypted_vars(self, user_id: str) -> dict[str, str]:
        """获取用户所有环境变量的明文 dict（供沙箱注入）"""
        cursor = self._coll.find(
            {"user_id": user_id},
            {"_id": 0, "key": 1, "value": 1},
        ).limit(MAX_ENV_VARS_PER_USER)
        result = {}
        async for doc in cursor:
            try:
                result[doc["key"]] = await self._decrypt_value(doc.get("value"))
            except Exception as e:
                # 单个变量解密失败（例如加密密钥轮换、数据损坏）不应影响其他变量的注入，
                # 跳过该变量并记录警告即可
                logger.warning(f"Failed to decrypt env var '{doc['key']}' for user {user_id}: {e}")
        return result

    async def set_var(self, user_id: str, key: str, value: str) -> EnvVarResponse:
        """设置（upsert）单个环境变量"""
        _validate_env_var_value_size(value)
        now = utc_now_iso()

        # 检查数量上限（仅 insert 时）
        # 先判断这个 key 是否已存在：已存在则是更新操作，不占用新的数量配额；
        # 不存在才需要检查是否会超出每用户上限
        existing = await self._coll.find_one({"user_id": user_id, "key": key})
        if not existing:
            count = await self._coll.count_documents({"user_id": user_id})
            if count >= MAX_ENV_VARS_PER_USER:
                raise ValueError(f"Maximum {MAX_ENV_VARS_PER_USER} environment variables per user")

        encrypted = await self._encrypt_value(value)
        await self._coll.update_one(
            {"user_id": user_id, "key": key},
            {
                "$set": {
                    "value": encrypted,
                    "updated_at": now,
                },
                # $setOnInsert 保证 created_at 只在首次插入时写入，更新时不会被覆盖
                "$setOnInsert": {
                    "created_at": now,
                },
            },
            upsert=True,
        )

        return EnvVarResponse(
            key=key,
            value="***",
            created_at=existing.get("created_at") if existing else now,
            updated_at=now,
        )

    async def set_vars_bulk(self, user_id: str, variables: dict[str, str]) -> int:
        """批量设置环境变量"""
        # 先做一次粗略校验：本次请求里不重复的 key 数量本身就不能超过上限
        if len({key for key in variables if key}) > MAX_ENV_VARS_PER_USER:
            raise ValueError(
                f"Would exceed maximum {MAX_ENV_VARS_PER_USER} environment variables per user"
            )
        _validate_env_var_bulk_value_size(variables)

        now = utc_now_iso()
        count = 0

        # 检查数量上限
        # 结合已有 key 集合计算出"真正会新增"的 key 数量，
        # 避免"更新已存在的 key"被误判为占用新配额而拒绝合法请求
        current_count = await self._coll.count_documents({"user_id": user_id})
        existing_keys = set()
        existing_cursor = self._coll.find({"user_id": user_id}, {"key": 1}).limit(
            MAX_ENV_VARS_PER_USER
        )
        async for doc in existing_cursor:
            existing_keys.add(doc["key"])

        new_keys = set(variables.keys()) - existing_keys
        if current_count + len(new_keys) > MAX_ENV_VARS_PER_USER:
            raise ValueError(
                f"Would exceed maximum {MAX_ENV_VARS_PER_USER} environment variables per user"
            )

        for key, value in variables.items():
            encrypted = await self._encrypt_value(value)
            await self._coll.update_one(
                {"user_id": user_id, "key": key},
                {
                    "$set": {
                        "value": encrypted,
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "created_at": now,
                    },
                },
                upsert=True,
            )
            count += 1

        return count

    async def delete_var(self, user_id: str, key: str) -> bool:
        """删除单个环境变量"""
        result = await self._coll.delete_one({"user_id": user_id, "key": key})
        return result.deleted_count > 0

    async def delete_all_vars(self, user_id: str) -> int:
        """删除用户所有环境变量"""
        result = await self._coll.delete_many({"user_id": user_id})
        return result.deleted_count

    async def close(self) -> None:
        # 关闭时取消尚未完成的建索引任务，并重置类级状态标记，
        # 便于测试或重启场景下重新调度索引创建
        cls = type(self)
        task = cls._index_task
        cls._index_task = None
        cls._index_ensured = False
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._collection = None

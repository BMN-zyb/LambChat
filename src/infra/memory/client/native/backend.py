"""Native Memory Backend — MongoDB-backed, zero external dependencies."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# NativeMemoryBackend 是 MemoryBackend 接口的一种零外部依赖实现：不依赖任何
# 专门的记忆/向量服务，全部功能建立在一个 MongoDB collection 之上（可选再叠加
# 一个 OpenAI 兼容的 embedding 接口做语义检索，没配置就退化为纯文本检索）。
# 本文件是"总装配"层，把其余同目录模块串联起来对外提供统一的记忆能力：
#   - classification.py：判断内容是否值得记、是否与已有记忆重复
#   - content.py：决定内容内联存储还是外部存储，并处理内容的增删
#   - summaries.py：调 LLM 生成 title/summary/tags 等摘要信息
#   - search.py：recall（检索）的具体实现
#   - indexing.py：build_memory_index（记忆索引文本）的具体实现
#   - consolidation.py：consolidate_memories（记忆整合/剪除）的具体实现
# 对外暴露的核心方法是 retain（手动记忆）/ recall（检索）/ delete（删除）/
# auto_retain_from_text（后台自动记忆决策）/ consolidate_memories（整合维护）。
# ============================================================================

import asyncio
import inspect
import uuid
from datetime import timedelta
from typing import Any, Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.memory.client.base import MemoryBackend
from src.infra.memory.client.native.classification import (
    find_existing_memory_match,
    is_manual_memory_worthy,
)
from src.infra.memory.client.native.consolidation import (
    consolidate_memories as run_consolidation,
)
from src.infra.memory.client.native.content import (
    build_content_fields,
    delete_memory_content,
    maybe_await,
)
from src.infra.memory.client.native.indexing import build_memory_index
from src.infra.memory.client.native.models import COLLECTION_NAME
from src.infra.memory.client.native.search import recall_memories
from src.infra.memory.client.native.summaries import (
    _fallback_enrich,
    build_index_label,
    llm_enrich_memory,
)
from src.infra.memory.client.types import MemoryType
from src.infra.storage.mongodb import get_mongo_client
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings

logger = get_logger(__name__)

# context 关键字 -> 记忆类型的简单映射：context 文本里出现对应关键字就归为该类型
_CONTEXT_TYPE_HINTS = {
    "feedback": MemoryType.FEEDBACK,
    "project": MemoryType.PROJECT,
    "reference": MemoryType.REFERENCE,
}


def _infer_memory_type(context: Optional[str] = None) -> str:
    # 按 _CONTEXT_TYPE_HINTS 的关键字在 context 中做子串匹配来猜测记忆类型；
    # 匹配不到任何关键字（或没传 context）时，默认归类为用户画像类记忆（USER）
    if context:
        ctx_lower = context.lower()
        for hint, mt in _CONTEXT_TYPE_HINTS.items():
            if hint in ctx_lower:
                return mt.value
    return MemoryType.USER


# ============================================================================
# NativeMemoryBackend
# ============================================================================


class NativeMemoryBackend(MemoryBackend):
    """MongoDB-native memory backend. No external API dependencies."""

    # Maximum entries in the per-instance index cache
    _INDEX_CACHE_MAX_SIZE: int = 1000

    def __init__(self):
        # 以下资源均为懒加载，构造函数本身不做任何 IO，真正的初始化在 initialize() 中完成
        self._collection: Any = None
        self._embedding_fn: Optional[Callable] = None
        self._httpx_client: Any = None  # keep ref for proper cleanup
        self._store: Any = None
        self._logger = logger
        # In-memory cache for memory index: {user_id: (built_at, index_str)}
        self._index_cache: dict[str, tuple[float, str]] = {}

    @property
    def name(self) -> str:
        # 后端标识名，供上层按配置选择/区分具体使用的是哪种 MemoryBackend 实现
        return "native"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _invalidate_cache(self, user_id: str) -> None:
        """Invalidate local index cache and publish invalidation to other instances."""
        # 先清本进程内的缓存，再尝试广播给其它实例（多实例部署下，各实例都有自己独立
        # 的 _index_cache），让它们也及时失效；广播失败不影响主流程，因为缓存本身还有
        # TTL 兜底，其它实例最终也会自然过期刷新，牺牲的只是短暂的时效性
        self._index_cache.pop(user_id, None)
        try:
            from src.infra.memory.distributed import publish_memory_invalidation

            await publish_memory_invalidation(user_id)
        except Exception:
            pass  # non-critical: other instances will eventually refresh via TTL

    async def initialize(self) -> None:
        """Ensure indexes exist; set up optional embedding function."""
        # 初始化顺序：先建立 collection 引用，再确保索引存在（否则后续查询可能很慢），
        # 然后按配置决定是否启用 embedding（语义检索能力是可选的），
        # 最后做一次遗留数据的清理迁移
        await run_blocking_io(self._ensure_collection)
        await self._create_indexes()
        self._setup_embedding_fn()
        await self._prune_legacy_session_summaries()

    async def close(self) -> None:
        # 依次释放 httpx 客户端连接池和所有内存态引用，供进程优雅关闭/测试间隔离时调用
        if self._httpx_client is not None:
            try:
                await self._httpx_client.aclose()
            except Exception:
                pass
            self._httpx_client = None
        self._collection = None
        self._embedding_fn = None
        self._store = None
        self._index_cache.clear()

    async def _prune_legacy_session_summaries(self) -> None:
        """One-time cleanup for old transcript-style session summary memories."""
        # 与 consolidation.py 里按"超过 7 天"逐步剪除 session_summary 不同，
        # 这里是一次性、无条件的迁移清理：session_summary 这种旧的会话摘要记忆
        # 机制已被废弃，每次启动时把历史遗留的全部清空，不需要再区分新旧
        if self._collection is None:
            return
        try:
            result = await self._collection.delete_many({"source": "session_summary"})
            deleted_count = int(getattr(result, "deleted_count", 0) or 0)
            if deleted_count:
                logger.info(
                    "[NativeMemory] Pruned %d legacy session summary memories",
                    deleted_count,
                )
        except Exception as e:
            logger.debug("[NativeMemory] Failed to prune legacy session summaries: %s", e)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    @staticmethod
    async def _get_memory_model():
        """Get LLM model for memory operations.

        Uses the configured model ID if set, otherwise falls back to the
        default model. Provider credentials and base URL come from the model
        provider configuration.
        """
        # 记忆相关任务（摘要生成、整合、自动记忆决策）用统一的一个模型配置，
        # temperature=0.1 追求稳定、可复现的结构化输出，而不是发散的创造性文本；
        # max_tokens 也刻意限制得比对话场景小，因为这里只需要产出摘要/标签/JSON
        max_tokens = int(getattr(settings, "NATIVE_MEMORY_MAX_TOKENS", 2000))
        from src.infra.llm.client import LLMClient
        from src.infra.llm.models_service import resolve_model_reference

        model_id, model_value = await resolve_model_reference(
            getattr(settings, "NATIVE_MEMORY_MODEL", "")
        )
        model_kwargs: dict[str, Any] = {
            "model_id": model_id,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        if model_value:
            model_kwargs["model"] = model_value
        return await LLMClient.get_model(
            **model_kwargs,
        )

    async def retain(
        self,
        user_id: str,
        content: str,
        context: Optional[str] = None,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[list[str]] = None,
        existing_memory_id: Optional[str] = None,
    ) -> dict[str, Any]:
        # --- Validation (relaxed for manual retention — trust user intent) ---
        if len(content.strip()) < 5:
            return {
                "success": False,
                "error": "Content too short (minimum 5 characters)",
            }

        if not is_manual_memory_worthy(content, context):
            return {
                "success": False,
                "error": "Content rejected: appears transient, noisy, or not durable enough",
            }

        memory_type = _infer_memory_type(context)

        # If caller provides all three, skip LLM enrichment entirely
        # 三个增强字段（title/summary/tags）都已给全时，跳过 LLM 调用直接用；
        # 缺 title 或 summary 时整体走一次富化调用去补齐（同时顺带补 tags，
        # 但已提供的 tags 不会被覆盖）；只缺 tags 时单独补一次 tags 即可，
        # 尽量减少不必要的 LLM 往返次数
        if title and summary and tags:
            tags = [str(t)[:20] for t in tags[:5] if t]
        elif not title or not summary:
            enriched = await llm_enrich_memory(self, content)
            if not tags:
                tags = enriched["tags"]
            if not summary:
                summary = enriched["summary"]
            if not title:
                title = enriched["title"]
        elif not tags:
            enriched = await llm_enrich_memory(self, content)
            tags = enriched["tags"]

        async def fetch_recent_memories(target_user_id: str) -> list[dict[str, Any]]:
            # 只取最近 7 天的记忆作为"是否已有相似记忆"的比对候选池，
            # 太久以前的记忆即使主题相似也未必还适合被合并/覆盖
            seven_days_ago = utc_now() - timedelta(days=7)
            return await self._collection.find(
                {"user_id": target_user_id, "updated_at": {"$gte": seven_days_ago}},
                {"summary": 1, "memory_id": 1, "memory_type": 1},
            ).to_list(length=50)

        existing_match = None
        _match_projection = {
            "memory_id": 1,
            "memory_type": 1,
            "summary": 1,
            "updated_at": 1,
            "content_storage_mode": 1,
            "content_store_key": 1,
        }
        if existing_memory_id:
            # 调用方（如 auto_retain_from_text）已经明确指定了要更新哪条记忆，
            # 直接按 id 精确查找，不再走模糊相似度匹配
            forced_match = await self._collection.find_one(
                {"user_id": user_id, "memory_id": existing_memory_id},
                _match_projection,
            )
            if forced_match:
                existing_match = forced_match
        if existing_match is None:
            # 没有显式指定 -> 用摘要相似度在近期记忆里找可能的重复项，
            # 找到就走"更新"而不是"新建"，避免同一主题反复产生近似重复的记忆
            existing_match = await find_existing_memory_match(
                fetch_recent=fetch_recent_memories,
                user_id=user_id,
                summary=summary,
                memory_type=memory_type,
            )
            # fetch content fields for store cleanup if matched via similarity
            # 相似度匹配得到的候选只带精简字段，缺 content_storage_mode 时
            # 需要额外查一次完整信息，供后面判断是否要清理旧的外部存储内容
            if existing_match and "content_storage_mode" not in existing_match:
                full_doc = await self._collection.find_one(
                    {"user_id": user_id, "memory_id": existing_match["memory_id"]},
                    {"content_storage_mode": 1, "content_store_key": 1},
                )
                if full_doc:
                    existing_match.update(full_doc)

        now = utc_now()
        is_update = existing_match is not None
        _existing: dict[str, Any] = existing_match if is_update else {}  # type: ignore[assignment]
        memory_id = _existing["memory_id"] if is_update else uuid.uuid4().hex
        # 内容存储字段的构造和向量嵌入的计算互不依赖，并发执行以减少总耗时
        content_fields, embedding = await asyncio.gather(
            build_content_fields(self, user_id, memory_id, content),
            self._maybe_embed(content),
        )

        if is_update:
            await self._collection.update_one(
                {"user_id": user_id, "memory_id": _existing["memory_id"]},
                {
                    "$set": {
                        "title": title[:25],
                        "summary": summary[:100],
                        "index_label": build_index_label(title, summary, content),
                        "context": context,
                        "tags": tags,
                        "embedding": embedding,
                        "updated_at": now,
                        **content_fields,
                    }
                },
            )
            # 旧内容如果是外部存储、且新内容换了不同的存储 key（说明内容确实变了），
            # 就把旧的外部内容一并删除，避免留下再也不会被引用的孤儿内容
            if (
                _existing.get("content_storage_mode") == "store"
                and _existing.get("content_store_key")
                and _existing.get("content_store_key") != content_fields.get("content_store_key")
            ):
                await delete_memory_content(self, user_id, _existing.get("content_store_key"))
            await self._invalidate_cache(user_id)
            return {
                "success": True,
                "memory_id": _existing["memory_id"],
                "memory_type": memory_type,
                "updated_existing": True,
                "message": "Memory updated successfully",
            }

        doc = {
            "memory_id": memory_id,
            "user_id": user_id,
            "title": title[:25],
            "summary": summary[:100],
            "index_label": build_index_label(title, summary, content),
            "memory_type": memory_type,
            "context": context,
            "tags": tags,
            # retain() 是用户主动触发的记忆入口，因此一律先标记为 manual；
            # 若是后台自动记忆流程调用它，会在返回后由调用方把 source 改写为 auto_retained
            "source": "manual",
            "embedding": embedding,
            "created_at": now,
            "updated_at": now,
            "accessed_at": now,
            "access_count": 0,
        }
        doc.update(content_fields)

        await self._collection.insert_one(doc)
        # Invalidate index cache (local + distributed)
        await self._invalidate_cache(user_id)

        return {
            "success": True,
            "memory_id": memory_id,
            "memory_type": memory_type,
            "message": "Memory stored successfully",
        }

    async def recall(
        self,
        user_id: str,
        query: str,
        max_results: int = 5,
        memory_types: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        # 检索逻辑本身较复杂（关键词+语义混合排序、重排等），完全委托给 search.py，
        # 这里只是满足 MemoryBackend 接口契约的一层薄转发
        return await recall_memories(self, user_id, query, max_results, memory_types)

    async def delete(
        self,
        user_id: str,
        memory_id: str,
    ) -> dict[str, Any]:
        # 删除前先查一次是否有外部存储的内容需要一并清理
        existing_doc = await self._collection.find_one(
            {"user_id": user_id, "memory_id": memory_id},
            {"content_storage_mode": 1, "content_store_key": 1},
        )
        result = await self._collection.delete_one({"user_id": user_id, "memory_id": memory_id})
        if result.deleted_count > 0:
            if existing_doc and existing_doc.get("content_storage_mode") == "store":
                await delete_memory_content(self, user_id, existing_doc.get("content_store_key"))
            await self._invalidate_cache(user_id)
            return {"success": True, "message": f"Memory {memory_id} deleted"}
        return {"success": False, "error": "Memory not found"}

    # ------------------------------------------------------------------
    # Memory consolidation (internal helper retained for native backend compatibility)
    # ------------------------------------------------------------------

    async def consolidate_memories(self, user_id: str) -> dict[str, Any]:
        # 整合的具体算法在 consolidation.py（此处作为 run_consolidation 导入），
        # 这里只负责套上分布式锁的获取/释放回调，防止多实例并发对同一用户重复整合
        from src.infra.memory.distributed import (
            acquire_consolidation_lock,
            release_consolidation_lock,
        )

        return await run_consolidation(
            self,
            user_id,
            acquire_lock=acquire_consolidation_lock,
            release_lock=release_consolidation_lock,
        )

    async def auto_retain_from_text(self, user_id: str, text: str) -> dict[str, Any]:
        # 后台自动记忆入口：不是用户主动要求记住什么，而是系统扫一段对话文本，
        # 自主判断里面是否含有值得长期记住的信息；判断本身也交给一个 LLM 决策，
        # 通过"工具调用"的方式表达决策——调用 memory_retain 即代表"这段值得记"，
        # 不调用任何工具则代表"这段没有值得记的内容"
        if not text.strip():
            return {"success": True, "stored": 0, "candidates": 0}

        try:
            from src.infra.memory.tools import memory_retain

            # 先检索出与本段文本相似的已有记忆，作为决策 LLM 的参考上下文，
            # 让它有机会选择"更新已有记忆"而不是无脑创建新的重复记忆
            candidates = await self._get_auto_retain_candidates(user_id, text)
            candidates_text = "\n".join(
                (
                    f"- id={item.get('memory_id')} "
                    f"type={item.get('type')} "
                    f"title={item.get('title', '')!r} "
                    f"summary={item.get('summary', '')!r} "
                    f"updated_at={item.get('created_at') or item.get('updated_at', '')}"
                )
                for item in candidates
            )
            model = (await maybe_await(self._get_memory_model())).bind_tools([memory_retain])
            response = await model.ainvoke(
                [
                    SystemMessage(
                        content=(
                            "You are a background memory-retention evaluator.\n"
                            "You receive one user message after the main assistant response has already finished.\n"
                            "You may see similar existing memories.\n"
                            "If the message contains durable cross-session memory, call memory_retain.\n"
                            "If it does not, do not call any tool.\n"
                            "Only retain user identity, preferences with reasons, durable project context, "
                            "explicit feedback, or lasting references. Never retain code, file paths, "
                            "temporary worklogs, greetings, or transient status updates.\n"
                            "When calling memory_retain, ALWAYS provide title, summary, and tags "
                            "— this avoids a second LLM call. Keep title under 25 chars, summary under 80 chars, "
                            "and provide 3-5 keyword tags.\n"
                            "If one existing memory already covers the same topic, call memory_retain with "
                            "`existing_memory_id` set to that memory id so the system updates it instead of "
                            "creating a duplicate.\n"
                            "If none match closely enough, omit `existing_memory_id`."
                        )
                    ),
                    HumanMessage(
                        content=(
                            f"User message:\n{text}\n\n"
                            f"Similar existing memories:\n{candidates_text or '(none)'}"
                        )
                    ),
                ]
            )
        except Exception as e:
            # 后台任务，出错不能影响主对话流程，记录调试日志后返回失败结果即可
            self._logger.debug("[NativeMemory] Background auto-retain decision failed: %s", e)
            return {"success": False, "stored": 0, "candidates": 0, "error": str(e)}

        tool_calls = getattr(response, "tool_calls", None) or []
        stored = 0
        for tool_call in tool_calls:
            if tool_call.get("name") != "memory_retain":
                continue
            args = tool_call.get("args") or {}
            content = str(args.get("content") or "").strip()
            if not content:
                continue
            # Ensure all three enrichment fields are present so retain() skips the LLM call.
            # Rule-based fallbacks fill gaps when the decision LLM omits optional params.
            title = args.get("title")
            summary = args.get("summary")
            tags = args.get("tags")
            if not title or not summary or not tags:
                # 用规则兜底而不是再调一次 LLM 富化——决策 LLM 大多数情况下会自己给全
                # 这三个字段，这里只是给个安全网，避免为了补几个字段又多花一次模型调用
                enriched = _fallback_enrich(content)
                title = title or enriched["title"]
                summary = summary or enriched["summary"]
                tags = tags or enriched["tags"]
            result = await self.retain(
                user_id,
                content,
                context=args.get("context"),
                title=title,
                summary=summary,
                tags=tags,
                existing_memory_id=args.get("existing_memory_id"),
            )
            if result.get("success"):
                if result.get("memory_id") and self._collection is not None:
                    # retain() 内部统一写入 source="manual"，这里事后改写成
                    # auto_retained，标明这条记忆其实是系统自动决定保留的，
                    # 供索引排序/整合剪除时按不同信任度区别对待
                    await self._collection.update_one(
                        {"user_id": user_id, "memory_id": result["memory_id"]},
                        {"$set": {"source": "auto_retained"}},
                    )
                stored += 1
        return {"success": True, "stored": stored, "candidates": len(tool_calls)}

    async def _get_auto_retain_candidates(self, user_id: str, text: str) -> list[dict[str, Any]]:
        # touch_access=False：仅用于内部重复检测，不应算作一次真实的"记忆被访问"；
        # enable_rerank=False：只是给决策 LLM 参考，不需要精排序带来的额外开销
        result = await recall_memories(
            self,
            user_id,
            text,
            max_results=5,
            touch_access=False,
            enable_rerank=False,
        )
        if not result.get("success"):
            return []
        return list(result.get("memories") or [])

    # ------------------------------------------------------------------
    # Memory index (for system prompt injection)
    # ------------------------------------------------------------------

    async def build_memory_index(self, user_id: str) -> str:
        # 方法名与 indexing.py 里导入的同名函数相同：self.build_memory_index 调用的
        # 是本方法（绑定方法），而方法体里的 build_memory_index(...) 引用的是模块级
        # 导入的那个函数，二者在 Python 作用域规则下不冲突；这里只是简单转发
        return await build_memory_index(self, user_id)

    async def _update_access_stats(self, memory_ids: list[str], user_id: str = "") -> None:
        # 用一次 update_many 批量给多条记忆同时打上"刚被访问"标记并自增访问计数，
        # 避免对每条记忆单独发一次更新请求
        query: dict[str, Any] = {"memory_id": {"$in": memory_ids}}
        if user_id:
            query["user_id"] = user_id
        await self._collection.update_many(
            query,
            {
                "$set": {"accessed_at": utc_now()},
                "$inc": {"access_count": 1},
            },
        )

    async def _maybe_embed(self, text: str) -> Optional[list[float]]:
        # embedding 是可选能力，未配置时直接返回 None（上层据此退化为纯文本检索）
        if not self._embedding_fn:
            return None
        try:
            # _embedding_fn 默认当作同步函数丢线程池执行；但如果它本身返回的是一个
            # 协程对象（说明传入的其实是个异步函数），则再 await 一次取出真正结果，
            # 这样无论配置的是同步还是异步实现都能兼容
            result = await run_blocking_io(self._embedding_fn, text)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as e:
            # 嵌入失败不应该导致整个记忆功能不可用，记录警告后退化为无 embedding
            logger.warning(f"[NativeMemory] Embedding failed: {e}")
            return None

    # ------------------------------------------------------------------
    # MongoDB setup
    # ------------------------------------------------------------------

    def _ensure_collection(self) -> None:
        # 同步方法，调用方用 run_blocking_io 包裹执行；只是取一次 collection 引用，
        # Motor 的取库/取集合操作本身不发起网络请求，可以安全同步调用
        client = get_mongo_client()
        db = client[settings.MONGODB_DB]
        self._collection = db[COLLECTION_NAME]

    async def _create_indexes(self) -> None:
        # create_index 是阻塞的网络调用（pymongo 同步驱动），
        # 通过 delegate 拿到底层同步 collection 后丢线程池执行
        sync_col = get_mongo_client().delegate[settings.MONGODB_DB][COLLECTION_NAME]
        await run_blocking_io(self._create_indexes_sync, sync_col)

    @staticmethod
    def _create_indexes_sync(col: Any) -> None:
        # 复合索引：按用户+类型过滤、按创建时间倒序，支撑"某用户某类型最新记忆"类查询
        col.create_index(
            [("user_id", 1), ("memory_type", 1), ("created_at", -1)],
            name="native_mem_user_type_idx",
        )
        # memory_id 是全局唯一的业务主键（多处以它作为跨集合引用），用唯一索引强约束
        col.create_index(
            [("memory_id", 1)],
            name="native_mem_id_idx",
            unique=True,
        )
        # 复合索引：支撑 indexing.py 里"按更新时间+访问次数"挑选索引展示条目的查询
        col.create_index(
            [("user_id", 1), ("updated_at", -1), ("access_count", -1)],
            name="native_mem_recency_idx",
        )
        try:
            # 全文索引：content 权重最高，其次 summary，再次 tags，
            # 供 search.py 的关键词检索路径使用 MongoDB 原生 $text 查询
            col.create_index(
                [
                    ("user_id", 1),
                    ("content", "text"),
                    ("summary", "text"),
                    ("tags", "text"),
                ],
                name="native_mem_text_idx",
                weights={"content": 10, "summary": 5, "tags": 2},
            )
        except Exception as e:
            # Text index creation can fail on existing collections with conflicts
            # MongoDB 一个 collection 只允许存在一个全文索引，若历史上已建过定义
            # 不同的全文索引会在这里冲突失败；不影响其它索引和主要功能，仅告警
            logger.warning(f"[NativeMemory] Text index creation skipped: {e}")
        try:
            # 复合索引：支撑按 context（会话/场景）分组查询该用户的记忆
            col.create_index(
                [("user_id", 1), ("context", 1)],
                name="native_mem_session_ctx_idx",
            )
        except Exception as e:
            logger.warning(f"[NativeMemory] Session context index creation skipped: {e}")

    def _setup_embedding_fn(self) -> None:
        """Set up optional embedding function from config."""
        # embedding 完全是可选增强：未配置 API base/key 时，后端仍可正常工作，
        # 只是检索退化为纯文本匹配（text-only mode），不影响核心记忆功能可用性
        api_base = getattr(settings, "NATIVE_MEMORY_EMBEDDING_API_BASE", "")
        api_key = getattr(settings, "NATIVE_MEMORY_EMBEDDING_API_KEY", "")
        model = getattr(settings, "NATIVE_MEMORY_EMBEDDING_MODEL", "text-embedding-3-small")

        if not api_base or not api_key:
            logger.debug("[NativeMemory] No embedding API configured, text-only mode")
            return

        try:
            import httpx

            # 复用同一个 AsyncClient（保持连接池），引用存在 self._httpx_client 上
            # 是为了在 close() 时能正确 aclose()，避免连接泄漏
            client = httpx.AsyncClient(
                base_url=api_base.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(30.0),
            )

            async def embed_fn(text: str) -> list[float]:
                # 走 OpenAI 兼容的 /v1/embeddings 接口，只取第一条结果的向量
                resp = await client.post(
                    "/v1/embeddings",
                    json={"input": text, "model": model},
                )
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]

            self._embedding_fn = embed_fn
            self._httpx_client = client
            logger.info(f"[NativeMemory] Embedding enabled: {api_base} ({model})")
        except ImportError:
            # httpx 属于可选依赖，未安装时优雅降级而不是让初始化整体失败
            logger.warning("[NativeMemory] httpx not available, embedding disabled")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

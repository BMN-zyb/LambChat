"""Content storage helpers for the native memory backend."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 记忆内容有大有小：大多数记忆很短，直接整段存进 MongoDB 文档（inline）即可；
# 但少数记忆内容很长（超过 NATIVE_MEMORY_INLINE_CONTENT_MAX_CHARS 配置的阈值），
# 若也整段塞进文档，会拖慢所有"只需要摘要/预览"的查询（如索引、列表展示）。
# 因此本模块实现了一套双模式存储策略：
#   - inline 模式：全文直接存在文档的 content 字段里；
#   - store 模式：文档 content 字段只存一个截断预览（inline_preview），
#     全文改存到外部的 key-value 存储（BaseStore 协议，通常后端是 MongoDB
#     的另一个 collection），文档里留一个 content_store_key 指针。
# get_store/store_put/store_get/store_delete 这组函数做了双重兼容：既兼容
# 新版异步原生接口（aput/aget/adelete），也兼容旧版同步接口（put/get/delete，
# 用 maybe_await 包一层）。hydrate_* 函数负责在需要完整内容时按指针取回全文。
# ============================================================================

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from src.kernel.config import settings


async def maybe_await(value: Any) -> Any:
    # 统一处理"可能是协程也可能是普通值"的返回结果：是 awaitable 就 await 拿到最终值，
    # 否则原样返回，方便调用方无需关心底层实现是同步还是异步
    if isinstance(value, Awaitable):
        return await value
    return value


def memory_store_namespace(user_id: str) -> tuple[str, ...]:
    # 构造外部内容存储用的命名空间：(namespace 前缀, user_id, "content")，
    # 天然按用户隔离，避免不同用户的记忆内容互相冲突或越权访问
    base = str(getattr(settings, "NATIVE_MEMORY_STORE_NAMESPACE", "memories") or "memories")
    return (base, user_id, "content")


async def get_store(backend) -> Any:
    # 懒加载并缓存到 backend._store 上，同一个 backend 实例的多次调用共享同一个 store
    if backend._store is None:
        from src.infra.storage.mongodb_store import acreate_store

        backend._store = await acreate_store()
    return backend._store


async def store_put(backend, namespace: tuple[str, ...], key: str, value: dict[str, Any]) -> None:
    # 优先用新版异步原生接口 aput；没有的话退回同步 put（用 maybe_await 兼容
    # 它可能本身就是协程函数的情况），两者都没有就静默放弃（store 不可用时不阻塞主流程）
    store = await get_store(backend)
    if store is None:
        return
    if hasattr(store, "aput"):
        await store.aput(namespace, key, value)
        return
    if hasattr(store, "put"):
        await maybe_await(store.put(namespace, key, value))


async def store_get(backend, namespace: tuple[str, ...], key: str) -> Any:
    # 与 store_put 相同的双接口兼容策略：优先 aget，其次 get，都没有则返回 None
    store = await get_store(backend)
    if store is None:
        return None
    if hasattr(store, "aget"):
        return await store.aget(namespace, key)
    if hasattr(store, "get"):
        return await maybe_await(store.get(namespace, key))
    return None


async def store_delete(backend, namespace: tuple[str, ...], key: str) -> None:
    store = await get_store(backend)
    if store is None:
        return
    if hasattr(store, "adelete"):
        await store.adelete(namespace, key)
        return
    if hasattr(store, "delete"):
        await maybe_await(store.delete(namespace, key))
        return
    # 没有任何形式的删除方法时，退化为把值写成 None 当作"删除"的替代表达
    # （某些精简版 store 实现只支持 put/aput，没有真正的 delete 语义）
    if hasattr(store, "aput"):
        await store.aput(namespace, key, None)
        return
    if hasattr(store, "put"):
        await maybe_await(store.put(namespace, key, None))
        return
    import logging

    # 彻底没有任何兼容方法可用：只记警告，不抛异常——内容清理属于辅助操作，
    # 失败不应该影响记忆删除/整合等主流程的完成
    logging.getLogger(__name__).warning(
        "[NativeMemory] store_delete: no compatible delete method found on store"
    )


async def delete_memory_content(backend, user_id: str, content_store_key: str | None) -> None:
    # 没有 store_key 说明这条记忆本来就是 inline 存储，没有外部内容需要清理
    if not content_store_key:
        return
    await store_delete(backend, memory_store_namespace(user_id), content_store_key)


def inline_preview(content: str) -> str:
    # 生成一段固定长度上限的预览文本：始终塞进文档的 content 字段，
    # 无论原文有多长，保证列表/索引类查询不需要读取完整内容也能展示概要
    max_chars = int(getattr(settings, "NATIVE_MEMORY_INLINE_CONTENT_MAX_CHARS", 1200))
    if len(content) <= max_chars:
        return content
    # 阈值太小时省略号反而占比过高甚至放不下，直接硬截断不加省略号
    if max_chars <= 3:
        return content[:max_chars]
    return content[: max_chars - 3].rstrip() + "..."


async def build_content_fields(
    backend, user_id: str, memory_id: str, content: str
) -> dict[str, Any]:
    # 决定一条记忆内容该走 inline 还是 store 模式的核心逻辑：
    # 未超过阈值就整段直接 inline 存储，文档自身即完整内容，无需额外存储引用
    preview = inline_preview(content)
    max_chars = int(getattr(settings, "NATIVE_MEMORY_INLINE_CONTENT_MAX_CHARS", 1200))
    if len(content) <= max_chars:
        return {
            "content": preview,
            "content_storage_mode": "inline",
            "content_store_key": None,
        }

    # 超过阈值：文档里只留截断预览，完整原文另存到外部 store，
    # key 用 memory_id 派生，保证同一条记忆的存储 key 稳定可追溯
    store_key = f"memory:{memory_id}"
    await store_put(
        backend,
        memory_store_namespace(user_id),
        store_key,
        {"text": content, "memory_id": memory_id},
    )
    return {
        "content": preview,
        "content_storage_mode": "store",
        "content_store_key": store_key,
    }


async def hydrate_memory_text(backend, doc: dict[str, Any]) -> str:
    # inline 模式下文档的 content 字段本就是完整内容，直接返回即可
    if doc.get("content_storage_mode") != "store" or not doc.get("content_store_key"):
        return str(doc.get("content", ""))

    # store 模式：按存储的 key 去外部 store 里取回完整内容
    item = await store_get(
        backend,
        memory_store_namespace(doc["user_id"]),
        doc["content_store_key"],
    )
    if item is None:
        # 取不到（可能已被清理或 store 暂时不可用）时退回文档里的截断预览兜底，
        # 保证调用方始终能拿到"至少是预览"的可用文本，而不是抛异常或返回空
        return str(doc.get("content", ""))
    # 不同 store 实现返回的可能是原始 dict，也可能是带 .value 属性的包装对象，两者都兼容
    value = getattr(item, "value", item)
    if isinstance(value, dict):
        return str(value.get("text") or doc.get("content", ""))
    return str(doc.get("content", ""))


async def hydrate_formatted_memory(backend, memory: dict[str, Any]) -> dict[str, Any]:
    # 这里的 memory 是"已格式化"的检索结果（字段名是 text/storage_mode，
    # 而不是原始文档的 content/content_storage_mode），供 recall 结果需要
    # 展开完整内容时调用
    if memory.get("storage_mode") != "store":
        # 本来就是 inline，内容已经是完整的，只是补齐 preview/storage_mode 默认值以统一返回结构
        memory.setdefault("preview", memory.get("text", ""))
        memory.setdefault("storage_mode", "inline")
        return memory

    # 把"已格式化"的字段名转换回 hydrate_memory_text 期望的原始文档字段名，
    # 借此复用同一套取全文逻辑，不需要再写一遍
    doc = {
        "user_id": memory.get("user_id"),
        "content": memory.get("text", ""),
        "content_storage_mode": memory.get("storage_mode"),
        "content_store_key": memory.get("content_store_key"),
    }
    full_text = await hydrate_memory_text(backend, doc)
    # 原来的 text（截断预览）挪到 preview 字段保留，text 字段替换为取回的完整内容
    memory["preview"] = memory.get("text", "")
    memory["text"] = full_text
    return memory

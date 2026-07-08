"""
Memory API router - list and manage stored memories
"""

import json
import re
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.api.deps import get_current_user_required
from src.infra.async_utils.blocking import run_blocking_io
from src.infra.memory.client.types import MemoryType
from src.infra.utils.datetime import parse_iso, to_iso, utc_now, utc_now_iso
from src.kernel.config import settings
from src.kernel.schemas.user import TokenPayload

router = APIRouter()

# 本模块挂载于 /api/memory，负责用户长期记忆的增删改查、检索、批量删除以及导入/导出。
# 所有接口都需登录（get_current_user_required），且严格按 user_id 隔离，仅能操作当前用户自己的记忆。
# 后端存储通过 _get_backend() 复用记忆工具里的单例（MongoDB 集合 + 可选向量嵌入）。
# 合法的记忆类型集合（由 MemoryType 枚举生成），用于校验入参
_VALID_MEMORY_TYPES = {mt.value for mt in MemoryType}
# 合法的记忆来源集合：manual 手动创建 / auto_retained 自动留存 / imported 导入 / consolidated 归并
_VALID_SOURCES = {"manual", "auto_retained", "imported", "consolidated"}
# 导出文件的格式版本号
_MEMORY_EXPORT_VERSION = 1
# 导入时单条记忆 content 的默认最大字符数
_DEFAULT_MEMORY_IMPORT_CONTENT_MAX_CHARS = 64_000
# 导入时所有记忆 content 累计的默认最大字符数（防止一次导入体量过大）
_DEFAULT_MEMORY_IMPORT_TOTAL_CONTENT_MAX_CHARS = 2_000_000
# 导出时单条记忆 content 的默认最大字符数（超出会被截断并标记）
_DEFAULT_MEMORY_EXPORT_CONTENT_MAX_CHARS = 64_000


# 复用记忆工具模块里的单例后端（MongoDB 集合 + 可选向量嵌入），避免重复建连。
async def _get_backend():
    """Reuse the singleton memory backend from memory tools."""
    from src.infra.memory.tools import _get_backend

    return await _get_backend()


# 清洗标签：非 list 直接返回空；逐个 strip 并截断到 40 字符，最多保留 20 个，丢弃空串。
def _clean_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for tag in value:
        text = str(tag).strip()
        if text:
            tags.append(text[:40])
        if len(tags) >= 20:
            break
    return tags


# 清洗文本：转字符串并去掉首尾空白；给了 max_length 时截断到该长度。
def _clean_text(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    if max_length is not None:
        return text[:max_length]
    return text


# 读取"导入时单条 content 上限"配置（NATIVE_MEMORY_IMPORT_CONTENT_MAX_CHARS），无效则回退默认值。
def _get_memory_import_content_max_chars() -> int:
    value = getattr(
        settings,
        "NATIVE_MEMORY_IMPORT_CONTENT_MAX_CHARS",
        _DEFAULT_MEMORY_IMPORT_CONTENT_MAX_CHARS,
    )
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return _DEFAULT_MEMORY_IMPORT_CONTENT_MAX_CHARS


# 读取"导入时全部 content 累计上限"配置，无效则回退默认值。
def _get_memory_import_total_content_max_chars() -> int:
    value = getattr(
        settings,
        "NATIVE_MEMORY_IMPORT_TOTAL_CONTENT_MAX_CHARS",
        _DEFAULT_MEMORY_IMPORT_TOTAL_CONTENT_MAX_CHARS,
    )
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return _DEFAULT_MEMORY_IMPORT_TOTAL_CONTENT_MAX_CHARS


# 读取"导出时单条 content 上限"配置，无效则回退默认值。
def _get_memory_export_content_max_chars() -> int:
    value = getattr(
        settings,
        "NATIVE_MEMORY_EXPORT_CONTENT_MAX_CHARS",
        _DEFAULT_MEMORY_EXPORT_CONTENT_MAX_CHARS,
    )
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return _DEFAULT_MEMORY_EXPORT_CONTENT_MAX_CHARS


# 校验单条记忆 content 的大小，超过导入单条上限抛 400。
def _validate_memory_content_size(content: str) -> None:
    max_chars = _get_memory_import_content_max_chars()
    if len(content) > max_chars:
        raise HTTPException(
            status_code=400,
            detail=f"Memory content too large (max {max_chars} characters)",
        )


# 校验一次导入的所有记忆 content 累计大小，超过总上限抛 400（边累加边判断，尽早失败）。
def _validate_memory_import_total_content_size(raw_memories: list[Any]) -> None:
    max_chars = _get_memory_import_total_content_max_chars()
    total_chars = 0
    for raw in raw_memories:
        if not isinstance(raw, dict):
            continue
        total_chars += len(str(raw.get("content") or "").strip())
        if total_chars > max_chars:
            raise HTTPException(
                status_code=400,
                detail=f"Memory import content too large (max {max_chars} total characters)",
            )


# MongoDB 查询投影：只取导出所需字段，减少读取与传输开销。
def _memory_projection() -> dict[str, int]:
    return {
        "memory_id": 1,
        "user_id": 1,
        "title": 1,
        "summary": 1,
        "memory_type": 1,
        "tags": 1,
        "content": 1,
        "content_storage_mode": 1,
        "content_store_key": 1,
        "context": 1,
        "source": 1,
        "created_at": 1,
        "updated_at": 1,
        "accessed_at": 1,
        "access_count": 1,
    }


# 把 JSONEncoder 的增量编码结果收集成 list；配合 run_blocking_io 放到线程池执行，避免阻塞事件循环。
def _json_iterencode_chunks(encoder: json.JSONEncoder, value: Any) -> list[str]:
    return list(encoder.iterencode(value))


# GET /api/memory/ —— 分页列出当前用户的记忆（列表视图，正文可能非全量）。
# 权限：需登录，仅返回本人（user_id）记忆。查询参数：
#   memory_type 按类型过滤；search 在 title/summary/tags 上做不区分大小写正则匹配；
#   limit（1~200，默认 50）、offset 分页。返回 {memories, total}。
@router.get("/")
async def list_memories(
    memory_type: Optional[str] = Query(None, description="Filter by memory type"),
    search: Optional[str] = Query(None, description="Search query (matches title, summary, tags)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: TokenPayload = Depends(get_current_user_required),
):
    """List stored memories for the current user."""
    # 后端不可用时返回空列表（而非报错），保证前端可优雅降级
    backend = await _get_backend()
    if not backend:
        return {"memories": [], "total": 0}

    if memory_type and memory_type not in _VALID_MEMORY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid memory_type. Must be one of: {', '.join(sorted(_VALID_MEMORY_TYPES))}",
        )

    # 查询强制带上 user_id，确保只查当前用户的记忆
    query_filter: dict = {"user_id": user.sub}
    if memory_type:
        query_filter["memory_type"] = memory_type

    # 搜索：转义后做不区分大小写的正则，在标题/摘要/标签任一命中即可
    if search:
        search_regex = {"$regex": re.escape(search), "$options": "i"}
        query_filter["$or"] = [
            {"title": search_regex},
            {"summary": search_regex},
            {"tags": search_regex},
        ]

    collection = backend._collection
    total = await collection.count_documents(query_filter)

    cursor = (
        collection.find(
            query_filter,
            {
                "memory_id": 1,
                "title": 1,
                "summary": 1,
                "memory_type": 1,
                "tags": 1,
                "content": 1,
                "content_storage_mode": 1,
                "content_store_key": 1,
                "source": 1,
                "created_at": 1,
                "updated_at": 1,
                "access_count": 1,
            },
        )
        .sort("updated_at", -1)
        .skip(offset)
        .limit(limit)
    )

    memories = []
    async for doc in cursor:
        # 组装列表项；has_full_content 表示正文是否被外存（store 模式），前端据此决定是否再拉详情
        memory = {
            "memory_id": doc["memory_id"],
            "title": doc.get("title", ""),
            "summary": doc.get("summary", ""),
            "memory_type": doc.get("memory_type", ""),
            "tags": doc.get("tags", []),
            "content": doc.get("content", ""),
            "source": doc.get("source", ""),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
            "access_count": doc.get("access_count", 0),
            "has_full_content": doc.get("content_storage_mode") == "store",
        }
        memories.append(memory)

    return {"memories": memories, "total": total}


# POST /api/memory/ —— 新建一条记忆。权限：需登录，归属当前用户。
# 请求体（JSON dict）字段：content（正文，至少 5 字符，必填）、title、summary、
#   memory_type（默认 user，须合法）、context、tags。source 固定标记为 "manual"。
# 副作用：可能生成向量嵌入、把超长正文外存，并使该用户的记忆缓存失效。
@router.post("/")
async def create_memory(
    payload: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(get_current_user_required),
):
    """Create a new memory."""
    from src.infra.memory.client.native.content import build_content_fields
    from src.infra.memory.client.native.summaries import build_index_label

    backend = await _get_backend()
    if not backend:
        raise HTTPException(status_code=404, detail="Memory backend not available")

    # 记忆类型缺省为 user，且必须落在合法集合内
    memory_type = _clean_text(payload.get("memory_type") or MemoryType.USER.value)
    if memory_type not in _VALID_MEMORY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid memory_type. Must be one of: {', '.join(sorted(_VALID_MEMORY_TYPES))}",
        )

    # 正文至少 5 个字符，并校验不超过单条上限
    content = _clean_text(payload.get("content"))
    if len(content) < 5:
        raise HTTPException(status_code=400, detail="Memory content must be at least 5 characters")
    _validate_memory_content_size(content)

    memory_id = uuid.uuid4().hex
    title = _clean_text(payload.get("title") or "New memory", 80)
    summary = _clean_text(payload.get("summary") or content, 300)
    tags = _clean_tags(payload.get("tags"))
    now = utc_now()

    # 根据长度决定正文内联存储还是外存（返回 content / content_storage_mode / content_store_key 等）
    content_fields = await build_content_fields(backend, user.sub, memory_id, content)
    # 若后端支持向量化，则为正文生成嵌入，供后续语义检索
    embedding = None
    if hasattr(backend, "_maybe_embed"):
        embedding = await backend._maybe_embed(content)

    # index_label 由标题/摘要/正文拼成用于关键词检索；**content_fields 展开正文相关存储字段
    doc = {
        "memory_id": memory_id,
        "user_id": user.sub,
        "title": title,
        "summary": summary,
        "index_label": build_index_label(title, summary, content),
        "memory_type": memory_type,
        "context": _clean_text(payload.get("context")),
        "tags": tags,
        "source": "manual",
        "embedding": embedding,
        "created_at": now,
        "updated_at": now,
        "accessed_at": now,
        "access_count": 0,
        **content_fields,
    }

    await backend._collection.insert_one(doc)
    # 写入后使该用户的记忆缓存失效，保证后续读取拿到最新数据
    if hasattr(backend, "_invalidate_cache"):
        await backend._invalidate_cache(user.sub)

    return {
        "success": True,
        "memory_id": memory_id,
        "title": title,
        "summary": summary,
        "memory_type": memory_type,
        "tags": tags,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


# PUT /api/memory/{memory_id} —— 局部更新一条记忆（仅更新 payload 中出现的字段）。
# 权限：需登录且记忆归属当前用户，否则 404。可更新 title/memory_type/tags/source/content/summary。
# 更新 content 时会重算外存字段与向量嵌入；若外存 key 变化，会异步清理旧的外存内容。
@router.put("/{memory_id}")
async def update_memory(
    memory_id: str,
    payload: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(get_current_user_required),
):
    """Update an existing memory."""
    from src.infra.memory.client.native.content import (
        build_content_fields,
        delete_memory_content,
    )
    from src.infra.memory.client.native.summaries import build_index_label

    backend = await _get_backend()
    if not backend:
        raise HTTPException(status_code=404, detail="Memory backend not available")

    # 先确认记忆存在且属于当前用户（顺带取出旧的外存信息，便于后面清理）
    existing = await backend._collection.find_one(
        {"user_id": user.sub, "memory_id": memory_id},
        {"content_storage_mode": 1, "content_store_key": 1},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Memory not found")

    update: dict[str, Any] = {"updated_at": utc_now()}

    if "title" in payload:
        update["title"] = _clean_text(payload["title"], 80)
    if "memory_type" in payload:
        mt = _clean_text(payload["memory_type"])
        if mt not in _VALID_MEMORY_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid memory_type. Must be one of: {', '.join(sorted(_VALID_MEMORY_TYPES))}",
            )
        update["memory_type"] = mt
    if "tags" in payload:
        update["tags"] = _clean_tags(payload["tags"])
    if "source" in payload:
        src = _clean_text(payload["source"])
        if src not in _VALID_SOURCES:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid source. Must be one of: {', '.join(sorted(_VALID_SOURCES))}",
            )
        update["source"] = src

    # 仅当请求带了 content 才更新正文：重新清洗、校验，并重算外存字段与嵌入
    content = payload.get("content")
    if content is not None:
        content = _clean_text(content)
        if len(content) < 5:
            raise HTTPException(
                status_code=400, detail="Memory content must be at least 5 characters"
            )
        _validate_memory_content_size(content)

        content_fields = await build_content_fields(backend, user.sub, memory_id, content)
        update["content"] = content_fields.get("content", content)
        update["content_storage_mode"] = content_fields["content_storage_mode"]
        if "content_store_key" in content_fields:
            update["content_store_key"] = content_fields["content_store_key"]

        if hasattr(backend, "_maybe_embed"):
            embedding = await backend._maybe_embed(content)
            if embedding is not None:
                update["embedding"] = embedding

    if "summary" in payload:
        update["summary"] = _clean_text(payload["summary"], 300)
    elif content is not None:
        update["summary"] = _clean_text(content, 300)

    title = update.get("title") or existing.get("title", "")
    summary = update.get("summary") or existing.get("summary", "")
    full_content = content if content is not None else existing.get("content", "")
    update["index_label"] = build_index_label(title, summary, full_content)

    await backend._collection.update_one(
        {"user_id": user.sub, "memory_id": memory_id},
        {"$set": update},
    )

    # 正文外存 key 发生变化时，删除旧的外存内容，避免残留孤儿数据
    old_key = (
        existing.get("content_store_key")
        if existing.get("content_storage_mode") == "store"
        else None
    )
    new_key = update.get("content_store_key")
    if old_key and new_key and old_key != new_key:
        await delete_memory_content(backend, user.sub, old_key)

    if hasattr(backend, "_invalidate_cache"):
        await backend._invalidate_cache(user.sub)

    return {"success": True, "memory_id": memory_id}


# GET /api/memory/export —— 流式导出当前用户的全部记忆为 JSON 文件（作为附件下载）。
# 权限：需登录。用 StreamingResponse 边查边写，避免一次性把所有记忆读入内存。
# 注意：本路由声明在 /{memory_id} 之前，以免 "export" 被当作 memory_id 匹配。
# 单条正文超过导出上限会被截断，并附带 content_truncated / content_original_chars 标记。
@router.get("/export")
async def export_memories(
    user: TokenPayload = Depends(get_current_user_required),
):
    """Export all stored memories for the current user."""
    from src.infra.memory.client.native.content import hydrate_memory_text

    backend = await _get_backend()
    if not backend:
        raise HTTPException(status_code=404, detail="Memory backend not available")

    cursor = backend._collection.find({"user_id": user.sub}, _memory_projection())
    if hasattr(cursor, "sort"):
        cursor = cursor.sort("updated_at", -1)

    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
    exported_at = utc_now_iso()

    # 生成器：手工拼出 JSON —— 先写 version/exported_at 头，再逐条追加 memories 数组元素
    async def stream_export():
        yield f'{{"version":{_MEMORY_EXPORT_VERSION},"exported_at":'
        for chunk in encoder.iterencode(exported_at):
            yield chunk
        yield ',"memories":['

        first = True
        async for doc in cursor:
            # 还原完整正文（可能来自外存），并按导出上限截断（截断时下面会打标记）
            content = await hydrate_memory_text(backend, doc)
            content_original_chars = len(content)
            max_content_chars = _get_memory_export_content_max_chars()
            content_truncated = content_original_chars > max_content_chars
            if content_truncated:
                content = content[:max_content_chars]
            item = {
                "memory_id": doc["memory_id"],
                "title": doc.get("title", ""),
                "summary": doc.get("summary", ""),
                "memory_type": doc.get("memory_type", ""),
                "tags": doc.get("tags", []),
                "content": content,
                "context": doc.get("context", ""),
                "source": doc.get("source", ""),
                "created_at": to_iso(doc.get("created_at")),
                "updated_at": to_iso(doc.get("updated_at")),
                "accessed_at": to_iso(doc.get("accessed_at")),
                "access_count": doc.get("access_count", 0),
            }
            if content_truncated:
                item["content_truncated"] = True
                item["content_original_chars"] = content_original_chars
            if first:
                first = False
            else:
                yield ","
            for chunk in await run_blocking_io(_json_iterencode_chunks, encoder, item):
                yield chunk

        yield "]}"

    return StreamingResponse(
        stream_export(),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="lambchat-memory-export.json"'},
    )


# POST /api/memory/import —— 批量导入记忆，按 memory_id 做 upsert（存在则覆盖，不存在则新建）。
# 权限：需登录，导入到当前用户名下。限制：一次最多 1000 条，且所有正文累计不超过总上限。
# 覆盖时若外存 key 变化会清理旧外存内容。返回 imported/created/overwritten 计数。
@router.post("/import")
async def import_memories(
    payload: dict[str, Any] = Body(...),
    user: TokenPayload = Depends(get_current_user_required),
):
    """Import memories for the current user, overwriting matching memory IDs."""
    from src.infra.memory.client.native.content import build_content_fields, delete_memory_content
    from src.infra.memory.client.native.summaries import build_index_label

    backend = await _get_backend()
    if not backend:
        raise HTTPException(status_code=404, detail="Memory backend not available")

    # 入参必须是 memories 列表，条数与累计内容大小都要在限制内
    raw_memories = payload.get("memories")
    if not isinstance(raw_memories, list):
        raise HTTPException(status_code=400, detail="memories must be a list")
    if len(raw_memories) > 1000:
        raise HTTPException(status_code=400, detail="Cannot import more than 1000 memories at once")
    _validate_memory_import_total_content_size(raw_memories)

    created = 0
    overwritten = 0
    now = utc_now()

    for raw in raw_memories:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="Each memory must be an object")

        memory_type = _clean_text(raw.get("memory_type") or MemoryType.USER.value)
        if memory_type not in _VALID_MEMORY_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid memory_type. Must be one of: {', '.join(sorted(_VALID_MEMORY_TYPES))}",
            )

        content = _clean_text(raw.get("content"))
        if len(content) < 5:
            raise HTTPException(
                status_code=400, detail="Memory content must be at least 5 characters"
            )
        _validate_memory_content_size(content)

        memory_id = _clean_text(raw.get("memory_id")) or uuid.uuid4().hex
        title = _clean_text(raw.get("title") or "Imported memory", 80)
        summary = _clean_text(raw.get("summary") or content, 300)
        tags = _clean_tags(raw.get("tags"))

        existing_doc = await backend._collection.find_one(
            {"user_id": user.sub, "memory_id": memory_id},
            {"content_storage_mode": 1, "content_store_key": 1},
        )

        content_fields = await build_content_fields(backend, user.sub, memory_id, content)
        embedding = None
        if hasattr(backend, "_maybe_embed"):
            embedding = await backend._maybe_embed(content)

        # 时间戳优先用导入数据里的 ISO 字符串，缺失/非法则用当前时间兜底
        created_at = (
            parse_iso(raw["created_at"])
            if isinstance(raw.get("created_at"), str) and raw["created_at"].strip()
            else now
        )
        updated_at = (
            parse_iso(raw["updated_at"])
            if isinstance(raw.get("updated_at"), str) and raw["updated_at"].strip()
            else now
        )
        accessed_at = (
            parse_iso(raw["accessed_at"])
            if isinstance(raw.get("accessed_at"), str) and raw["accessed_at"].strip()
            else updated_at
        )

        try:
            access_count = max(0, int(raw.get("access_count") or 0))
        except (TypeError, ValueError):
            access_count = 0

        doc = {
            "memory_id": memory_id,
            "user_id": user.sub,
            "title": title,
            "summary": summary,
            "index_label": build_index_label(title, summary, content),
            "memory_type": memory_type,
            "context": _clean_text(raw.get("context")),
            "tags": tags,
            "source": _clean_text(raw.get("source") or "imported"),
            "embedding": embedding,
            "created_at": created_at,
            "updated_at": updated_at,
            "accessed_at": accessed_at,
            "access_count": access_count,
            **content_fields,
        }

        # upsert 覆盖写：matched_count>0 说明命中已有记录（记为覆盖），否则记为新建
        result = await backend._collection.replace_one(
            {"user_id": user.sub, "memory_id": memory_id},
            doc,
            upsert=True,
        )
        if getattr(result, "matched_count", 0):
            overwritten += 1
        else:
            created += 1

        old_key = existing_doc.get("content_store_key") if existing_doc else None
        new_key = content_fields.get("content_store_key")
        if (
            existing_doc
            and existing_doc.get("content_storage_mode") == "store"
            and old_key
            and old_key != new_key
        ):
            await delete_memory_content(backend, user.sub, old_key)

    imported = created + overwritten
    if imported and hasattr(backend, "_invalidate_cache"):
        await backend._invalidate_cache(user.sub)

    return {
        "success": True,
        "imported": imported,
        "created": created,
        "overwritten": overwritten,
    }


# GET /api/memory/{memory_id} —— 获取单条记忆的完整信息（含 hydrate 后的全量正文）。
# 权限：需登录且记忆归属当前用户，否则 404。与列表接口不同，这里会还原被外存的正文。
@router.get("/{memory_id}")
async def get_memory(
    memory_id: str,
    user: TokenPayload = Depends(get_current_user_required),
):
    """Get full content of a specific memory."""
    from src.infra.memory.client.native.content import hydrate_memory_text

    backend = await _get_backend()
    if not backend:
        raise HTTPException(status_code=404, detail="Memory backend not available")

    doc = await backend._collection.find_one(
        {"user_id": user.sub, "memory_id": memory_id},
        {
            "memory_id": 1,
            "title": 1,
            "summary": 1,
            "memory_type": 1,
            "tags": 1,
            "content": 1,
            "content_storage_mode": 1,
            "content_store_key": 1,
            "context": 1,
            "source": 1,
            "created_at": 1,
            "updated_at": 1,
            "access_count": 1,
        },
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Memory not found")

    # 还原完整正文（若为外存模式则从外部拉取），列表接口不做这一步
    full_content = await hydrate_memory_text(backend, doc)

    return {
        "memory_id": doc["memory_id"],
        "title": doc.get("title", ""),
        "summary": doc.get("summary", ""),
        "memory_type": doc.get("memory_type", ""),
        "tags": doc.get("tags", []),
        "content": full_content,
        "context": doc.get("context", ""),
        "source": doc.get("source", ""),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "access_count": doc.get("access_count", 0),
    }


# DELETE /api/memory/{memory_id} —— 删除单条记忆。权限：需登录且归属当前用户，找不到则 404。
@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    user: TokenPayload = Depends(get_current_user_required),
):
    """Delete a specific memory."""
    backend = await _get_backend()
    if not backend:
        raise HTTPException(status_code=404, detail="Memory backend not available")

    result = await backend.delete(user.sub, memory_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail="Memory not found")

    return result


# POST /api/memory/batch-delete —— 批量删除记忆。权限：需登录，仅删当前用户的记忆。
# 请求体 {"memory_ids": [...]}，必须为非空列表且不超过 100 条；返回实际删除数量。
@router.post("/batch-delete")
async def batch_delete_memories(
    request: Request,
    user: TokenPayload = Depends(get_current_user_required),
):
    """Delete multiple memories at once."""
    # 从原始请求体解析 memory_ids，要求为非空列表
    body = await request.json()
    memory_ids = body.get("memory_ids", [])
    if not memory_ids or not isinstance(memory_ids, list):
        raise HTTPException(status_code=400, detail="memory_ids must be a non-empty list")

    if len(memory_ids) > 100:
        raise HTTPException(status_code=400, detail="Cannot delete more than 100 memories at once")

    backend = await _get_backend()
    if not backend:
        raise HTTPException(status_code=404, detail="Memory backend not available")

    deleted = 0
    for mid in memory_ids:
        result = await backend.delete(user.sub, mid)
        if result.get("success"):
            deleted += 1

    return {"success": True, "deleted": deleted}

# src/api/routes/revealed_file.py
"""API routes for the revealed file library."""

# “产出文件库”（revealed file）路由模块，挂载于 /api/files：
# 管理 agent 运行过程中“暴露/产出”的文件的元数据，供前端浏览、检索、按会话分组、
# 查看统计与切换收藏。所有接口均要求登录，且只操作当前用户（user.sub）自己的数据。
# 说明：本模块只读写文件元数据记录，文件内容本身仍走 /api/upload 相关接口。
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_current_user_required
from src.infra.revealed_file.storage import get_revealed_file_storage
from src.kernel.schemas.user import TokenPayload

# 本模块路由挂载于 /api/files 前缀下
router = APIRouter()


# GET /revealed：分页列出当前用户的产出文件，支持按类型/会话/项目筛选、关键词搜索、
# 仅看收藏与排序。返回 items 列表与 total 总数、分页信息。需要登录。
@router.get("/revealed")
async def list_revealed_files(
    # 页码，从 1 开始
    page: int = Query(1, ge=1),
    # 每页数量，取值 1..50
    page_size: int = Query(20, ge=1, le=50),
    # 按文件类型筛选（可选）
    file_type: Optional[str] = Query(None),
    # 按会话 ID 筛选（可选）
    session_id: Optional[str] = Query(None),
    # 按项目 ID 筛选（可选）
    project_id: Optional[str] = Query(None),
    # 按文件名等关键词搜索（可选）
    search: Optional[str] = Query(None),
    # 排序字段，默认按创建时间
    sort_by: str = Query("created_at"),
    # 排序方向，默认降序（desc）
    sort_order: str = Query("desc"),
    # 是否只返回已收藏的文件
    favorites_only: bool = Query(False),
    # 当前登录用户（依赖注入，未登录会被直接拒绝）
    user: TokenPayload = Depends(get_current_user_required),
):
    # 只查询当前用户（user.sub）自己的文件；分页由 skip/limit 换算
    storage = get_revealed_file_storage()
    result = await storage.list_files(
        user.sub,
        file_type=file_type,
        session_id=session_id,
        project_id=project_id,
        search=search,
        favorites_only=favorites_only,
        sort_by=sort_by,
        sort_order=sort_order,
        skip=(page - 1) * page_size,
        limit=page_size,
    )
    return {
        "items": result["items"],
        "total": result["total"],
        "page": page,
        "page_size": page_size,
    }


# GET /revealed/stats：返回当前用户产出文件的统计信息（如各类型数量等）。需要登录。
@router.get("/revealed/stats")
async def get_revealed_file_stats(
    user: TokenPayload = Depends(get_current_user_required),
):
    storage = get_revealed_file_storage()
    stats = await storage.get_stats(user.sub)
    return stats


# GET /revealed/sessions：列出当前用户曾产出过文件的会话列表，供按会话浏览。需要登录。
@router.get("/revealed/sessions")
async def list_revealed_file_sessions(
    user: TokenPayload = Depends(get_current_user_required),
):
    storage = get_revealed_file_storage()
    sessions = await storage.get_user_sessions(user.sub)
    return sessions


# GET /revealed/grouped：与 /revealed 类似，但结果按会话（session）分组返回。
# 分页作用于“会话”维度，返回 sessions 列表与 total_sessions 总数。需要登录。
@router.get("/revealed/grouped")
async def list_revealed_files_grouped(
    # 页码，从 1 开始（按会话分页）
    page: int = Query(1, ge=1),
    # 每页会话数量，取值 1..50
    page_size: int = Query(20, ge=1, le=50),
    # 按文件类型筛选（可选）
    file_type: Optional[str] = Query(None),
    # 按项目 ID 筛选（可选）
    project_id: Optional[str] = Query(None),
    # 按关键词搜索（可选）
    search: Optional[str] = Query(None),
    # 排序字段，默认按创建时间
    sort_by: str = Query("created_at"),
    # 排序方向，默认降序（desc）
    sort_order: str = Query("desc"),
    # 是否只返回已收藏文件
    favorites_only: bool = Query(False),
    # 当前登录用户（依赖注入）
    user: TokenPayload = Depends(get_current_user_required),
):
    storage = get_revealed_file_storage()
    result = await storage.list_files_grouped_by_session(
        user.sub,
        file_type=file_type,
        project_id=project_id,
        search=search,
        favorites_only=favorites_only,
        sort_by=sort_by,
        sort_order=sort_order,
        skip=(page - 1) * page_size,
        limit=page_size,
    )
    return {
        "sessions": result["sessions"],
        "total_sessions": result["total_sessions"],
        "page": page,
        "page_size": page_size,
    }


# PATCH /revealed/{file_id}/favorite：切换指定文件的收藏状态（在已收藏/未收藏间翻转），
# 返回切换后的最新状态。需要登录，且只能操作本人文件。
@router.patch("/revealed/{file_id}/favorite")
async def toggle_revealed_file_favorite(
    # 路径参数：目标文件的 ID（MongoDB ObjectId 的字符串形式）
    file_id: str,
    user: TokenPayload = Depends(get_current_user_required),
):
    storage = get_revealed_file_storage()
    try:
        new_val = await storage.toggle_favorite(user.sub, file_id)
    except ValueError as e:
        # 文件不存在（或不属于该用户）→ 404
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # Catch InvalidId and other BSON errors for malformed file_id
        # file_id 格式非法（无法解析为 ObjectId 等 BSON 错误）→ 400
        if "InvalidId" in type(e).__name__ or "bson" in type(e).__module__:
            raise HTTPException(status_code=400, detail="Invalid file ID format")
        raise
    return {"is_favorite": new_val}

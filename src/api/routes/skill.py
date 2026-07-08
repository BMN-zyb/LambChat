"""
用户 Skills API

提供用户 Skills 的 CRUD、Toggle 和发布到商店操作。
Simplified architecture: files + metadata (stored in __meta__ doc), enabled/disabled in user metadata.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from src.api.deps import require_permissions
from src.api.routes import skill_uploads
from src.api.routes.upload import _read_upload_file_limited
from src.infra.async_utils import run_blocking_io
from src.infra.skill.binary import guess_mime_type, parse_binary_ref_async
from src.infra.skill.marketplace import MarketplaceStorage
from src.infra.skill.storage import SkillStorage, normalize_skill_name_list
from src.infra.skill.types import (
    InstalledFrom,
    MarketplaceSkillCreate,
    MarketplaceSkillResponse,
    MarketplaceSkillUpdate,
    PublishToMarketplaceRequest,
    UserSkill,
    UserSkillListResponse,
    UserSkillPreferenceResponse,
    UserSkillPreferenceUpdate,
)
from src.infra.user.storage import UserStorage
from src.kernel.config import settings  # noqa: F401 - compatibility for route tests/patching
from src.kernel.schemas.user import TokenPayload

# 技能管理路由（挂载于 /api/skills）：技能的增删改查、启停、文件读写、
# ZIP 批量上传、批量操作，以及发布到技能商店（marketplace）
router = APIRouter()
# ZIP 内单个成员解压后的字节上限（None 表示沿用 skill_uploads 的默认限制）
_ZIP_MEMBER_MAX_BYTES: int | None = None
# ZIP 内允许的成员文件数量上限，防止 zip bomb
_ZIP_MAX_MEMBERS = 500
# 批量操作（删除/切换）单次允许的最大技能名数量
SKILL_BATCH_OPERATION_MAX_NAMES = 100
# 缓存 skill_uploads 模块的上传大小获取函数，便于本模块包装与测试打桩
_skill_uploads_get_skill_upload_max_size = skill_uploads._get_skill_upload_max_size


# 返回技能上传的大小限制 (字节上限, MB 上限)，委托给 skill_uploads 实现
def _get_skill_upload_max_size() -> tuple[int, int]:
    return _skill_uploads_get_skill_upload_max_size()


# 将本模块配置的 ZIP 解析限制同步到 skill_uploads 模块，
# 确保解析 ZIP 时使用一致的大小/数量约束（也方便测试时统一打桩）
def _sync_zip_upload_limits() -> None:
    skill_uploads._ZIP_MEMBER_MAX_BYTES = _ZIP_MEMBER_MAX_BYTES
    skill_uploads._ZIP_MAX_MEMBERS = _ZIP_MAX_MEMBERS
    skill_uploads._get_skill_upload_max_size = _get_skill_upload_max_size


# 预解析 ZIP 中的技能列表（仅预览，不落库）：同步限制后委托 skill_uploads 解析
def _parse_zip_skill_preview(zip_content: bytes) -> list[dict]:
    _sync_zip_upload_limits()
    return skill_uploads._parse_zip_skill_preview(zip_content)


# 完整解析 ZIP 中的技能：返回 [(技能名, {文本文件路径: 内容}, {二进制文件路径: 字节})]
def _parse_zip_skills(
    zip_content: bytes,
) -> list[tuple[str, dict[str, str], dict[str, bytes]]]:
    _sync_zip_upload_limits()
    return skill_uploads._parse_zip_skills(zip_content)


# 依赖注入：提供用户技能存储实例
def get_storage() -> SkillStorage:
    return SkillStorage()


# 依赖注入：提供技能商店（marketplace）存储实例
def get_marketplace_storage() -> MarketplaceStorage:
    return MarketplaceStorage()


def sanitize_file_path(path: str) -> str:
    """Sanitize file path to prevent path traversal."""
    # 统一分隔符为 /，剔除空段与 ".."，防止路径穿越（如 ../../etc/passwd）
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != ".."]
    return "/".join(parts)


# 统计列表中唯一的、有效（非空字符串）技能名的数量
def _count_unique_skill_names(values: list[str]) -> int:
    seen: set[str] = set()
    for value in values:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
    return len(seen)


# 批量操作保护：唯一技能名数量超过上限时直接抛 400，避免一次处理过多
def _reject_oversized_skill_batch(values: list[str]) -> None:
    if _count_unique_skill_names(values) > SKILL_BATCH_OPERATION_MAX_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot process more than {SKILL_BATCH_OPERATION_MAX_NAMES} skills at once",
        )


# 合并"已禁用技能名"列表：在 current 基础上加入 add、移除 remove，并去重、保序、规范化。
# 顺序为「新增项在前、原有项在后」，remove 优先级最高（既不新增也不保留）
def _merge_disabled_skill_names(
    current: object,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> list[str]:
    # 待移除集合
    remove_set = set(normalize_skill_name_list(remove or []))
    # 规范化后的新增名单
    add_names = normalize_skill_name_list(add or [])
    # 先放入未被移除的新增项
    ordered = [name for name in add_names if name not in remove_set]
    # 再追加原有项中未被移除且尚未出现的名字
    ordered.extend(
        name
        for name in normalize_skill_name_list(current)
        if name not in remove_set and name not in ordered
    )
    return normalize_skill_name_list(ordered)


class UpdateFileRequest(BaseModel):
    """更新文件内容的请求"""

    # 文件的完整新内容（整文件覆盖写入）
    content: str


# 解析 SKILL.md 的 frontmatter，返回 (名称, 描述, 标签列表)。
# 解析属 CPU 阻塞操作，放到线程池执行，避免阻塞事件循环
async def _parse_skill_md_offload(content: str) -> tuple[Optional[str], str, list[str]]:
    from src.infra.skill.parser import parse_skill_md

    return await run_blocking_io(parse_skill_md, content)


# ==========================================
# 用户 Skills API
# ==========================================


@router.post("/upload/preview")
async def preview_zip_skills(
    file: UploadFile,
    user: TokenPayload = Depends(require_permissions("skill:write")),
    storage: SkillStorage = Depends(get_storage),
):
    """预览 ZIP 文件中的 skills（不创建，返回 skill 列表供用户选择）"""
    # 只接受 .zip 压缩包
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a ZIP archive")

    try:
        max_size_bytes, max_size_mb = _get_skill_upload_max_size()
        content = await _read_upload_file_limited(
            file,
            max_size_bytes=max_size_bytes,
            max_size_mb=max_size_mb,
            purpose="ZIP file",
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to read file content")

    # 在线程池中解析 ZIP，得到技能预览列表；解析失败（如格式非法）返回 400
    try:
        skill_list = await run_blocking_io(_parse_zip_skill_preview, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 批量检查哪些已存在
    user_skills = await storage.list_user_skills(user.sub)
    existing_names = {s["skill_name"] for s in user_skills}

    # 为每个待导入技能标注是否与已安装技能重名，供前端提示/选择
    for skill in skill_list:
        skill["already_exists"] = skill["name"] in existing_names

    return {
        "skill_count": len(skill_list),
        "skills": skill_list,
    }


@router.post("/upload", status_code=201)
async def upload_skill_from_zip(
    file: UploadFile,
    skill_names: Optional[str] = Form(default=None),
    user: TokenPayload = Depends(require_permissions("skill:write")),
    storage: SkillStorage = Depends(get_storage),
):
    """从 ZIP 文件上传创建技能（支持多个 SKILL.md，可选择性安装）"""
    # 只接受 .zip 压缩包
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a ZIP archive")

    try:
        max_size_bytes, max_size_mb = _get_skill_upload_max_size()
        content = await _read_upload_file_limited(
            file,
            max_size_bytes=max_size_bytes,
            max_size_mb=max_size_mb,
            purpose="ZIP file",
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to read file content")

    # 在线程池中解析 ZIP 得到全部技能；解析失败（如格式非法）返回 400
    try:
        skills = await run_blocking_io(_parse_zip_skills, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 如果指定了 skill_names，只安装选中的
    if skill_names:
        name_set = set(n.strip() for n in skill_names.split(",") if n.strip())
        skills = [(n, t, b) for n, t, b in skills if n in name_set]

    created: list[dict] = []
    errors: list[dict] = []

    # 批量获取已存在 skill
    user_skills = await storage.list_user_skills(user.sub)
    existing_names = {s["skill_name"] for s in user_skills}

    # 逐个创建技能：重名的跳过并记入 errors，其余写入存储
    for skill_name, text_files, binary_files in skills:
        if skill_name in existing_names:
            errors.append({"name": skill_name, "reason": "already exists"})
            continue

        try:
            await storage.create_user_skill(
                skill_name,
                text_files,
                user.sub,
                installed_from=InstalledFrom.MANUAL,
                binary_files=binary_files if binary_files else None,
            )
            created.append(
                {
                    "name": skill_name,
                    "file_count": len(text_files) + len(binary_files),
                    "binary_file_count": len(binary_files),
                }
            )
        except Exception as e:
            errors.append({"name": skill_name, "reason": str(e)})

    # 无一成功但存在错误：整体判定为失败，返回 400
    if not created and errors:
        raise HTTPException(status_code=400, detail=f"All skills failed: {errors[0]['reason']}")

    return {
        "message": f"Created {len(created)} skill(s)",
        "created": created,
        "errors": errors,
        "skill_count": len(created),
    }


@router.get("/", response_model=UserSkillListResponse)
async def list_user_skills(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    q: str | None = None,
    tags: list[str] | None = Query(None),
    user: TokenPayload = Depends(require_permissions("skill:read")),
    storage: SkillStorage = Depends(get_storage),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """列出用户安装的所有 Skills（含发布状态）"""
    # Get disabled_skills from user metadata
    user_storage = UserStorage()
    user_doc = await user_storage.get_by_id(user.sub)
    disabled_skills: list[str] = []
    pinned_skill_names: list[str] = []
    favorite_skill_names: list[str] = []
    if user_doc and user_doc.metadata:
        disabled_skills = normalize_skill_name_list(user_doc.metadata.get("disabled_skills", []))
        pinned_skill_names = normalize_skill_name_list(
            user_doc.metadata.get("pinned_skill_names", [])
        )
        favorite_skill_names = normalize_skill_name_list(
            user_doc.metadata.get("favorite_skill_names", [])
        )
    available_tags = await storage.list_user_skill_tags(user.sub)

    # 分页拉取当前页技能，同时带入禁用/置顶/收藏名单与搜索(q)、标签过滤条件
    skills = await storage.list_user_skills(
        user.sub,
        skip=skip,
        limit=limit,
        disabled_skills=disabled_skills,
        pinned_skill_names=pinned_skill_names,
        favorite_skill_names=favorite_skill_names,
        q=q,
        tags=tags,
    )
    # 统计满足过滤条件的技能总数与其中被禁用的数量，推算启用数量
    total = await storage.count_user_skills(user.sub, q=q, tags=tags)
    disabled_count = await storage.count_disabled_user_skills(
        user.sub,
        disabled_skills=disabled_skills,
        q=q,
        tags=tags,
    )
    enabled_count = total - disabled_count
    # 当前页无数据时提前返回，避免后续无意义的批量查询
    if not skills:
        return UserSkillListResponse(
            skills=[],
            total=total,
            enabled_count=max(enabled_count, 0),
            skip=skip,
            limit=limit,
            available_tags=available_tags,
        )

    skill_names = [s["skill_name"] for s in skills]
    # 批量查询当前页发布状态，避免按用户拉取全部发布记录
    published_map = await marketplace.get_user_published_skills(
        user.sub,
        skill_names=skill_names,
    )

    # 批量获取所有 SKILL.md 用于提取 description
    skill_md_map = await storage.batch_get_skill_md_contents(skill_names, user.sub)
    description_map: dict[str, str] = {}
    tags_map: dict[str, list[str]] = {}
    # 逐个解析 SKILL.md，提取描述与标签用于列表展示
    for name, content in skill_md_map.items():
        if content:
            _, parsed_desc, parsed_tags = await _parse_skill_md_offload(content)
            if parsed_desc:
                description_map[name] = parsed_desc
            if parsed_tags:
                tags_map[name] = parsed_tags

    # 组装响应项：合并描述/标签/发布状态/置顶/收藏等信息
    items = [
        UserSkill(
            skill_name=s["skill_name"],
            description=description_map.get(s["skill_name"], ""),
            tags=tags_map.get(s["skill_name"], []),
            files=s.get("file_paths", []),
            enabled=s["enabled"],
            file_count=s["file_count"],
            installed_from=s.get("installed_from"),
            published_marketplace_name=s.get("published_marketplace_name"),
            created_at=s.get("created_at"),
            updated_at=s.get("updated_at"),
            is_published=bool(s.get("published_marketplace_name")),
            # 商店上架状态：优先用已发布的商店名查询，缺省视为上架(True)
            marketplace_is_active=published_map.get(
                s.get("published_marketplace_name") or s["skill_name"], {}
            ).get("is_active", True),
            is_pinned=bool(s.get("is_pinned")),
            is_favorite=bool(s.get("is_favorite")),
        )
        for s in skills
    ]
    return UserSkillListResponse(
        skills=items,
        total=total,
        enabled_count=max(enabled_count, 0),
        skip=skip,
        limit=limit,
        available_tags=available_tags,
    )


@router.get("/{name}", response_model=UserSkill)
async def get_user_skill(
    name: str,
    user: TokenPayload = Depends(require_permissions("skill:read")),
    storage: SkillStorage = Depends(get_storage),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """获取用户某个 Skill 的详细信息"""
    # 以文件路径列表是否为空来判断技能是否存在
    file_paths = await storage.list_skill_file_paths(name, user.sub)
    if not file_paths:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    # Get disabled_skills from user metadata
    user_storage = UserStorage()
    user_doc = await user_storage.get_by_id(user.sub)
    disabled_skills = set()
    if user_doc and user_doc.metadata:
        disabled_skills = set(
            normalize_skill_name_list(user_doc.metadata.get("disabled_skills", []))
        )
    pinned_skill_names = (
        set(normalize_skill_name_list((user_doc.metadata or {}).get("pinned_skill_names", [])))
        if user_doc
        else set()
    )
    favorite_skill_names = (
        set(normalize_skill_name_list((user_doc.metadata or {}).get("favorite_skill_names", [])))
        if user_doc
        else set()
    )
    # 启用状态 = 不在用户的禁用名单中
    enabled = name not in disabled_skills

    # Get metadata from __meta__ doc
    # 从 __meta__ 文档读取安装来源、已发布的商店名等元信息
    meta = await storage.get_skill_meta(name, user.sub)
    published_map = await marketplace.get_user_published_skills(
        user.sub,
        skill_names=[name],
    )

    # 使用文件聚合统计获取时间戳，与 list_user_skills 保持一致
    file_stats = await storage.get_skill_file_stats(name, user.sub)

    # 读取并解析 SKILL.md，提取描述与标签
    async def extract_metadata() -> tuple[str, list[str]]:
        skill_md = await storage.get_skill_file(name, "SKILL.md", user.sub)
        _, desc, tags = await _parse_skill_md_offload(skill_md or "")
        return desc, tags

    description, tags = await extract_metadata()

    return UserSkill(
        skill_name=name,
        description=description,
        tags=tags,
        enabled=enabled,
        files=file_paths,
        file_count=file_stats["file_count"],
        installed_from=meta.installed_from.value if meta else None,
        published_marketplace_name=meta.published_marketplace_name if meta else None,
        created_at=file_stats.get("created_at"),
        updated_at=file_stats.get("updated_at"),
        is_published=(bool(meta.published_marketplace_name) if meta else name in published_map),
        marketplace_is_active=published_map.get(
            (meta.published_marketplace_name if meta else None) or name, {}
        ).get("is_active", True),
        is_pinned=name in pinned_skill_names,
        is_favorite=name in favorite_skill_names,
    )


@router.get("/{name}/files/{path:path}")
async def get_skill_file(
    name: str,
    path: str,
    user: TokenPayload = Depends(require_permissions("skill:read")),
    storage: SkillStorage = Depends(get_storage),
):
    """读取 Skill 的单个文件（文本内容或二进制文件元数据）"""
    # 规范化并校验路径：清洗后若与原路径不一致，说明含非法片段(如 ..)，拒绝
    safe_path = sanitize_file_path(path)
    if safe_path != path:
        raise HTTPException(status_code=400, detail="Invalid file path")
    content = await storage.get_skill_file(name, safe_path, user.sub)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")

    # 检查是否为二进制文件引用
    # 若内容是二进制引用（指向对象存储），返回可下载 URL 与元信息而非原始内容
    binary_ref = await parse_binary_ref_async(content)
    if binary_ref:
        file_url = f"/api/upload/file/{binary_ref.storage_key}"
        return {
            "content": content,
            "is_binary": True,
            "url": file_url,
            "mime_type": binary_ref.mime_type,
            "size": binary_ref.size,
        }

    return {"content": content}


@router.put("/{name}/files/{path:path}")
async def update_skill_file(
    name: str,
    path: str,
    body: UpdateFileRequest,
    user: TokenPayload = Depends(require_permissions("skill:write")),
    storage: SkillStorage = Depends(get_storage),
):
    """更新 Skill 的单个文件"""
    # 规范化并校验路径，防止路径穿越
    safe_path = sanitize_file_path(path)
    if safe_path != path:
        raise HTTPException(status_code=400, detail="Invalid file path")
    content = body.content

    # 检查 __meta__ 是否已存在，以决定是否是新 skill
    existing_meta = await storage.get_skill_meta(name, user.sub)
    is_new = existing_meta is None

    await storage.set_skill_file(name, safe_path, content, user.sub)

    # 新 skill 自动创建 __meta__
    if is_new:
        await storage.set_skill_meta(name, user.sub)

    # 失效缓存
    await storage.invalidate_user_cache(user.sub)

    return {"message": "File updated"}


@router.put("/{name}/binary-files/{path:path}")
async def upload_skill_binary_file(
    name: str,
    path: str,
    file: UploadFile,
    user: TokenPayload = Depends(require_permissions("skill:write")),
    storage: SkillStorage = Depends(get_storage),
):
    """上传二进制文件到 Skill（自动存储到 S3/本地存储）"""
    # 规范化并校验路径，防止路径穿越
    safe_path = sanitize_file_path(path)
    if safe_path != path:
        raise HTTPException(status_code=400, detail="Invalid file path")

    max_file_size, max_file_size_mb = _get_skill_upload_max_size()
    data = await _read_upload_file_limited(
        file,
        max_size_bytes=max_file_size,
        max_size_mb=max_file_size_mb,
        purpose="Binary file",
    )

    # 拒绝空文件
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    # 检测 MIME 类型
    mime_type = file.content_type or guess_mime_type(safe_path)

    # 检查 skill 是否已存在
    existing_meta = await storage.get_skill_meta(name, user.sub)
    is_new = existing_meta is None

    # 将二进制数据写入底层存储(S3/本地)，返回引用（存储 key、MIME、大小）
    try:
        binary_ref = await storage.set_skill_binary_file(
            name, safe_path, data, user.sub, mime_type=mime_type
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload binary file: {e}")

    # 新 skill 自动创建 __meta__
    if is_new:
        await storage.set_skill_meta(name, user.sub)

    # 失效缓存
    await storage.invalidate_user_cache(user.sub)

    return {
        "message": "Binary file uploaded",
        "storage_key": binary_ref.storage_key,
        "url": f"/api/upload/file/{binary_ref.storage_key}",
        "mime_type": binary_ref.mime_type,
        "size": binary_ref.size,
    }


@router.delete("/{name}/files/{path:path}")
async def delete_skill_file(
    name: str,
    path: str,
    user: TokenPayload = Depends(require_permissions("skill:write")),
    storage: SkillStorage = Depends(get_storage),
):
    """删除 Skill 的单个文件"""
    # 规范化并校验路径，防止路径穿越
    safe_path = sanitize_file_path(path)
    if safe_path != path:
        raise HTTPException(status_code=400, detail="Invalid file path")
    # 检查 skill 和文件是否存在
    existing_paths = await storage.list_skill_file_paths(name, user.sub)
    if not existing_paths:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    if safe_path not in existing_paths:
        raise HTTPException(status_code=404, detail=f"File '{path}' not found in skill '{name}'")

    await storage.delete_skill_file(name, safe_path, user.sub)

    # 检查 skill 是否还有剩余文件（排除 __meta__），若无则清理 __meta__ 避免幽灵 skill
    remaining = await storage.list_skill_file_paths(name, user.sub)
    if not remaining:
        await storage.delete_skill_meta(name, user.sub)

    # 失效缓存
    await storage.invalidate_user_cache(user.sub)

    return {"message": f"File '{path}' deleted"}


@router.delete("/{name}")
async def delete_user_skill(
    name: str,
    user: TokenPayload = Depends(require_permissions("skill:delete")),
    storage: SkillStorage = Depends(get_storage),
):
    """删除（卸载）用户的 Skill（不影响商店发布状态）"""
    # 删除该技能的所有文件与 __meta__ 文档（商店中的发布副本保持不变）
    await storage.delete_skill_and_meta(name, user.sub)

    # 清理 disabled_skills 中的条目（如果有）
    user_storage = UserStorage()
    user_doc = await user_storage.get_by_id(user.sub)
    if user_doc and user_doc.metadata:
        disabled = _merge_disabled_skill_names(
            user_doc.metadata.get("disabled_skills", []),
            remove=[name],
        )
        await user_storage.update_metadata(user.sub, {"disabled_skills": disabled})

    # 同步清理该技能的用户偏好（置顶/收藏）
    await storage.remove_user_skill_preference(user.sub, [name])

    # 失效缓存
    await storage.invalidate_user_cache(user.sub)

    return {"message": f"Skill '{name}' deleted"}


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""

    # 待删除的技能名列表
    names: list[str]


class BatchToggleRequest(BaseModel):
    """批量切换请求"""

    # 待切换启用状态的技能名列表
    names: list[str]
    # 目标启用状态：True=启用，False=禁用
    enabled: bool


class ToggleRequest(BaseModel):
    """Toggle 请求（可选指定目标状态）"""

    # 目标启用状态；为 None 时表示「翻转」当前状态
    enabled: Optional[bool] = None


async def _ensure_skill_exists(storage: SkillStorage, skill_name: str, user_id: str) -> None:
    """Reject toggle operations for non-existent skills to avoid ghost disabled state."""
    paths = await storage.list_skill_file_paths(skill_name, user_id)
    if not paths:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")


@router.patch("/{name}/preference", response_model=UserSkillPreferenceResponse)
async def update_skill_preference(
    name: str,
    preference: UserSkillPreferenceUpdate,
    user: TokenPayload = Depends(require_permissions("skill:read")),
    storage: SkillStorage = Depends(get_storage),
):
    """更新当前用户对 Skill 的置顶/收藏偏好。"""
    await _ensure_skill_exists(storage, name, user.sub)
    # 更新当前用户对该技能的偏好（置顶/收藏），仅影响该用户视图
    updated = await storage.update_user_preference(
        user_id=user.sub,
        skill_name=name,
        update=preference.model_dump(mode="json"),
    )
    return UserSkillPreferenceResponse(skill_name=name, **updated)


# ==========================================
# 批量操作
# ==========================================


@router.post("/batch/delete")
async def batch_delete_skills(
    body: BatchDeleteRequest,
    user: TokenPayload = Depends(require_permissions("skill:delete")),
    storage: SkillStorage = Depends(get_storage),
):
    """批量删除 Skills"""
    # 数量保护 + 规范化去重后逐个删除，失败记入 errors 不中断
    _reject_oversized_skill_batch(body.names)
    names = normalize_skill_name_list(body.names)
    deleted: list[str] = []
    errors: list[dict[str, str]] = []

    for name in names:
        try:
            await storage.delete_skill_and_meta(name, user.sub)
            deleted.append(name)
        except Exception as e:
            errors.append({"name": name, "reason": str(e)})

    # 有实际删除才失效缓存并同步清理禁用名单/偏好
    if deleted:
        await storage.invalidate_user_cache(user.sub)

        # 清理 disabled_skills 中已删除的 skill
        user_storage = UserStorage()
        user_doc = await user_storage.get_by_id(user.sub)
        if user_doc and user_doc.metadata:
            disabled = _merge_disabled_skill_names(
                user_doc.metadata.get("disabled_skills", []),
                remove=deleted,
            )
            await user_storage.update_metadata(user.sub, {"disabled_skills": disabled})
        await storage.remove_user_skill_preference(user.sub, deleted)

    return {"deleted": deleted, "errors": errors}


@router.post("/batch/toggle")
async def batch_toggle_skills(
    body: BatchToggleRequest,
    user: TokenPayload = Depends(require_permissions("skill:write")),
    storage: SkillStorage = Depends(get_storage),
):
    """批量切换 Skills 的启用状态"""
    missing_names = []
    # 数量保护 + 规范化；先校验全部技能存在，任一缺失则整体 404（避免产生幽灵禁用态）
    _reject_oversized_skill_batch(body.names)
    names = normalize_skill_name_list(body.names)
    for name in names:
        try:
            await _ensure_skill_exists(storage, name, user.sub)
        except HTTPException:
            missing_names.append(name)

    if missing_names:
        missing = ", ".join(sorted(missing_names))
        raise HTTPException(status_code=404, detail=f"Skill(s) not found: {missing}")

    # Get current disabled_skills from user metadata
    user_storage = UserStorage()
    user_doc = await user_storage.get_by_id(user.sub)
    if user_doc is None:
        raise HTTPException(status_code=404, detail="User not found")

    # 启用=从禁用名单移除这些技能；禁用=加入禁用名单
    if body.enabled:
        disabled = _merge_disabled_skill_names(
            (user_doc.metadata or {}).get("disabled_skills", []),
            remove=names,
        )
    else:
        disabled = _merge_disabled_skill_names(
            (user_doc.metadata or {}).get("disabled_skills", []),
            add=names,
        )

    # Invalidate cache first, then update metadata
    # This ensures clients see fresh data even if metadata update fails
    await storage.invalidate_user_cache(user.sub)
    await user_storage.update_metadata(
        user.sub,
        {"disabled_skills": disabled},
    )

    return {"updated": names, "errors": []}


@router.patch("/{name}/toggle")
async def toggle_user_skill(
    name: str,
    body: Optional[ToggleRequest] = None,
    user: TokenPayload = Depends(require_permissions("skill:write")),
    storage: SkillStorage = Depends(get_storage),
):
    """切换或设置 Skill 的启用状态"""
    await _ensure_skill_exists(storage, name, user.sub)

    # Get current disabled_skills from user metadata
    user_storage = UserStorage()
    user_doc = await user_storage.get_by_id(user.sub)
    if user_doc is None:
        raise HTTPException(status_code=404, detail="User not found")
    current_disabled = normalize_skill_name_list(
        (user_doc.metadata or {}).get("disabled_skills", [])
    )

    # 请求体带 enabled 则用作目标状态；否则为 None 表示翻转当前状态
    target_enabled = body.enabled if body else None

    if target_enabled is not None:
        # 直接设置目标状态
        if target_enabled:
            disabled = _merge_disabled_skill_names(current_disabled, remove=[name])
        else:
            disabled = _merge_disabled_skill_names(current_disabled, add=[name])
    else:
        # Flip 当前状态
        if name in current_disabled:
            disabled = _merge_disabled_skill_names(current_disabled, remove=[name])
        else:
            disabled = _merge_disabled_skill_names(current_disabled, add=[name])

    # Invalidate cache first, then update metadata
    await storage.invalidate_user_cache(user.sub)
    await user_storage.update_metadata(
        user.sub,
        {"disabled_skills": disabled},
    )

    # 依据是否仍在禁用名单中，计算最终启用状态并返回
    is_enabled = name not in disabled
    status = "enabled" if is_enabled else "disabled"
    return {
        "skill_name": name,
        "enabled": is_enabled,
        "message": f"Skill '{name}' is now {status}",
    }


# ==========================================
# 发布到商店
# ==========================================


@router.post("/{name}/publish", response_model=MarketplaceSkillResponse)
async def publish_skill_to_marketplace(
    name: str,
    data: Optional[PublishToMarketplaceRequest] = None,
    user: TokenPayload = Depends(require_permissions("marketplace:publish")),
    storage: SkillStorage = Depends(get_storage),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """将用户的 Skill 发布到商店（支持多次发布更新）"""
    # 取该技能的全部文件；不存在则 404
    user_files = await storage.get_skill_files(name, user.sub)
    if not user_files:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    from src.infra.skill.parser import parse_skill_md as _parse_md
    from src.infra.skill.parser import sanitize_skill_name

    # 从 SKILL.md 解析默认描述与标签；商店名可由请求自定义，否则用技能原名并做清洗
    _, default_description, default_tags = _parse_md(user_files.get("SKILL.md", ""))
    target_name = sanitize_skill_name(
        (data.skill_name if data and data.skill_name else name).strip()
    )
    if not target_name:
        raise HTTPException(status_code=400, detail="Marketplace skill name is required")

    # 商店已存在同名条目：本人则更新，非本人则名称被占用返回 409；不存在则走新建分支
    existing = await marketplace.get_marketplace_skill(target_name)
    if existing:
        if existing.created_by != user.sub:
            raise HTTPException(
                status_code=409,
                detail=f"Marketplace skill name '{target_name}' is already taken",
            )
        update_data = MarketplaceSkillUpdate(
            description=(
                data.description if data and data.description is not None else default_description
            ),
            tags=data.tags if data and data.tags is not None else existing.tags,
            version=(data.version if data and data.version is not None else existing.version),
            is_active=True,
        )
        await marketplace.update_marketplace_skill(target_name, update_data)
    else:
        create_data = MarketplaceSkillCreate(
            skill_name=target_name,
            description=(
                data.description if data and data.description is not None else default_description
            ),
            tags=data.tags if data and data.tags is not None else default_tags,
            version=data.version if data and data.version is not None else "1.0.0",
        )
        await marketplace.create_marketplace_skill(create_data, user_id=user.sub)

    # 同步文件到商店，并在本地 __meta__ 记录已发布的商店名以建立关联
    try:
        await marketplace.sync_marketplace_files(target_name, user_files)
        # Update __meta__ doc with published_marketplace_name
        meta = await storage.get_skill_meta(name, user.sub)
        await storage.set_skill_meta(
            name,
            user.sub,
            installed_from=meta.installed_from if meta else InstalledFrom.MANUAL,
            published_marketplace_name=target_name,
        )
    except Exception:
        # 同步失败：若本次是新建的商店记录则回滚删除，避免残留空条目
        if not existing:
            await marketplace.delete_marketplace_skill(target_name)
        raise HTTPException(status_code=500, detail="Failed to sync files to marketplace")

    response = await marketplace.get_marketplace_skill_response(target_name)
    if not response:
        raise HTTPException(status_code=500, detail="Failed to publish skill")
    return response

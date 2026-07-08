# src/api/routes/marketplace.py
"""
用户商城 API

提供商城浏览、安装和直接发布功能。
"""

# 用户商城（marketplace）API 路由模块，挂载于 /api/marketplace：
# 面向普通用户与创建者/管理员，提供技能（Skill）市场的浏览、预览、发布、更新、
# 安装到本地、从市场更新，以及管理员的激活/停用/删除等操作。
# 权限分级：marketplace:read（浏览/安装）、marketplace:publish（发布/更新）、
# marketplace:admin（激活/删除；创建者对自己的条目亦可操作）。
# 安全要点：所有涉及文件路径的入参都经 sanitize_file_path 做防目录穿越处理。
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api.deps import require_permissions
from src.infra.skill.binary import parse_binary_ref
from src.infra.skill.marketplace import MarketplaceStorage
from src.infra.skill.storage import SkillStorage
from src.infra.skill.types import (
    InstalledFrom,
    MarketplaceSkillCreate,
    MarketplaceSkillResponse,
)
from src.kernel.schemas.user import TokenPayload


def sanitize_file_path(path: str) -> str:
    """Sanitize file path to prevent path traversal."""
    # 统一分隔符后按 "/" 拆分，丢弃空段与 ".." 段再拼回，从而消除目录穿越风险。
    # 注意：调用方通常用“清洗后是否等于原值”来判断路径是否合法（不等则拒绝）。
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != ".."]
    return "/".join(parts)


# 本模块路由挂载于 /api/marketplace 前缀下
router = APIRouter()
# 单个市场技能允许的最大文件数
MARKETPLACE_SKILL_MAX_FILES = 100
# 单个文件允许的最大字符数
MARKETPLACE_SKILL_MAX_FILE_CHARS = 256_000
# 单个技能所有文件合计的最大字符数
MARKETPLACE_SKILL_MAX_TOTAL_CHARS = 1_000_000


# 依赖注入：提供市场存储（管理商城技能条目及其文件）
def get_marketplace_storage() -> MarketplaceStorage:
    return MarketplaceStorage()


# 依赖注入：提供用户本地技能存储（安装/更新时写入用户目录）
def get_storage() -> SkillStorage:
    return SkillStorage()


class MarketplaceCreateRequest(BaseModel):
    """直接在商店创建 Skill 的请求"""

    # 技能名（发布前会经 sanitize_skill_name 安全化）
    skill_name: str
    # 技能描述
    description: str = ""
    # 标签列表，用于分类与检索
    tags: list[str] = []
    # 版本号
    version: str = "1.0.0"
    # 技能文件字典：{相对路径: 文本内容}
    files: dict[str, str] = {}


class SetActiveRequest(BaseModel):
    """Admin 激活/停用请求"""

    # True 激活、False 停用该市场技能
    is_active: bool


# 校验市场技能文件字典的合法性（发布/更新前调用）：
#   - 文件数量不超过上限；
#   - 每个路径必须“清洗后等于原值”（防目录穿越，否则 400）；
#   - 单文件字符数与所有文件合计字符数均不超过上限（超出返回 413）。
def _validate_marketplace_files_payload(files: dict[str, str]) -> None:
    # 文件数量上限
    if len(files) > MARKETPLACE_SKILL_MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"Marketplace skill contains too many files (max {MARKETPLACE_SKILL_MAX_FILES})",
        )

    total_chars = 0
    for path, content in files.items():
        # 路径安全校验：清洗后与原值不一致说明含 ".." 等非法段，拒绝
        safe_path = sanitize_file_path(path)
        if safe_path != path:
            raise HTTPException(status_code=400, detail=f"Invalid file path: {path}")
        content_chars = len(str(content))
        # 单文件字符数上限
        if content_chars > MARKETPLACE_SKILL_MAX_FILE_CHARS:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Marketplace skill file is too large "
                    f"(max {MARKETPLACE_SKILL_MAX_FILE_CHARS} characters)"
                ),
            )
        total_chars += content_chars
        # 所有文件合计字符数上限
        if total_chars > MARKETPLACE_SKILL_MAX_TOTAL_CHARS:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Marketplace skill files are too large "
                    f"(max {MARKETPLACE_SKILL_MAX_TOTAL_CHARS} total characters)"
                ),
            )


# 分批读取某市场技能的全部文件并合并成 {路径: 内容} 字典。
# 会核对是否与预期文件清单一致（缺文件视为数据不完整 → 500），并复用大小/路径校验。
async def _load_marketplace_files_payload(
    *,
    name: str,
    marketplace: MarketplaceStorage,
    expected_paths: list[str],
) -> dict[str, str]:
    # 分批拉取，避免一次性把大量文件读入内存
    files: dict[str, str] = {}
    async for batch in marketplace.iter_marketplace_file_batches(name):
        files.update(batch)
    # 完整性校验：期望的文件必须全部拿到，否则说明数据缺失
    missing_paths = set(expected_paths) - set(files)
    if missing_paths:
        raise HTTPException(
            status_code=500,
            detail="Marketplace skill files are incomplete",
        )
    _validate_marketplace_files_payload(files)
    return files


# ==========================================
# 用户商城 API
# ==========================================


# GET /：列出市场技能。权限 marketplace:read。
# 可见范围：所有已激活技能 + 调用者自己发布的（含已停用），支持标签/搜索/分页。
@router.get("/", response_model=list[MarketplaceSkillResponse])
async def list_marketplace_skills(
    tags: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = Query(50, ge=1, le=100),
    user: TokenPayload = Depends(require_permissions("marketplace:read")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """列出商城 Skills（所有用户：激活的 skill + 自己发布的含停用的）"""
    # tags 以逗号分隔的字符串传入，拆成列表
    tag_list = tags.split(",") if tags else None
    skills = await marketplace.list_marketplace_skills(
        tags=tag_list,
        search=search,
        include_inactive=False,
        viewer_id=user.sub,
        skip=skip,
        limit=limit,
    )
    return skills


# GET /tags：获取市场中所有可用标签。权限 marketplace:read。
@router.get("/tags")
async def list_tags(
    user: TokenPayload = Depends(require_permissions("marketplace:read")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """获取所有标签"""
    tags = await marketplace.list_all_tags()
    return {"tags": tags}


# POST /：在市场直接创建并发布一个技能（仅入市场库，不写入用户本地）。权限 marketplace:publish。
@router.post("/", response_model=MarketplaceSkillResponse, status_code=201)
async def create_marketplace_skill(
    data: MarketplaceCreateRequest,
    user: TokenPayload = Depends(require_permissions("marketplace:publish")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """在商店创建 Skill（仅发布，不写入用户本地）"""
    # 至少要有一个文件，且文件需通过数量/大小/路径安全校验
    if not data.files:
        raise HTTPException(status_code=400, detail="Skill must have at least one file")
    _validate_marketplace_files_payload(data.files)

    # 技能名安全化，作为市场中的唯一标识
    from src.infra.skill.parser import sanitize_skill_name

    safe_name = sanitize_skill_name(data.skill_name)

    try:
        create_data = MarketplaceSkillCreate(
            skill_name=safe_name,
            description=data.description,
            tags=data.tags,
            version=data.version,
        )
        await marketplace.create_marketplace_skill(create_data, user_id=user.sub)
    except ValueError as e:
        # 同名技能已存在等冲突 → 409
        raise HTTPException(status_code=409, detail=str(e))

    # 先建元数据条目，再同步文件；文件同步失败则回滚（删除刚建的条目），避免留下空壳
    try:
        await marketplace.sync_marketplace_files(safe_name, data.files)
    except Exception:
        await marketplace.delete_marketplace_skill(safe_name)
        raise HTTPException(
            status_code=500, detail="Failed to sync files, marketplace entry rolled back"
        )

    response = await marketplace.get_marketplace_skill_response(safe_name, viewer_id=user.sub)
    return response


# GET /{name}：预览单个市场技能的元数据。权限 marketplace:read。不存在则 404。
@router.get("/{name}", response_model=MarketplaceSkillResponse)
async def get_marketplace_skill(
    name: str,
    user: TokenPayload = Depends(require_permissions("marketplace:read")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """预览商城 Skill"""
    skill = await marketplace.get_marketplace_skill_response(name, viewer_id=user.sub)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Marketplace skill '{name}' not found")
    return skill


# PUT /{name}：更新市场技能（元数据 + 全量文件覆盖）。权限 marketplace:publish，
# 且仅创建者本人可改（否则 403）。
@router.put("/{name}", response_model=MarketplaceSkillResponse)
async def update_marketplace_skill(
    name: str,
    data: MarketplaceCreateRequest,
    user: TokenPayload = Depends(require_permissions("marketplace:publish")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """直接更新商店 Skill（仅创建者可操作）"""
    # 校验存在且仅创建者可更新
    skill = await marketplace.get_marketplace_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Marketplace skill '{name}' not found")
    if skill.created_by != user.sub:
        raise HTTPException(status_code=403, detail="Only creator can update")

    # 至少要有一个文件，且文件需通过数量/大小/路径安全校验
    if not data.files:
        raise HTTPException(status_code=400, detail="Skill must have at least one file")
    _validate_marketplace_files_payload(data.files)

    # 更新元数据
    from src.infra.skill.types import MarketplaceSkillUpdate

    update_data = MarketplaceSkillUpdate(
        description=data.description,
        tags=data.tags,
        version=data.version,
        is_active=True,
    )
    await marketplace.update_marketplace_skill(name, update_data)

    # 同步文件
    # 全量覆盖该技能在市场中的文件
    await marketplace.sync_marketplace_files(name, data.files)

    response = await marketplace.get_marketplace_skill_response(name, viewer_id=user.sub)
    return response


# GET /{name}/files：列出某市场技能的所有文件相对路径。权限 marketplace:read。
@router.get("/{name}/files")
async def list_marketplace_skill_files(
    name: str,
    user: TokenPayload = Depends(require_permissions("marketplace:read")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """列出商城 Skill 的所有文件路径"""
    # 无文件时区分两种情况：技能本身不存在 → 404；技能存在但确实没有文件 → 返回空列表
    paths = await marketplace.list_marketplace_file_paths(name)
    if not paths:
        skill = await marketplace.get_marketplace_skill(name)
        if not skill:
            raise HTTPException(status_code=404, detail="Skill not found")
    return {"files": paths}


# GET /{name}/files/{path}：读取某市场技能的单个文件内容。权限 marketplace:read。
# 二进制文件在库中以“引用”形式存储，命中时返回其对象存储访问 URL 与元信息。
@router.get("/{name}/files/{path:path}")
async def get_marketplace_file(
    name: str,
    path: str,
    user: TokenPayload = Depends(require_permissions("marketplace:read")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """读取商城 Skill 的单个文件"""
    # 路径安全校验：清洗后与原值不一致即视为非法路径（防目录穿越）
    safe_path = sanitize_file_path(path)
    if safe_path != path:
        raise HTTPException(status_code=400, detail="Invalid file path")
    content = await marketplace.get_marketplace_file(name, safe_path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")

    # 检查是否为二进制文件引用
    # 二进制文件不内联返回内容，而是给出指向 /api/upload 的下载 URL 与 mime/size
    binary_ref = parse_binary_ref(content)
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


# POST /{name}/install：把市场技能安装到当前用户的本地技能目录。权限 marketplace:read。
# 关键流程：校验存在/激活 → 校验未重复安装 → 完整读取市场文件后再覆盖本地副本
# （先读后写，避免读取失败却已破坏本地手动技能）→ 写入 __meta__ 标记来源为市场。
@router.post("/{name}/install")
async def install_marketplace_skill(
    name: str,
    user: TokenPayload = Depends(require_permissions("marketplace:read")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
    storage: SkillStorage = Depends(get_storage),
):
    """安装商城 Skill 到用户目录"""
    # 1. 检查商城 Skill 是否存在且激活（创建者可安装自己已停用的 skill）
    marketplace_skill = await marketplace.get_marketplace_skill(name)
    if not marketplace_skill:
        raise HTTPException(status_code=404, detail=f"Marketplace skill '{name}' not found")
    if not marketplace_skill.is_active and marketplace_skill.created_by != user.sub:
        raise HTTPException(status_code=403, detail="This skill has been deactivated")

    # 2. 检查用户是否已安装（检查 __meta__ 或文件是否存在）
    existing_meta = await storage.get_skill_meta(name, user.sub)
    if existing_meta:
        # 已作为“市场技能”安装过则拒绝重复安装（手动创建的同名技能不在此拦截）
        if existing_meta.installed_from == InstalledFrom.MARKETPLACE:
            raise HTTPException(status_code=409, detail=f"Skill '{name}' already installed")

    # 3. 获取商城文件数量
    file_paths = await marketplace.list_marketplace_file_paths(name)
    if not file_paths:
        raise HTTPException(status_code=400, detail="Marketplace skill has no files")

    # 4. 先完整读取商城文件，再替换用户本地副本；避免读取失败时删除本地手动技能。
    try:
        files = await _load_marketplace_files_payload(
            name=name,
            marketplace=marketplace,
            expected_paths=file_paths,
        )
        await storage.sync_skill_files(name, files, user.sub)
        await storage.set_skill_meta(
            name,
            user.sub,
            installed_from=InstalledFrom.MARKETPLACE,
        )
        await storage.invalidate_user_cache(user.sub)
    except Exception as e:
        # 并发/重复安装等竞态：错误信息含 duplicate/already 时归一化为 409
        err_msg = str(e).lower()
        if "duplicate" in err_msg or "already" in err_msg:
            raise HTTPException(status_code=409, detail=f"Skill '{name}' already installed")
        raise

    return {
        "message": f"Skill '{name}' installed successfully",
        "skill_name": name,
        "file_count": len(files),
    }


# POST /{name}/update：用市场最新内容覆盖用户已安装的同名技能。权限 marketplace:read。
# 仅当该技能确由市场安装（installed_from=MARKETPLACE）时允许；手动技能不可被市场覆盖。
@router.post("/{name}/update")
async def update_from_marketplace(
    name: str,
    user: TokenPayload = Depends(require_permissions("marketplace:read")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
    storage: SkillStorage = Depends(get_storage),
):
    """从商城更新用户的 Skill（覆盖）"""
    marketplace_skill = await marketplace.get_marketplace_skill(name)
    if not marketplace_skill:
        raise HTTPException(status_code=404, detail=f"Marketplace skill '{name}' not found")
    if not marketplace_skill.is_active and marketplace_skill.created_by != user.sub:
        raise HTTPException(status_code=403, detail="This skill has been deactivated")

    # Check if skill is installed by checking __meta__
    meta = await storage.get_skill_meta(name, user.sub)
    if not meta:
        raise HTTPException(
            status_code=400, detail=f"Skill '{name}' not installed. Install it first."
        )
    # 手动创建的技能不允许被市场版本覆盖，避免误删用户自有内容
    if meta.installed_from != InstalledFrom.MARKETPLACE:
        raise HTTPException(
            status_code=409,
            detail=f"Skill '{name}' is a manual skill and cannot be updated from marketplace.",
        )

    file_paths = await marketplace.list_marketplace_file_paths(name)
    if not file_paths:
        raise HTTPException(status_code=400, detail="Marketplace skill has no files")

    files = await _load_marketplace_files_payload(
        name=name,
        marketplace=marketplace,
        expected_paths=file_paths,
    )
    await storage.sync_skill_files(name, files, user.sub)

    # Update __meta__ doc (preserve installed_from and published_marketplace_name)
    # 覆盖文件后刷新 __meta__，保留原来的来源与已发布市场名，避免丢失溯源信息
    await storage.set_skill_meta(
        name,
        user.sub,
        installed_from=meta.installed_from,
        published_marketplace_name=meta.published_marketplace_name,
    )

    await storage.invalidate_user_cache(user.sub)

    return {
        "message": f"Skill '{name}' updated from marketplace",
        "skill_name": name,
        "file_count": len(files),
    }


# ==========================================
# Admin 操作（集成在商城路由中）
# ==========================================


# PATCH /{name}/activate：激活或停用市场技能。权限 marketplace:admin，
# 或该技能的创建者本人。停用后普通用户不可见/不可安装。
@router.patch("/{name}/activate", response_model=MarketplaceSkillResponse)
async def set_marketplace_active(
    name: str,
    data: SetActiveRequest,
    user: TokenPayload = Depends(require_permissions("marketplace:admin")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """激活或停用商城 Skill（admin 或创建者可操作）"""
    skill = await marketplace.get_marketplace_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Marketplace skill '{name}' not found")
    # 仅管理员或创建者可激活/停用
    if "marketplace:admin" not in (user.permissions or []) and skill.created_by != user.sub:
        raise HTTPException(status_code=403, detail="Only admin or creator can activate/deactivate")

    await marketplace.set_marketplace_active(name, data.is_active)
    response = await marketplace.get_marketplace_skill_response(name, viewer_id=user.sub)
    return response


# DELETE /{name}：从市场删除技能。权限 marketplace:admin 或创建者本人。
# 仅删除市场条目，不影响已安装用户的本地副本。
@router.delete("/{name}")
async def delete_marketplace_skill(
    name: str,
    user: TokenPayload = Depends(require_permissions("marketplace:admin")),
    marketplace: MarketplaceStorage = Depends(get_marketplace_storage),
):
    """删除商城 Skill（admin 或创建者可操作，不影响已安装用户的本地副本）"""
    skill = await marketplace.get_marketplace_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Marketplace skill '{name}' not found")
    # 仅管理员或创建者可删除
    if "marketplace:admin" not in (user.permissions or []) and skill.created_by != user.sub:
        raise HTTPException(status_code=403, detail="Only admin or creator can delete")

    deleted = await marketplace.delete_marketplace_skill(name)
    # 并发下可能已被他人删除，返回 False → 视为未找到
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Marketplace skill '{name}' not found")
    return {"message": f"Marketplace skill '{name}' deleted"}

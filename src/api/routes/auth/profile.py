"""
User profile routes (password change, avatar, profile, username)
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from src.api.deps import get_current_user_required
from src.infra.logging import get_logger
from src.infra.user.manager import UserManager
from src.kernel.exceptions import ValidationError
from src.kernel.schemas.user import TokenPayload, User, UserUpdate

router = APIRouter()
logger = get_logger(__name__)
# 用户 metadata 中列表类字段的通用最大条目数（防止恶意写入超大列表撑爆存储）
MAX_USER_METADATA_LIST_ITEMS = 100
# 置顶模型 id 列表的最大数量
MAX_PINNED_MODEL_IDS = 10
# 置顶预设 id 列表的最大数量
MAX_PINNED_PRESET_IDS = 10
# 收藏预设 id 列表的最大数量
MAX_FAVORITE_PRESET_IDS = 100


class AvatarUpdateRequest(BaseModel):
    """Request schema for updating avatar"""

    # 头像 URL（通常是文件上传到对象存储/S3 后返回的可访问地址）
    avatar_url: str


class UsernameUpdateRequest(BaseModel):
    """Request schema for updating username"""

    # 新用户名：长度限制 3~50 个字符
    username: str = Field(..., min_length=3, max_length=50)


class MetadataUpdateRequest(BaseModel):
    """Request schema for updating user metadata (partial merge)"""

    # 待更新的 metadata 字典：与现有 metadata 做部分合并（merge），而非整体覆盖
    metadata: dict


# 校验某字段必须是"字符串列表"且长度不超过 max_items，否则抛 400；用于约束 metadata 中的各列表字段
def _validate_bounded_string_list(
    values: object,
    *,
    field_name: str,
    max_items: int = MAX_USER_METADATA_LIST_ITEMS,
) -> None:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name}: must be a list of strings.",
        )
    if len(values) > max_items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many {field_name}: maximum {max_items} allowed.",
        )


# POST /update-avatar —— 更新当前登录用户的头像
# 认证要求：需携带有效 access token；请求体：AvatarUpdateRequest（avatar_url）
# 副作用：将 avatar_url 写入用户记录并返回更新后的用户
@router.post("/update-avatar")
async def update_avatar(
    request: AvatarUpdateRequest,
    current_user: TokenPayload = Depends(get_current_user_required),
):
    """
    更新当前用户头像

    需要提供头像 URL（S3 上传后返回的 URL）。
    """
    manager = UserManager()
    user = await manager.get_user(current_user.sub)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    # Update avatar_url
    from src.infra.user.storage import UserStorage

    storage = UserStorage()
    updated_user = await storage.update(current_user.sub, UserUpdate(avatar_url=request.avatar_url))

    return updated_user


# GET /profile —— 获取当前登录用户的完整个人资料
# 认证要求：需携带有效 access token；响应体：User
@router.get("/profile", response_model=User)
async def get_user_profile(
    current_user: TokenPayload = Depends(get_current_user_required),
):
    """
    获取当前用户个人资料

    返回用户的完整信息，包括头像 URL 等。
    """
    manager = UserManager()
    user = await manager.get_user(current_user.sub)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )
    return user


# POST /update-username —— 修改当前登录用户的用户名
# 请求体：UsernameUpdateRequest（3~50 字符）；用户名重复时由存储层抛 ValidationError，转成 400
@router.post("/update-username")
async def update_username(
    request: UsernameUpdateRequest,
    current_user: TokenPayload = Depends(get_current_user_required),
):
    """
    更新当前用户名

    用户名不能与现有用户名重复。
    """
    from src.infra.user.storage import UserStorage

    storage = UserStorage()
    try:
        updated_user = await storage.update(current_user.sub, UserUpdate(username=request.username))
        return updated_user
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


# PUT /profile/metadata —— 部分更新当前用户的 metadata（合并式，而非整体覆盖）
# 请求体：MetadataUpdateRequest（metadata 字典）；写入前逐项校验受支持字段
# 校验项：language（白名单枚举）、theme（light/dark）、各列表字段（必须为字符串列表且不超上限）
@router.put("/profile/metadata")
async def update_user_metadata(
    request: MetadataUpdateRequest,
    current_user: TokenPayload = Depends(get_current_user_required),
):
    """
    部分更新当前用户 metadata（merge 方式）

    metadata 中的字段会与现有 metadata 合并。
    支持的字段: language (str), theme (str: light/dark), disabled_tools (list[str])
    """
    from src.infra.user.storage import UserStorage

    storage = UserStorage()

    # Validate language if provided
    # 支持的界面语言白名单（传入不在其中的值直接 400）
    supported_languages = {"en", "zh", "ja", "ko", "ru"}
    if "language" in request.metadata:
        lang = request.metadata["language"]
        if lang not in supported_languages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported language: {lang}. Supported: {', '.join(sorted(supported_languages))}",
            )

    # Validate theme if provided
    if "theme" in request.metadata:
        theme = request.metadata["theme"]
        if theme not in ("light", "dark"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid theme: {theme}. Must be 'light' or 'dark'.",
            )

    # Validate disabled_tools if provided
    if "disabled_tools" in request.metadata:
        _validate_bounded_string_list(
            request.metadata["disabled_tools"],
            field_name="disabled_tools",
        )

    # Validate pinned_model_ids if provided
    if "pinned_model_ids" in request.metadata:
        _validate_bounded_string_list(
            request.metadata["pinned_model_ids"],
            field_name="pinned_model_ids",
            max_items=MAX_PINNED_MODEL_IDS,
        )

    # Validate pinned_preset_ids if provided
    if "pinned_preset_ids" in request.metadata:
        _validate_bounded_string_list(
            request.metadata["pinned_preset_ids"],
            field_name="pinned_preset_ids",
            max_items=MAX_PINNED_PRESET_IDS,
        )

    # Validate favorite_preset_ids if provided
    if "favorite_preset_ids" in request.metadata:
        _validate_bounded_string_list(
            request.metadata["favorite_preset_ids"],
            field_name="favorite_preset_ids",
            max_items=MAX_FAVORITE_PRESET_IDS,
        )

    # Validate user skill preference lists if provided
    # 技能相关的三个偏好列表统一按"字符串列表 + 默认上限"校验
    for field_name in ("disabled_skills", "pinned_skill_names", "favorite_skill_names"):
        if field_name in request.metadata:
            _validate_bounded_string_list(
                request.metadata[field_name],
                field_name=field_name,
            )

    # 校验通过后做部分合并（只更新传入的键，其余保留），并返回更新后的用户
    updated_user = await storage.update_metadata(current_user.sub, request.metadata)
    return updated_user

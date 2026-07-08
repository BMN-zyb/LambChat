"""
Settings API router
"""

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_current_user_required, require_permissions
from src.infra.settings.service import SettingsService, get_settings_service
from src.kernel.schemas.setting import (
    SettingItem,
    SettingResetResponse,
    SettingsResponse,
    SettingUpdate,
    SettingUpdateResponse,
)
from src.kernel.schemas.user import TokenPayload

# 系统设置路由：挂载在 /api/settings，提供运行时可修改配置项的读取与更新
# 读取对已登录用户开放（按权限过滤返回内容），写/重置类操作需要 settings:manage 权限
router = APIRouter()


# GET /api/settings —— 获取全部设置项（响应体 SettingsResponse）
# 任意已登录用户可调用；拥有 settings:manage 权限者进入 admin 模式，可看到更多/敏感设置项
@router.get("/", response_model=SettingsResponse)
async def get_settings(
    user: TokenPayload = Depends(get_current_user_required),
    service: SettingsService = Depends(get_settings_service),
):
    """Get settings (filtered by permission)"""
    # Check if user has settings:manage permission
    # 判断当前用户是否具备管理权限，决定是否以 admin 模式返回（含更多/敏感配置项）
    has_admin = "settings:manage" in (user.permissions or [])
    settings = await service.get_all(admin_mode=has_admin)
    return SettingsResponse(settings=settings)


# GET /api/settings/{key} —— 按 key 获取单个设置项详情（响应体 SettingItem）
# 需要 settings:manage 权限，仅管理员可查看任意单项（可能含内部/敏感项）
@router.get("/{key}", response_model=SettingItem)
async def get_setting(
    key: str,
    _: TokenPayload = Depends(require_permissions("settings:manage")),
    service: SettingsService = Depends(get_settings_service),
):
    """Get single setting by key"""
    # 直接访问底层存储读取原始设置项（绕过 service 的默认值/脱敏包装，仅管理端使用）
    setting = await service._storage.get(key)
    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")
    return setting


# PUT /api/settings/{key} —— 更新单个设置项的值（运行时热更新）
# 请求体 SettingUpdate.value；需要 settings:manage 权限
# 写入后一般立即生效；若属于需重启才生效的配置，则在响应中提示 requires_restart
@router.put("/{key}", response_model=SettingUpdateResponse)
async def update_setting(
    key: str,
    data: SettingUpdate,
    user: TokenPayload = Depends(require_permissions("settings:manage")),
    service: SettingsService = Depends(get_settings_service),
):
    """Update a setting (requires settings:manage permission)"""
    try:
        # 写入新值并持久化，记录修改者 user.sub；key 不存在时返回 None
        setting = await service.set(key, data.value, user.sub)
        if not setting:
            raise HTTPException(status_code=404, detail="Setting not found")

        # 判断该配置项是否属于"需重启服务才生效"的类别（否则为运行时热生效）
        requires_restart = SettingsService.requires_restart(key)

        return SettingUpdateResponse(
            setting=setting,
            message=(
                "Setting updated. Server restart required to take effect."
                if requires_restart
                else "Setting updated successfully."
            ),
            requires_restart=requires_restart,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# POST /api/settings/init —— 从 .env 环境变量导入设置到数据库
# 仅导入数据库中尚未设置（unset）的项，不覆盖已有值；需要 settings:manage 权限
@router.post("/init", response_model=SettingResetResponse)
async def init_settings_from_env(
    _: TokenPayload = Depends(require_permissions("settings:manage")),
    service: SettingsService = Depends(get_settings_service),
):
    """Import settings from .env to database (only unset values)"""
    count = await service.init_from_env()
    return SettingResetResponse(
        message=f"Imported {count} settings from environment",
        reset_count=count,
    )


# POST /api/settings/reset —— 将所有设置项重置为默认值；需要 settings:manage 权限
@router.post("/reset", response_model=SettingResetResponse)
async def reset_all_settings(
    _: TokenPayload = Depends(require_permissions("settings:manage")),
    service: SettingsService = Depends(get_settings_service),
):
    """Reset all settings to default values"""
    count = await service.reset()
    return SettingResetResponse(
        message="All settings reset to defaults",
        reset_count=count,
    )


# POST /api/settings/reset/{key} —— 将单个设置项重置为默认值
# 需要 settings:manage 权限；该项不存在（重置数量 count==0）时返回 404
@router.post("/reset/{key}", response_model=SettingResetResponse)
async def reset_setting(
    key: str,
    _: TokenPayload = Depends(require_permissions("settings:manage")),
    service: SettingsService = Depends(get_settings_service),
):
    """Reset single setting to default value"""
    count = await service.reset(key)
    if count == 0:
        raise HTTPException(status_code=404, detail="Setting not found")
    return SettingResetResponse(
        message=f"Setting {key} reset to default",
        reset_count=count,
    )

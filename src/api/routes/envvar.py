"""
Environment Variable API router

提供用户环境变量的 CRUD 接口，环境变量加密存储，在沙箱创建时注入。
"""

import re

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import require_permissions
from src.infra.envvar.storage import EnvVarStorage
from src.infra.envvar.sync import sync_envvar_change
from src.infra.logging import get_logger
from src.kernel.schemas.envvar import (
    EnvVarBulkUpdateRequest,
    EnvVarBulkUpdateResponse,
    EnvVarCreate,
    EnvVarListResponse,
    EnvVarResponse,
    EnvVarUpdate,
)
from src.kernel.schemas.user import TokenPayload

logger = get_logger(__name__)

# 环境变量路由：挂载在 /api/env-vars，管理用户自定义环境变量
# value 加密存储，仅在创建沙箱时解密注入；列表接口对 value 做掩码脱敏
router = APIRouter()

# 环境变量 key 格式校验
# 合法的 shell 环境变量名：必须以字母或下划线开头，后续仅允许字母、数字、下划线
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# FastAPI 依赖：构造并注入环境变量存储实例（负责敏感值的加密持久化与读取）
async def get_envvar_storage() -> EnvVarStorage:
    return EnvVarStorage()


# 校验环境变量 key 是否符合命名规范，不合法则抛出 400（避免写入非法变量名）
def _validate_key(key: str) -> None:
    if not _ENV_KEY_PATTERN.match(key):
        raise HTTPException(
            status_code=400,
            detail="Invalid key format. Must match: ^[A-Za-z_][A-Za-z0-9_]*$",
        )


# ==========================================
# Static routes (before dynamic {key} routes)
# ==========================================


# GET /api/env-vars —— 列出当前用户的所有环境变量（响应体 EnvVarListResponse）
# 需要 envvar:read 权限；响应中的 value 会被掩码脱敏（不返回明文敏感值）
@router.get("", response_model=EnvVarListResponse)
async def list_env_vars(
    user: TokenPayload = Depends(require_permissions("envvar:read")),
    storage: EnvVarStorage = Depends(get_envvar_storage),
):
    """列出当前用户所有环境变量（value 掩码）"""
    variables = await storage.list_vars(user.sub)
    return EnvVarListResponse(variables=variables, count=len(variables))


# POST /api/env-vars —— 创建一个环境变量（请求体 EnvVarCreate: key/value）
# 需要 envvar:write 权限；value 加密后存储；写入后触发同步以刷新下游
@router.post("", response_model=EnvVarResponse, status_code=201)
async def create_env_var(
    data: EnvVarCreate,
    user: TokenPayload = Depends(require_permissions("envvar:write")),
    storage: EnvVarStorage = Depends(get_envvar_storage),
):
    """创建环境变量"""
    try:
        # 加密写入该用户的环境变量（key 冲突或非法时由 storage 抛 ValueError）
        result = await storage.set_var(user.sub, data.key, data.value)
        # 同步变更：通知运行中的沙箱/下游在下次注入时使用最新值
        await sync_envvar_change(user.sub)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# PUT /api/env-vars/bulk —— 批量设置环境变量（请求体 EnvVarBulkUpdateRequest.variables: dict）
# 需要 envvar:write 权限；先逐个校验 key 合法性，再整体加密写入并同步
@router.put("/bulk", response_model=EnvVarBulkUpdateResponse)
async def bulk_update_env_vars(
    data: EnvVarBulkUpdateRequest,
    user: TokenPayload = Depends(require_permissions("envvar:write")),
    storage: EnvVarStorage = Depends(get_envvar_storage),
):
    """批量设置环境变量"""
    # 校验所有 key 格式
    for key in data.variables:
        _validate_key(key)

    try:
        # 批量加密写入，返回实际更新的数量
        count = await storage.set_vars_bulk(user.sub, data.variables)
        # 同步变更，通知下游刷新环境变量
        await sync_envvar_change(user.sub)
        return EnvVarBulkUpdateResponse(
            updated_count=count,
            message=f"Updated {count} environment variable(s)",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# DELETE /api/env-vars/all —— 删除当前用户的全部环境变量
# 需要 envvar:delete 权限；返回删除数量并触发同步
@router.delete("/all")
async def delete_all_env_vars(
    user: TokenPayload = Depends(require_permissions("envvar:delete")),
    storage: EnvVarStorage = Depends(get_envvar_storage),
):
    """删除当前用户所有环境变量"""
    count = await storage.delete_all_vars(user.sub)
    await sync_envvar_change(user.sub)
    return {"message": f"Deleted {count} environment variable(s)"}


# ==========================================
# Dynamic routes (with path parameters)
# ==========================================


# GET /api/env-vars/{key} —— 获取单个环境变量（返回解密后的明文 value）
# 需要 envvar:read 权限；不存在时返回 404
# 注意：与列表接口不同，此处返回明文值，供前端编辑等场景使用
@router.get("/{key}", response_model=EnvVarResponse)
async def get_env_var(
    key: str,
    user: TokenPayload = Depends(require_permissions("envvar:read")),
    storage: EnvVarStorage = Depends(get_envvar_storage),
):
    """获取单个环境变量（明文）"""
    result = await storage.get_var(user.sub, key)
    if not result:
        raise HTTPException(status_code=404, detail=f"Environment variable '{key}' not found")
    return result


# PUT /api/env-vars/{key} —— 更新指定 key 的环境变量值（请求体 EnvVarUpdate.value）
# 需要 envvar:write 权限；先校验 key 格式，再加密写入并同步；key 由路径提供
@router.put("/{key}", response_model=EnvVarResponse)
async def update_env_var(
    key: str,
    data: EnvVarUpdate,
    user: TokenPayload = Depends(require_permissions("envvar:write")),
    storage: EnvVarStorage = Depends(get_envvar_storage),
):
    """更新环境变量"""
    _validate_key(key)
    try:
        # 加密写入更新后的值（key 来自路径参数）
        result = await storage.set_var(user.sub, key, data.value)
        # 同步变更，通知下游刷新
        await sync_envvar_change(user.sub)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# DELETE /api/env-vars/{key} —— 删除指定 key 的环境变量
# 需要 envvar:delete 权限；不存在时返回 404，删除成功后触发同步
@router.delete("/{key}")
async def delete_env_var(
    key: str,
    user: TokenPayload = Depends(require_permissions("envvar:delete")),
    storage: EnvVarStorage = Depends(get_envvar_storage),
):
    """删除单个环境变量"""
    deleted = await storage.delete_var(user.sub, key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Environment variable '{key}' not found")
    await sync_envvar_change(user.sub)
    return {"message": f"Environment variable '{key}' deleted"}

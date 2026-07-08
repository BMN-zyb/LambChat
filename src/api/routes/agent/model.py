"""
Model 配置路由

提供 Model 配置管理接口（CRUD）：
- 列出所有模型
- 获取单个模型
- 创建模型
- 更新模型
- 删除模型
- 批量导入模型
"""

from fastapi import APIRouter, Body, Depends, HTTPException

from src.api.deps import require_permissions
from src.infra.agent.model_storage import get_model_storage
from src.infra.logging import get_logger
from src.kernel.schemas.model import (
    AvailableModelListResponse,
    ModelConfig,
    ModelConfigCreate,
    ModelConfigUpdate,
    ModelListResponse,
    ModelResponse,
    mask_api_key,
    to_available_model,
)
from src.kernel.schemas.user import TokenPayload
from src.kernel.types import Permission

router = APIRouter()
logger = get_logger(__name__)
MODEL_BATCH_MAX_ITEMS = 200


# 批量操作的数量上限保护：当条目数超过 MODEL_BATCH_MAX_ITEMS(200) 时抛 413，防止一次性处理过多模型。
def _reject_oversized_model_batch(count: int) -> None:
    if count > MODEL_BATCH_MAX_ITEMS:
        raise HTTPException(
            status_code=413,
            detail=f"Cannot process more than {MODEL_BATCH_MAX_ITEMS} models at once",
        )


# ============================================
# CRUD 接口
# ============================================


# GET /api/agent/models/ —— 列出全部模型配置（管理员接口，需 MODEL_ADMIN 权限）。
# 查询参数 include_disabled 控制是否包含已禁用模型；响应含总数与启用数，且返回的 api_key 会被脱敏（mask_api_key）。
@router.get("/", response_model=ModelListResponse)
async def list_models(
    include_disabled: bool = False,
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """获取所有模型配置（仅管理员）"""
    storage = get_model_storage()
    # 读取模型列表，并统计总数/启用数
    models = await storage.list_models(include_disabled=include_disabled)
    counts = await storage.count()

    # 响应中对每个模型的 api_key 做脱敏处理，避免泄露密钥
    return ModelListResponse(
        models=[mask_api_key(m) for m in models],
        count=counts["total"],
        enabled_count=counts["enabled"],
    )


# GET /api/agent/models/available —— 获取当前用户"可用"的模型（需 AGENT_READ 权限，普通用户即可）。
# 仅返回已启用的模型，并按用户角色授权过滤；只暴露公开字段（to_available_model，不含密钥）。
@router.get("/available", response_model=AvailableModelListResponse)
async def list_available_models(
    user: TokenPayload = Depends(require_permissions(Permission.AGENT_READ.value)),
):
    """获取当前用户可用模型（仅启用模型，按角色授权过滤，返回公开字段）"""
    logger.info("[Model] list_available_models called")
    storage = get_model_storage()

    from src.infra.agent.model_access import resolve_user_allowed_model_ids

    # 依据用户角色解析其被授权的模型 id 集合；None 表示"不限制"（可见全部已启用模型）
    allowed_model_ids = await resolve_user_allowed_model_ids(user)
    # 有授权白名单时按 id/value 过滤；否则返回全部已启用模型
    if allowed_model_ids is not None:
        models = await storage.list_enabled_by_ids_or_values(allowed_model_ids)
    else:
        models = await storage.list_models(include_disabled=False)

    logger.info(f"[Model] Found {len(models)} visible models for user_id={user.sub}")

    from src.infra.llm.models_service import select_default_model

    # 从可见模型中挑选一个默认模型，供前端预选
    default_model = select_default_model([m.model_dump() for m in models])

    return AvailableModelListResponse(
        models=[to_available_model(m) for m in models],
        count=len(models),
        enabled_count=len(models),
        default_model_id=default_model.get("id") if default_model else None,
    )


# GET /api/agent/models/{model_id} —— 获取单个模型配置（管理员接口，需 MODEL_ADMIN 权限）。
# 不存在则抛 404；返回的 api_key 已脱敏。
@router.get("/{model_id}", response_model=ModelResponse)
async def get_model(
    model_id: str,
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """获取单个模型配置"""
    storage = get_model_storage()
    model = await storage.get(model_id)

    # 模型不存在则抛 404
    if not model:
        from src.kernel.exceptions import NotFoundError

        raise NotFoundError(f"Model '{model_id}' not found")

    return ModelResponse(model=mask_api_key(model))


# POST /api/agent/models/ —— 创建新模型配置（管理员接口，需 MODEL_ADMIN 权限），成功返回 201。
# 请求体为 ModelConfigCreate；若 id 重复（唯一键冲突）则转换为友好的校验错误。创建后使模型服务缓存失效。
@router.post("/", response_model=ModelResponse, status_code=201)
async def create_model(
    model_create: ModelConfigCreate,
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """创建新模型配置"""
    storage = get_model_storage()

    # 用请求体构造完整的 ModelConfig
    model = ModelConfig(**model_create.model_dump())
    # 写入数据库；捕获唯一键冲突（duplicate key），转换为更友好的校验错误
    try:
        created = await storage.create(model)
    except Exception as e:
        if "duplicate key" in str(e).lower():
            from src.kernel.exceptions import ValidationError

            raise ValidationError(f"Model with id '{model.id}' already exists")
        raise

    logger.info(f"[Model] Created model: {created.value} (id={created.id})")

    # 使 models_service 缓存失效
    from src.infra.llm.models_service import invalidate_cache

    await invalidate_cache()

    return ModelResponse(model=mask_api_key(created), message="Model created successfully")


# GET /api/agent/models/providers/list —— 列出所有支持的 LLM 供应商（本路由未声明额外权限依赖）。
# 数据来自 PROVIDER_REGISTRY，每项含 value(标识)、protocol(协议)、prefixes(模型名前缀)。
@router.get("/providers/list")
async def list_providers():
    """列出所有支持的 LLM 供应商（从 PROVIDER_REGISTRY 生成）。"""
    from src.infra.llm.client import PROVIDER_REGISTRY

    providers = []
    for slug, (protocol, prefixes) in PROVIDER_REGISTRY.items():
        providers.append(
            {
                "value": slug,
                "protocol": protocol,
                "prefixes": prefixes,
            }
        )
    return providers


# PUT /api/agent/models/reorder —— 批量调整模型显示顺序（管理员接口，需 MODEL_ADMIN 权限）。
# 请求体为按新顺序排列的 model id 列表；超过批量上限会被拒绝；完成后使缓存失效。
@router.put("/reorder", response_model=ModelListResponse)
async def reorder_models(
    model_ids: list[str] = Body(..., description="Model IDs in new order"),
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """批量更新模型顺序"""
    # 数量上限保护
    _reject_oversized_model_batch(len(model_ids))
    storage = get_model_storage()

    models = await storage.reorder(model_ids)

    logger.info(f"[Model] Reordered {len(models)} models")

    # 使 models_service 缓存失效
    from src.infra.llm.models_service import invalidate_cache

    await invalidate_cache()

    return ModelListResponse(
        models=[mask_api_key(m) for m in models],
        count=len(models),
        enabled_count=sum(1 for m in models if m.enabled),
    )


# PUT /api/agent/models/{model_id} —— 更新模型配置（管理员接口，需 MODEL_ADMIN 权限）。
# 请求体仅含需变更字段（exclude_none）；支持传空串 "" 显式清空 api_key；并校验 fallback_model 合法性。完成后使缓存失效。
@router.put("/{model_id}", response_model=ModelResponse)
async def update_model(
    model_id: str,
    model_update: ModelConfigUpdate,
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """更新模型配置"""
    storage = get_model_storage()

    # 检查模型是否存在
    existing = await storage.get(model_id)
    if not existing:
        from src.kernel.exceptions import NotFoundError

        raise NotFoundError(f"Model '{model_id}' not found")

    # 执行更新
    # 仅保留请求中显式提供（非 None）的字段，避免未提供字段被误覆盖为 None
    update_data = {k: v for k, v in model_update.model_dump(exclude_none=True).items()}
    # Allow clearing api_key by sending empty string ""
    if "api_key" in update_data and update_data["api_key"] == "":
        update_data["api_key"] = None
    # 校验 fallback_model
    if "fallback_model" in update_data and update_data["fallback_model"] is not None:
        # 不允许模型把自己设为兜底模型（fallback），否则会形成自引用
        if update_data["fallback_model"] == model_id:
            from src.kernel.exceptions import ValidationError

            raise ValidationError("A model cannot be its own fallback")
        # 兜底模型必须真实存在
        fallback_exists = await storage.get(update_data["fallback_model"])
        if not fallback_exists:
            from src.kernel.exceptions import ValidationError

            raise ValidationError(
                f"Fallback model '{update_data['fallback_model']}' does not exist"
            )
    updated = await storage.update(model_id, update_data)

    if not updated:
        from src.kernel.exceptions import NotFoundError

        raise NotFoundError(f"Model '{model_id}' not found during update")

    logger.info(f"[Model] Updated model: {updated.value} (id={updated.id})")

    # 使 models_service 缓存失效
    from src.infra.llm.models_service import invalidate_cache

    await invalidate_cache()

    return ModelResponse(model=mask_api_key(updated), message="Model updated successfully")


# DELETE /api/agent/models/{model_id} —— 删除模型配置（管理员接口，需 MODEL_ADMIN 权限），成功返回 204。
# 删除后会级联清理：把其他模型中指向它的 fallback_model 置空，并从所有角色的模型授权中移除，最后使缓存失效。
@router.delete("/{model_id}", status_code=204)
async def delete_model(
    model_id: str,
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """删除模型配置"""
    storage = get_model_storage()

    # 检查模型是否存在
    existing = await storage.get(model_id)
    if not existing:
        from src.kernel.exceptions import NotFoundError

        raise NotFoundError(f"Model '{model_id}' not found")

    model_value = existing.value
    await storage.delete(model_id)

    logger.info(f"[Model] Deleted model: {model_value} (id={model_id})")

    # 清理所有模型中被删模型作为 fallback_model 的孤儿引用
    from src.infra.utils.datetime import utc_now_iso

    collection = storage._get_collection()
    clear_result = await collection.update_many(
        {"fallback_model": model_id},
        {"$set": {"fallback_model": None, "updated_at": utc_now_iso()}},
    )
    if clear_result.modified_count:
        logger.info(
            f"[Model] Cleared orphaned fallback_model refs in {clear_result.modified_count} model(s)"
        )

    # 同步清理所有角色中关联的该模型（按 model_id 移除）
    from src.infra.agent.config_storage import get_agent_config_storage

    agent_storage = get_agent_config_storage()
    affected = await agent_storage.remove_model_from_all_roles(model_id)
    if affected:
        logger.info(f"[Model] Removed deleted model '{model_id}' from {affected} role(s)")

    # 使 models_service 缓存失效
    from src.infra.llm.models_service import invalidate_cache

    await invalidate_cache()

    return None


# POST /api/agent/models/{model_id}/toggle —— 启用或禁用某个模型（管理员接口，需 MODEL_ADMIN 权限）。
# 查询参数 enabled 为目标状态（true 启用 / false 禁用）；不存在则抛 404；完成后使缓存失效。
@router.post("/{model_id}/toggle", response_model=ModelResponse)
async def toggle_model(
    model_id: str,
    enabled: bool,
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """启用/禁用模型"""
    storage = get_model_storage()

    model = await storage.toggle(model_id, enabled)
    if not model:
        from src.kernel.exceptions import NotFoundError

        raise NotFoundError(f"Model '{model_id}' not found")

    action = "enabled" if enabled else "disabled"
    logger.info(f"[Model] {action.capitalize()} model: {model.value} (id={model.id})")

    # 使 models_service 缓存失效
    from src.infra.llm.models_service import invalidate_cache

    await invalidate_cache()

    return ModelResponse(model=mask_api_key(model), message=f"Model {action} successfully")


# POST /api/agent/models/import —— 批量导入模型（管理员接口，需 MODEL_ADMIN 权限）。
# 按 value 做 upsert（存在则更新、不存在则插入）；超过批量上限会被拒绝；完成后使缓存失效。
@router.post("/import", response_model=ModelListResponse)
async def import_models(
    models: list[ModelConfigCreate],
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """批量导入模型（upsert）"""
    _reject_oversized_model_batch(len(models))
    storage = get_model_storage()

    config_models = [ModelConfig(**m.model_dump()) for m in models]
    imported = await storage.bulk_upsert_by_value(config_models)

    logger.info(f"[Model] Imported {len(imported)} models")

    # 使 models_service 缓存失效
    from src.infra.llm.models_service import invalidate_cache

    await invalidate_cache()

    counts = await storage.count()
    return ModelListResponse(
        models=[mask_api_key(m) for m in imported],
        count=counts["total"],
        enabled_count=counts["enabled"],
    )


# POST /api/agent/models/batch-create —— 批量创建共享同一套连接配置的模型（管理员接口，需 MODEL_ADMIN 权限），成功返回 201。
# Body 分两部分：shared 为公共配置（api_base/api_key/provider/temperature 等），models 为各模型的 value/label 列表。
@router.post("/batch-create", response_model=ModelListResponse, status_code=201)
async def batch_create_models(
    body: dict = Body(...),
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """批量创建模型（共享 api_base、api_key 等配置）

    Body:
        {
            "shared": { "api_base": "...", "api_key": "...", ... },
            "models": [{ "value": "...", "label": "..." }, ...]
        }
    """
    from src.kernel.schemas.model import ModelProfile

    # 拆出公共配置 shared 与待创建模型列表 models
    shared = body.get("shared", {})
    models = body.get("models", [])

    # models 必须是非空列表，否则报 400
    if not models or not isinstance(models, list):
        raise HTTPException(status_code=400, detail="models must be a non-empty list")
    _reject_oversized_model_batch(len(models))

    # Validate provider if provided
    raw_provider = shared.get("provider")
    provider = None
    if raw_provider:
        if not isinstance(raw_provider, str) or not raw_provider.strip():
            raise HTTPException(
                status_code=400,
                detail=f"Invalid provider '{raw_provider}'. Must be a non-empty string.",
            )
        provider = raw_provider.strip()

    storage = get_model_storage()

    # Build profile from shared config
    profile = None
    if shared.get("max_input_tokens"):
        profile = ModelProfile(max_input_tokens=int(shared["max_input_tokens"]))

    created_models = []
    try:
        # 逐个创建：缺少 value 或 label 的条目直接跳过
        for item in models:
            if not item.get("value") or not item.get("label"):
                continue
            model = ModelConfig(
                value=item["value"],
                label=item["label"],
                provider=provider,
                api_key=shared.get("api_key") or None,
                api_base=shared.get("api_base") or None,
                temperature=shared.get("temperature"),
                max_tokens=shared.get("max_tokens"),
                profile=profile,
                enabled=True,
            )
            created = await storage.create(model)
            created_models.append(created)
            logger.debug(f"[Model] Batch created model: {created.value}")
    finally:
        # Always invalidate cache, even on partial failure
        from src.infra.llm.models_service import invalidate_cache

        await invalidate_cache()

    logger.info(f"[Model] Batch created {len(created_models)} models")

    counts = await storage.count()
    return ModelListResponse(
        models=[mask_api_key(m) for m in created_models],
        count=counts["total"],
        enabled_count=counts["enabled"],
    )


# DELETE /api/agent/models/ —— 删除所有模型配置（管理员接口，需 MODEL_ADMIN 权限，危险操作），成功返回 204。
# 会同步清空所有角色的模型授权关联，并使模型服务缓存失效。
@router.delete("/", status_code=204)
async def delete_all_models(
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """删除所有模型配置（危险操作）"""
    storage = get_model_storage()

    count = await storage.delete_all()

    logger.warning(f"[Model] Deleted all {count} models")

    # 同步清空所有角色的模型关联（单次批量操作）
    from src.infra.agent.config_storage import get_agent_config_storage

    agent_storage = get_agent_config_storage()
    affected = await agent_storage.clear_all_role_models()
    if affected:
        logger.info(f"[Model] Cleared models in {affected} role(s)")

    # 使 models_service 缓存失效
    from src.infra.llm.models_service import invalidate_cache

    await invalidate_cache()

    return None

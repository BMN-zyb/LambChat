"""Generic channel configuration API router.

Provides endpoints for managing per-user channel configurations.
Supports multiple channel types and multiple instances per channel type.
"""

import inspect

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_current_user_required, require_permissions
from src.infra.agent.config_storage import get_agent_config_storage
from src.infra.async_utils.blocking import run_blocking_io
from src.infra.channel.channel_storage import ChannelStorage
from src.infra.channel.pubsub import publish_channel_config_changed
from src.infra.channel.registry import get_registry
from src.infra.logging import get_logger
from src.infra.role.storage import RoleStorage
from src.kernel.exceptions import AuthorizationError, NotFoundError
from src.kernel.schemas.channel import (
    ChannelConfigCreate,
    ChannelConfigResponse,
    ChannelConfigStatus,
    ChannelConfigUpdate,
    ChannelListResponse,
    ChannelType,
    ChannelTypeListResponse,
)
from src.kernel.schemas.user import TokenPayload
from src.kernel.types import Permission

logger = get_logger(__name__)

# 本模块挂载于 /api/channels，负责用户的外部渠道（如飞书 Feishu）配置管理：
# 渠道类型元数据查询、飞书一键注册，以及各渠道实例的增删改查、状态查询与连接测试。
# 配置通过 ChannelStorage 持久化；敏感字段（如密钥）在返回前统一脱敏为 "***"。
# 权限基于 Permission.CHANNEL_READ / CHANNEL_WRITE / CHANNEL_DELETE 控制。
router = APIRouter()
# 单次列表返回的渠道配置数量上限；超过则返回 413，避免一次性拉取过多
CHANNEL_LIST_MAX_ITEMS = 200


# FastAPI 依赖：为各路由提供 ChannelStorage 实例（渠道配置的持久化访问层）。
async def get_channel_storage() -> ChannelStorage:
    """Dependency to get ChannelStorage"""
    return ChannelStorage()


# 校验用户是否有权把该渠道绑定到指定 agent：
# 1) agent 必须全局启用；2) 若用户角色配置了 allowed_agents 白名单，则该 agent 必须在白名单内。
async def _validate_agent_id(agent_id: str | None, user: TokenPayload) -> None:
    """Validate that the user has permission to use the specified agent."""
    # 未指定 agent，无需校验
    if not agent_id:
        return

    agent_storage = get_agent_config_storage()

    # 校验 agent 是否全局启用
    # Check agent is globally enabled
    if not await agent_storage.is_agent_enabled(agent_id):
        raise HTTPException(status_code=400, detail=f"Agent '{agent_id}' is not available")

    # 校验 agent 是否在用户角色允许的范围内（汇总各角色的 allowed_agents）
    # Check agent is allowed for user's roles
    if user.roles:
        role_storage = RoleStorage()
        allowed = set()
        for role in await role_storage.get_by_names(user.roles):
            if role.allowed_agents:
                allowed.update(role.allowed_agents)
        if allowed and agent_id not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{agent_id}' is not allowed for your role",
            )


# 校验项目存在且归属当前用户（渠道可绑定到某个项目）。
async def _validate_project_id(project_id: str | None, user: TokenPayload) -> None:
    """Validate that the project exists and belongs to the current user."""
    if not project_id:
        return

    from src.infra.folder.storage import get_project_storage

    project_storage = get_project_storage()
    project = await project_storage.get_by_id(project_id, user.sub)
    if not project:
        raise HTTPException(status_code=400, detail=f"Project '{project_id}' does not exist")


# 校验所选人设预设对当前用户可见（存在且有权限）；管理员可见全部。
async def _validate_persona_preset_id(persona_preset_id: str | None, user: TokenPayload) -> None:
    """Validate that the selected persona preset is visible to the current user."""
    if not persona_preset_id:
        return

    from src.infra.persona_preset.manager import PersonaPresetManager

    try:
        await PersonaPresetManager().get_preset(
            persona_preset_id,
            user_id=user.sub,
            is_admin=Permission.PERSONA_PRESET_ADMIN in (user.permissions or []),
        )
    except NotFoundError:
        raise HTTPException(status_code=400, detail="Persona preset does not exist")
    except AuthorizationError:
        raise HTTPException(status_code=403, detail="Persona preset is not allowed")


# 判断某渠道实例是否已连接：优先用分布式检查（可能是协程，需 await），
# 否则回退到单机内存态检查。兼容 manager 是否实现分布式接口。
async def _is_manager_connected(manager, user_id: str, instance_id: str) -> bool:
    distributed_checker = getattr(manager, "is_connected_distributed", None)
    if callable(distributed_checker):
        result = distributed_checker(user_id, instance_id)
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)
    return bool(manager.is_connected(user_id, instance_id))


# GET /api/channels/types —— 列出所有可用渠道类型及其元数据（配置字段、能力等）。
# 权限：CHANNEL_READ。前端据此渲染"新建渠道"表单。
# 注意：本路由及 /feishu/* 等具体路径都声明在 /{channel_type} 之前，避免被路径参数吞掉。
@router.get(
    "/types",
    response_model=ChannelTypeListResponse,
    dependencies=[Depends(require_permissions(Permission.CHANNEL_READ))],
)
async def get_channel_types():
    """Get all available channel types with metadata"""
    registry = get_registry()
    metadata_list = registry.get_channel_metadata()
    return ChannelTypeListResponse(types=metadata_list)


# POST /api/channels/feishu/registrations —— 发起飞书应用"一键注册"会话。
# 权限：CHANNEL_WRITE。依赖 lark-oapi 的 register_app 能力，缺失时返回 400。
# 返回的会话信息不含 secret（include_secret=False）。
@router.post(
    "/feishu/registrations",
    dependencies=[Depends(require_permissions(Permission.CHANNEL_WRITE))],
)
async def start_feishu_registration():
    """Start a one-click Feishu app registration session."""
    try:
        from src.infra.channel.feishu.registration import start_registration

        session = await run_blocking_io(start_registration, timeout=5.0)
        return session.to_dict(include_secret=False)
    except ImportError as e:
        raise HTTPException(
            status_code=400,
            detail=f"lark-oapi register_app is unavailable: {e}",
        )


# GET /api/channels/feishu/registrations/{session_id} —— 轮询一键注册会话进度。
# 权限：CHANNEL_WRITE。仅当会话状态为 success 时才在响应中带上 secret。
@router.get(
    "/feishu/registrations/{session_id}",
    dependencies=[Depends(require_permissions(Permission.CHANNEL_WRITE))],
)
async def get_feishu_registration(session_id: str):
    """Poll a one-click Feishu app registration session."""
    from src.infra.channel.feishu.registration import get_registration

    session = await run_blocking_io(get_registration, session_id, timeout=5.0)
    if not session:
        raise HTTPException(status_code=404, detail="Registration session not found")
    return session.to_dict(include_secret=session.status == "success")


# DELETE /api/channels/feishu/registrations/{session_id} —— 取消一键注册会话。
# 权限：CHANNEL_WRITE。会话不存在返回 404。
@router.delete(
    "/feishu/registrations/{session_id}",
    dependencies=[Depends(require_permissions(Permission.CHANNEL_WRITE))],
)
async def cancel_feishu_registration(session_id: str):
    """Cancel a one-click Feishu app registration session."""
    from src.infra.channel.feishu.registration import cancel_registration

    if not await run_blocking_io(cancel_registration, session_id, timeout=5.0):
        raise HTTPException(status_code=404, detail="Registration session not found")
    return {"cancelled": True}


# GET /api/channels/ —— 列出当前用户已配置的所有渠道实例（跨类型）。
# 权限：CHANNEL_READ。配置数超过上限返回 413；敏感字段脱敏后返回；未知渠道类型跳过。
@router.get(
    "/",
    response_model=ChannelListResponse,
    dependencies=[Depends(require_permissions(Permission.CHANNEL_READ))],
)
async def list_user_channels(
    user: TokenPayload = Depends(get_current_user_required),
    storage: ChannelStorage = Depends(get_channel_storage),
):
    """List all configured channel instances for current user"""
    registry = get_registry()
    total_configs = await storage.count_user_configs(user.sub)
    if total_configs > CHANNEL_LIST_MAX_ITEMS:
        raise HTTPException(
            status_code=413,
            detail=f"Too many channel configurations to list at once (max {CHANNEL_LIST_MAX_ITEMS})",
        )
    configs = await storage.list_user_configs(user.sub)

    responses = []
    for config in configs:
        try:
            channel_type = ChannelType(config.get("channel_type"))
            metadata = registry.get_channel_class(channel_type)
            if metadata:
                meta = metadata.get_metadata()
                sensitive_fields = set()
                for field in meta.get("config_fields", []):
                    if field.get("sensitive"):
                        sensitive_fields.add(field["name"])

                # 脱敏：先剔除敏感字段，再把有值的敏感字段统一替换成 "***"（存在但不外泄）
                # Mask sensitive fields
                masked_config = {k: v for k, v in config.items() if k not in sensitive_fields}
                for field in sensitive_fields:
                    if config.get(field):
                        masked_config[field] = "***"

                responses.append(
                    ChannelConfigResponse(
                        id=config.get("instance_id", ""),
                        channel_type=channel_type,
                        name=config.get("name", ""),
                        user_id=user.sub,
                        enabled=config.get("enabled", True),
                        config=masked_config,
                        capabilities=meta.get("capabilities", []),
                        agent_id=config.get("agent_id"),
                        model_id=config.get("model_id"),
                        project_id=config.get("project_id"),
                        persona_preset_id=config.get("persona_preset_id"),
                        created_at=config.get("created_at"),
                        updated_at=config.get("updated_at"),
                    )
                )
        # 配置中的 channel_type 无法解析为已知枚举：跳过（可能是已下线的渠道类型）
        except ValueError:
            # Unknown channel type, skip
            continue

    return ChannelListResponse(channels=responses)


# GET /api/channels/{channel_type} —— 列出某一渠道类型下的所有实例。
# 权限：CHANNEL_READ。未知渠道类型返回 404，配置数超过上限返回 413；敏感字段同样脱敏。
@router.get(
    "/{channel_type}",
    response_model=ChannelListResponse,
    dependencies=[Depends(require_permissions(Permission.CHANNEL_READ))],
)
async def list_channel_instances(
    channel_type: ChannelType,
    user: TokenPayload = Depends(get_current_user_required),
    storage: ChannelStorage = Depends(get_channel_storage),
):
    """List all instances of a specific channel type"""
    registry = get_registry()
    channel_class = registry.get_channel_class(channel_type)
    if not channel_class:
        raise HTTPException(status_code=404, detail=f"Unknown channel type: {channel_type}")

    total_configs = await storage.count_user_configs_by_type(user.sub, channel_type)
    if total_configs > CHANNEL_LIST_MAX_ITEMS:
        raise HTTPException(
            status_code=413,
            detail=f"Too many channel configurations to list at once (max {CHANNEL_LIST_MAX_ITEMS})",
        )

    configs = await storage.list_user_configs_by_type(user.sub, channel_type)

    metadata = channel_class.get_metadata()
    responses = []
    for config in configs:
        sensitive_fields = set()
        for field in metadata.get("config_fields", []):
            if field.get("sensitive"):
                sensitive_fields.add(field["name"])

        # 脱敏：先剔除敏感字段，再把有值的敏感字段统一替换成 "***"
        # Mask sensitive fields
        masked_config = {k: v for k, v in config.items() if k not in sensitive_fields}
        for field in sensitive_fields:
            if config.get(field):
                masked_config[field] = "***"

        responses.append(
            ChannelConfigResponse(
                id=config.get("instance_id", ""),
                channel_type=channel_type,
                name=config.get("name", ""),
                user_id=user.sub,
                enabled=config.get("enabled", True),
                config=masked_config,
                capabilities=metadata.get("capabilities", []),
                agent_id=config.get("agent_id"),
                model_id=config.get("model_id"),
                project_id=config.get("project_id"),
                persona_preset_id=config.get("persona_preset_id"),
                created_at=config.get("created_at"),
                updated_at=config.get("updated_at"),
            )
        )

    return ChannelListResponse(channels=responses)


# GET /api/channels/{channel_type}/{instance_id} —— 获取单个渠道实例配置。
# 权限：CHANNEL_READ。未知类型或实例不存在返回 404；返回体由 storage 统一脱敏构造。
@router.get(
    "/{channel_type}/{instance_id}",
    response_model=ChannelConfigResponse,
    dependencies=[Depends(require_permissions(Permission.CHANNEL_READ))],
)
async def get_channel_instance(
    channel_type: ChannelType,
    instance_id: str,
    user: TokenPayload = Depends(get_current_user_required),
    storage: ChannelStorage = Depends(get_channel_storage),
):
    """Get a specific channel instance"""
    registry = get_registry()
    channel_class = registry.get_channel_class(channel_type)
    if not channel_class:
        raise HTTPException(status_code=404, detail=f"Unknown channel type: {channel_type}")

    config = await storage.get_config(user.sub, channel_type, instance_id)
    if not config:
        raise HTTPException(status_code=404, detail="Channel instance not found")

    metadata = channel_class.get_metadata()
    return storage.build_response_from_config(config, channel_type, user.sub, metadata)


# POST /api/channels/{channel_type} —— 新建一个渠道实例。权限：CHANNEL_WRITE。
# 请求体 ChannelConfigCreate（channel_type 需与路径一致、name 必填、config 及可选的
#   agent_id/model_id/project_id/team_id/persona_preset_id）。会校验角色的渠道数量上限、
#   agent/项目/人设权限；创建后热加载对应渠道客户端并广播配置变更事件。
@router.post(
    "/{channel_type}",
    response_model=ChannelConfigResponse,
    status_code=201,
    dependencies=[Depends(require_permissions(Permission.CHANNEL_WRITE))],
)
async def create_channel_instance(
    channel_type: ChannelType,
    data: ChannelConfigCreate,
    user: TokenPayload = Depends(get_current_user_required),
    storage: ChannelStorage = Depends(get_channel_storage),
):
    """Create a new channel instance"""
    # 请求体里的 channel_type 必须与路径参数一致
    if data.channel_type != channel_type:
        raise HTTPException(
            status_code=400,
            detail=f"Channel type mismatch: expected {channel_type}, got {data.channel_type}",
        )

    if not data.name or not data.name.strip():
        raise HTTPException(status_code=400, detail="Instance name is required")

    # 依据用户角色计算可创建渠道数量上限：取各角色中最严格（最小）的 max_channels
    # Check channel limit from user roles
    max_channels = None  # Default: no limit
    if user.roles:
        role_storage = RoleStorage()
        for role in await role_storage.get_by_names(user.roles):
            if role.limits and role.limits.max_channels is not None:
                # Get the minimum limit among all roles (most restrictive)
                if max_channels is None or role.limits.max_channels < max_channels:
                    max_channels = role.limits.max_channels

    if max_channels is not None and max_channels >= 0:
        existing_channel_count = await storage.count_user_configs(user.sub)
        if existing_channel_count >= max_channels:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum channel limit ({max_channels}) reached. Please delete an existing channel before creating a new one.",
            )

    registry = get_registry()
    channel_class = registry.get_channel_class(channel_type)
    if not channel_class:
        raise HTTPException(status_code=404, detail=f"Unknown channel type: {channel_type}")

    metadata = channel_class.get_metadata()

    # 逐项校验绑定对象的权限/存在性：agent、项目、人设预设
    # Validate agent_id against user permissions
    await _validate_agent_id(data.agent_id, user)
    await _validate_project_id(data.project_id, user)
    await _validate_persona_preset_id(data.persona_preset_id, user)

    try:
        config = await storage.create_config(
            user_id=user.sub,
            channel_type=channel_type,
            config=data.config,
            name=data.name.strip(),
            agent_id=data.agent_id,
            model_id=data.model_id,
            project_id=data.project_id,
            team_id=data.team_id,
            persona_preset_id=data.persona_preset_id,
        )

        # 若该渠道有 manager，则热加载客户端使新配置立即生效（失败仅告警，不影响创建）
        # Reload the channel client if manager exists
        manager_class = registry.get_manager_class(channel_type)
        if manager_class:
            try:
                manager = manager_class.get_instance()
                await manager.reload_user(user.sub, config.get("instance_id"))
            except Exception as e:
                logger.warning(f"Failed to reload {channel_type} client: {e}")

        # 通过 pubsub 广播"配置已变更"，通知其它进程/实例同步重载该渠道
        await publish_channel_config_changed(
            user_id=user.sub,
            channel_type=channel_type.value,
            channel_instance_id=config.get("instance_id"),
            action="created",
        )

        return await storage.get_response(
            user.sub, channel_type, config.get("instance_id"), metadata
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# PUT /api/channels/{channel_type}/{instance_id} —— 更新渠道实例。权限：CHANNEL_WRITE。
# 请求体 ChannelConfigUpdate。合并配置时保留原有敏感字段（前端传空即视为不改）；
# 对 agent_id/model_id/project_id/team_id/persona_preset_id 用 ... 哨兵区分"未提供"与"显式清空"。
# 更新后同样热加载渠道客户端并广播配置变更。
@router.put(
    "/{channel_type}/{instance_id}",
    response_model=ChannelConfigResponse,
    dependencies=[Depends(require_permissions(Permission.CHANNEL_WRITE))],
)
async def update_channel_instance(
    channel_type: ChannelType,
    instance_id: str,
    data: ChannelConfigUpdate,
    user: TokenPayload = Depends(get_current_user_required),
    storage: ChannelStorage = Depends(get_channel_storage),
):
    """Update a specific channel instance"""
    registry = get_registry()
    channel_class = registry.get_channel_class(channel_type)
    if not channel_class:
        raise HTTPException(status_code=404, detail=f"Unknown channel type: {channel_type}")

    metadata = channel_class.get_metadata()

    # Get existing config to merge with updates
    existing = await storage.get_config(user.sub, channel_type, instance_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Channel instance not found")

    # 合并配置：新值覆盖旧值；但敏感字段若前端传空，则保留数据库里的原值（避免误清空密钥）
    # Merge configs: keep existing values for empty sensitive fields
    merged_config = {**existing, **data.config}
    for field in metadata.get("config_fields", []):
        if field.get("sensitive") and not data.config.get(field["name"]):
            # Keep existing value for empty sensitive fields
            merged_config[field["name"]] = existing.get(field["name"])

    # 以下字段用 ...（Ellipsis）作哨兵：仅当字段出现在 data.model_fields_set（即请求显式传了）时才更新，
    # 传了就用新值（可为 None，表示解绑），没传则保持 ...，由 storage 层识别并跳过、不动原值。
    # Validate agent_id if explicitly provided in the request
    agent_id_value: str | None = ...  # type: ignore[assignment]
    if "agent_id" in data.model_fields_set:
        await _validate_agent_id(data.agent_id, user)
        agent_id_value = data.agent_id
    else:
        agent_id_value = ...  # type: ignore[assignment]

    # Handle model_id with same ellipsis pattern
    model_id_value: str | None = ...  # type: ignore[assignment]
    if "model_id" in data.model_fields_set:
        model_id_value = data.model_id
    else:
        model_id_value = ...  # type: ignore[assignment]

    # Handle project_id with same ellipsis pattern
    project_id_value: str | None = ...  # type: ignore[assignment]
    if "project_id" in data.model_fields_set:
        await _validate_project_id(data.project_id, user)
        project_id_value = data.project_id
    else:
        project_id_value = ...  # type: ignore[assignment]

    # Handle team_id with same ellipsis pattern
    team_id_value: str | None = ...  # type: ignore[assignment]
    if "team_id" in data.model_fields_set:
        team_id_value = data.team_id
    else:
        team_id_value = ...  # type: ignore[assignment]

    # Handle persona_preset_id with same ellipsis pattern
    persona_preset_id_value: str | None = ...  # type: ignore[assignment]
    if "persona_preset_id" in data.model_fields_set:
        await _validate_persona_preset_id(data.persona_preset_id, user)
        persona_preset_id_value = data.persona_preset_id
    else:
        persona_preset_id_value = ...  # type: ignore[assignment]

    config = await storage.update_config(
        user_id=user.sub,
        channel_type=channel_type,
        config=merged_config,
        instance_id=instance_id,
        enabled=data.enabled,
        agent_id=agent_id_value,
        model_id=model_id_value,
        project_id=project_id_value,
        team_id=team_id_value,
        persona_preset_id=persona_preset_id_value,
    )

    if not config:
        raise HTTPException(status_code=404, detail="Channel instance not found")

    # 热加载渠道客户端，使更新后的配置立即生效（失败仅告警）
    # Reload the channel client
    manager_class = registry.get_manager_class(channel_type)
    if manager_class:
        try:
            manager = manager_class.get_instance()
            await manager.reload_user(user.sub, instance_id)
        except Exception as e:
            logger.warning(f"Failed to reload {channel_type} client: {e}")

    # 广播配置变更，通知其它进程/实例重载该渠道
    await publish_channel_config_changed(
        user_id=user.sub,
        channel_type=channel_type.value,
        channel_instance_id=instance_id,
        action="updated",
    )

    return await storage.get_response(user.sub, channel_type, instance_id, metadata)


# DELETE /api/channels/{channel_type}/{instance_id} —— 删除渠道实例。权限：CHANNEL_DELETE。
# 关键顺序：先删配置再停客户端——否则热加载会读到残留配置又把渠道重启起来。停客户端失败仅记错误日志。
@router.delete(
    "/{channel_type}/{instance_id}",
    dependencies=[Depends(require_permissions(Permission.CHANNEL_DELETE))],
)
async def delete_channel_instance(
    channel_type: ChannelType,
    instance_id: str,
    user: TokenPayload = Depends(get_current_user_required),
    storage: ChannelStorage = Depends(get_channel_storage),
):
    """Delete a specific channel instance"""
    registry = get_registry()
    channel_class = registry.get_channel_class(channel_type)
    if not channel_class:
        raise HTTPException(status_code=404, detail=f"Unknown channel type: {channel_type}")

    # Check if instance exists
    existing = await storage.get_config(user.sub, channel_type, instance_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Channel instance not found")

    # 先删除配置，再停止运行中的渠道客户端（顺序不能反，见函数说明）
    # Delete config first, then stop the running channel
    # (must delete before reload, otherwise reload sees the config and restarts it)
    deleted = await storage.delete_config(user.sub, channel_type, instance_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Channel instance not found")

    # Stop the channel client after config is removed
    manager_class = registry.get_manager_class(channel_type)
    if manager_class:
        try:
            manager = manager_class.get_instance()
            await manager.reload_user(user.sub, instance_id)
        except Exception as e:
            logger.error(
                f"Failed to stop {channel_type} client for user {user.sub}, instance {instance_id}. "
                f"The channel may still be running. Error: {e}"
            )

    # 广播删除事件，通知其它进程/实例停用该渠道
    await publish_channel_config_changed(
        user_id=user.sub,
        channel_type=channel_type.value,
        channel_instance_id=instance_id,
        action="deleted",
    )

    return {"message": "Channel instance deleted successfully"}


# GET /api/channels/{channel_type}/{instance_id}/status —— 查询渠道实例的连接状态。
# 权限：CHANNEL_READ。先取存储中的状态，再尝试用 manager 刷新实时的 connected 标志（失败仅告警）。
@router.get(
    "/{channel_type}/{instance_id}/status",
    response_model=ChannelConfigStatus,
    dependencies=[Depends(require_permissions(Permission.CHANNEL_READ))],
)
async def get_channel_instance_status(
    channel_type: ChannelType,
    instance_id: str,
    user: TokenPayload = Depends(get_current_user_required),
    storage: ChannelStorage = Depends(get_channel_storage),
):
    """Get connection status for a specific channel instance"""
    registry = get_registry()
    channel_class = registry.get_channel_class(channel_type)
    if not channel_class:
        raise HTTPException(status_code=404, detail=f"Unknown channel type: {channel_type}")

    # Check if instance exists
    config = await storage.get_config(user.sub, channel_type, instance_id)
    if not config:
        raise HTTPException(status_code=404, detail="Channel instance not found")

    status = await storage.get_status(user.sub, channel_type, instance_id)

    # 用渠道 manager 刷新实时连接状态，覆盖存储里的旧值
    # Update connection status from channel manager
    manager_class = registry.get_manager_class(channel_type)
    if manager_class:
        try:
            manager = manager_class.get_instance()
            connected = await _is_manager_connected(manager, user.sub, instance_id)
            status.connected = connected
        except Exception as e:
            logger.warning(
                "Failed to refresh %s channel status for user %s, instance %s: %s",
                channel_type.value,
                user.sub,
                instance_id,
                e,
            )

    return status


# POST /api/channels/{channel_type}/{instance_id}/test —— 测试渠道实例连接。
# 权限：CHANNEL_READ。实例不存在返回 404、被禁用返回 400；若当前未连接会先尝试
#   reload_user 启动一次再复检。返回 {success, message}。
@router.post(
    "/{channel_type}/{instance_id}/test",
    dependencies=[Depends(require_permissions(Permission.CHANNEL_READ))],
)
async def test_channel_instance_connection(
    channel_type: ChannelType,
    instance_id: str,
    user: TokenPayload = Depends(get_current_user_required),
    storage: ChannelStorage = Depends(get_channel_storage),
):
    """Test connection for a specific channel instance"""
    registry = get_registry()
    channel_class = registry.get_channel_class(channel_type)
    if not channel_class:
        raise HTTPException(status_code=404, detail=f"Unknown channel type: {channel_type}")

    config = await storage.get_config(user.sub, channel_type, instance_id)
    if not config:
        raise HTTPException(status_code=404, detail="Channel instance not found")

    if not config.get("enabled", True):
        raise HTTPException(status_code=400, detail="Channel instance is disabled")

    # 若未连接，先尝试启动（reload_user）再复检，据此返回是否连接成功
    # Check if connected; if not, attempt to start the channel
    manager_class = registry.get_manager_class(channel_type)
    if manager_class:
        try:
            manager = manager_class.get_instance()
            connected = await _is_manager_connected(manager, user.sub, instance_id)

            if not connected:
                await manager.reload_user(user.sub, instance_id)
                connected = await _is_manager_connected(manager, user.sub, instance_id)

            if connected:
                return {
                    "success": True,
                    "message": f"{channel_type} channel is connected",
                }
            else:
                return {
                    "success": False,
                    "message": f"{channel_type} channel is not connected. Check logs for errors.",
                }
        except Exception as e:
            return {"success": False, "message": str(e)}

    return {"success": False, "message": "Channel manager not available"}

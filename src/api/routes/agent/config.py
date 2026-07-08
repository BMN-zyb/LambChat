"""
Agent 配置路由

提供 Agent 配置管理接口：
- 全局 Agent 启用/禁用配置
- 角色可用的 Agents 映射
- 用户默认 Agent 设置
"""

from fastapi import APIRouter, Depends

from src.agents.core.base import AgentFactory, list_registered_agents
from src.api.deps import require_permissions
from src.infra.agent.config_storage import get_agent_config_storage
from src.infra.logging import get_logger
from src.infra.role.manager import get_role_manager
from src.kernel.schemas.agent import (
    AgentCatalogConfig,
    AgentCatalogConfigResponse,
    AgentCatalogConfigUpdate,
    AgentConfig,
    AgentConfigUpdate,
    GlobalAgentConfigResponse,
    RoleAgentAssignment,
    RoleAgentAssignmentResponse,
    RoleAgentAssignmentUpdate,
    RoleModelAssignment,
    RoleModelAssignmentUpdate,
    UserAgentPreference,
    UserAgentPreferenceResponse,
    UserAgentPreferenceUpdate,
)
from src.kernel.schemas.user import TokenPayload
from src.kernel.types import Permission

router = APIRouter()
logger = get_logger(__name__)


# 将"代码中注册的 Agent 运行时默认值"与"数据库中已持久化的目录元数据"合并，生成单个目录条目 AgentCatalogConfig。
# 合并策略：以注册信息（agent 字典）为基底，若存在已保存配置（saved）则用其覆盖 enabled/icon/sort_order/labels。
def _catalog_entry_from_registered(
    agent: dict,
    saved: AgentCatalogConfig | AgentConfig | None = None,
) -> AgentCatalogConfig:
    """Merge registered runtime defaults with persisted catalog metadata."""
    # 优先取已保存的排序值；若为 None，则在下方 sort_order 处回退到注册默认值
    saved_sort_order = getattr(saved, "sort_order", None) if saved else None
    # 逐字段合并：id/name/description 来自注册信息；enabled 默认 True；icon 默认 "Bot"；
    # sort_order 优先用已保存值、否则用注册默认（缺省 100）；labels 取已保存值、否则空字典
    return AgentCatalogConfig(
        id=agent["id"],
        name=agent.get("name") or agent["id"],
        description=agent.get("description") or "",
        enabled=saved.enabled if saved else True,
        icon=(getattr(saved, "icon", None) if saved else None) or "Bot",
        sort_order=saved_sort_order
        if saved_sort_order is not None
        else agent.get("sort_order", 100),
        labels=getattr(saved, "labels", {}) if saved else {},
    )


# 加载并规范化 Agent 展示目录配置：读取代码中注册的全部 Agent，与数据库已保存的目录配置按 id 合并，
# 排序后回写存储并返回。若尚无目录配置，则回退到旧版全局配置（get_global_config）作为已保存元数据来源。
async def _load_catalog_config() -> list[AgentCatalogConfig]:
    storage = get_agent_config_storage()
    # 通过注册机制发现的全部 Agent（运行时默认值来源）
    all_agents = AgentFactory.list_agents()
    # 数据库中已持久化的目录配置
    saved_configs = await storage.get_catalog_config()
    # 兼容旧数据：若还没有目录配置，则回退到旧的全局配置作为"已保存元数据"来源
    if not saved_configs and hasattr(storage, "get_global_config"):
        global_configs = await storage.get_global_config()
        saved_configs_map: dict[str, AgentCatalogConfig | AgentConfig] = {
            c.id: c for c in global_configs
        }
    else:
        # 正常路径：把已保存目录配置整理成 id -> 配置 的映射
        saved_configs_map = {c.id: c for c in saved_configs}

    # 以注册的全部 Agent 为准，逐个与已保存配置（按 id 匹配）合并成目录条目
    catalog = [
        _catalog_entry_from_registered(agent, saved_configs_map.get(agent["id"]))
        for agent in all_agents
    ]
    # 按 sort_order 升序、同序号再按 name 排序
    catalog.sort(key=lambda agent: (agent.sort_order, agent.name))
    # 将规范化后的目录回写存储，保证后续读取一致
    await storage.set_catalog_config(catalog)
    return catalog


# 将目录条目 AgentCatalogConfig 转换为旧版全局配置结构 AgentConfig（字段一一对应），用于兼容旧接口的响应体。
def _catalog_to_global_config(agent: AgentCatalogConfig) -> AgentConfig:
    return AgentConfig(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        enabled=agent.enabled,
        icon=agent.icon,
        sort_order=agent.sort_order,
        labels=agent.labels,
    )


# ============================================
# 管理员接口
# ============================================


# GET /api/agent/config/global —— 获取全局 Agent 配置（管理员接口，需 AGENT_ADMIN 权限）。
# 返回全部 Agent 的全局配置列表，以及其中已启用的 agent id 列表（available_agents）。
@router.get("/global", response_model=GlobalAgentConfigResponse)
async def get_global_agent_config(
    _: TokenPayload = Depends(require_permissions(Permission.AGENT_ADMIN.value)),
):
    """获取全局 Agent 配置"""
    # 加载规范化后的目录，再转换成旧版全局配置结构返回
    catalog = await _load_catalog_config()
    agent_configs = [_catalog_to_global_config(agent) for agent in catalog]

    return GlobalAgentConfigResponse(
        agents=agent_configs,
        available_agents=[a.id for a in agent_configs if a.enabled],
    )


# GET /api/agent/config/catalog —— 获取可配置的 Agent 展示目录（管理员接口，需 AGENT_ADMIN 权限）。
# 与 /global 数据同源，但返回目录结构（含 icon、labels 等展示元数据）。
@router.get("/catalog", response_model=AgentCatalogConfigResponse)
async def get_agent_catalog_config(
    _: TokenPayload = Depends(require_permissions(Permission.AGENT_ADMIN.value)),
):
    """获取可配置 Agent 展示目录。"""
    catalog = await _load_catalog_config()
    return AgentCatalogConfigResponse(
        agents=catalog,
        available_agents=[a.id for a in catalog if a.enabled],
    )


# PUT /api/agent/config/global —— 更新全局 Agent 配置（管理员接口，需 AGENT_ADMIN 权限）。
# 请求体 config_update.agents 为待保存的 Agent 配置列表；先逐个校验 id 是否已注册，再写入目录并返回最新配置。
@router.put("/global", response_model=GlobalAgentConfigResponse)
async def update_global_agent_config(
    config_update: AgentConfigUpdate,
    _: TokenPayload = Depends(require_permissions(Permission.AGENT_ADMIN.value)),
):
    """更新全局 Agent 配置"""
    storage = get_agent_config_storage()

    # 验证 agent IDs 是否已注册
    registered_ids = set(list_registered_agents())
    for agent in config_update.agents:
        if agent.id not in registered_ids:
            from src.kernel.exceptions import ValidationError

            raise ValidationError(f"Agent '{agent.id}' 未注册")

    # 构建 id -> 注册信息 的映射，供下面回填缺省字段（name/description/sort_order）使用
    registered_agents = {agent["id"]: agent for agent in AgentFactory.list_agents()}
    # 将请求配置转换为目录条目：未显式提供的 name/description/sort_order 回退到注册默认值，icon 缺省为 "Bot"
    catalog_agents = [
        AgentCatalogConfig(
            id=agent.id,
            name=agent.name or registered_agents[agent.id].get("name", agent.id),
            description=agent.description or registered_agents[agent.id].get("description", ""),
            enabled=agent.enabled,
            icon=agent.icon or "Bot",
            sort_order=agent.sort_order
            if agent.sort_order is not None
            else registered_agents[agent.id].get("sort_order", 100),
            labels=agent.labels,
        )
        for agent in config_update.agents
    ]
    # 持久化目录配置（覆盖式写入）
    await storage.set_catalog_config(catalog_agents)
    # 转换回旧版全局配置结构，构造响应
    agents = [_catalog_to_global_config(agent) for agent in catalog_agents]

    return GlobalAgentConfigResponse(
        agents=agents,
        available_agents=[a.id for a in agents if a.enabled],
    )


# PUT /api/agent/config/catalog —— 更新可配置 Agent 展示目录（管理员接口，需 AGENT_ADMIN 权限）。
# 与 /global 类似，但直接以目录结构（含 icon/labels）写入；同样先逐个校验 id 是否已注册。
@router.put("/catalog", response_model=AgentCatalogConfigResponse)
async def update_agent_catalog_config(
    config_update: AgentCatalogConfigUpdate,
    _: TokenPayload = Depends(require_permissions(Permission.AGENT_ADMIN.value)),
):
    """更新可配置 Agent 展示目录。"""
    storage = get_agent_config_storage()

    # 校验请求中每个 agent id 是否已注册
    registered_ids = set(list_registered_agents())
    for agent in config_update.agents:
        if agent.id not in registered_ids:
            from src.kernel.exceptions import ValidationError

            raise ValidationError(f"Agent '{agent.id}' 未注册")

    # id -> 注册信息 映射，用于回填缺省的 name/description
    registered_agents = {agent["id"]: agent for agent in AgentFactory.list_agents()}
    # 转换为目录条目（注意：此处 sort_order 直接采用请求值，不做注册默认回退）
    agents = [
        AgentCatalogConfig(
            id=agent.id,
            name=agent.name or registered_agents[agent.id].get("name", agent.id),
            description=agent.description or registered_agents[agent.id].get("description", ""),
            enabled=agent.enabled,
            icon=agent.icon or "Bot",
            sort_order=agent.sort_order,
            labels=agent.labels,
        )
        for agent in config_update.agents
    ]
    # 持久化目录配置（覆盖式写入）
    await storage.set_catalog_config(agents)

    return AgentCatalogConfigResponse(
        agents=agents,
        available_agents=[a.id for a in agents if a.enabled],
    )


# GET /api/agent/config/roles/{role_id} —— 获取指定角色被授权使用的 Agents（管理员接口，需 AGENT_ADMIN 权限）。
# 路径参数 role_id 为角色 ID；先校验角色存在，再返回该角色的 allowed_agents 列表（未配置时为空列表）。
@router.get("/roles/{role_id}", response_model=RoleAgentAssignment)
async def get_role_agents(
    role_id: str,
    _: TokenPayload = Depends(require_permissions(Permission.AGENT_ADMIN.value)),
):
    """获取角色的可用 Agents"""
    storage = get_agent_config_storage()
    role_manager = get_role_manager()

    # 校验角色是否存在，不存在则抛 404
    role = await role_manager.get_role(role_id)
    if not role:
        from src.kernel.exceptions import NotFoundError

        raise NotFoundError(f"角色 '{role_id}' 不存在")

    # 读取该角色被授权的 agent id 列表（未配置时回退为空列表）
    allowed_agents = await storage.get_role_agents(role_id) or []

    return RoleAgentAssignment(
        role_id=role_id,
        role_name=role.name,
        allowed_agents=allowed_agents,
    )


# PUT /api/agent/config/roles/{role_id} —— 设置指定角色可用的 Agents（管理员接口，需 AGENT_ADMIN 权限）。
# 请求体 assignment.allowed_agents 为该角色授权的 agent id 列表；先校验角色存在，写入后返回最新分配结果。
@router.put("/roles/{role_id}", response_model=RoleAgentAssignmentResponse)
async def update_role_agents(
    role_id: str,
    assignment: RoleAgentAssignmentUpdate,
    _: TokenPayload = Depends(require_permissions(Permission.AGENT_ADMIN.value)),
):
    """设置角色的可用 Agents"""
    storage = get_agent_config_storage()
    role_manager = get_role_manager()

    # 校验角色是否存在，不存在则抛 404
    role = await role_manager.get_role(role_id)
    if not role:
        from src.kernel.exceptions import NotFoundError

        raise NotFoundError(f"角色 '{role_id}' 不存在")

    # 写入角色-Agents 授权关系，返回持久化后的最新列表
    allowed_agents = await storage.set_role_agents(role_id, role.name, assignment.allowed_agents)

    return RoleAgentAssignmentResponse(
        role_id=role_id,
        role_name=role.name,
        allowed_agents=allowed_agents,
    )


# ============================================
# 角色 Models 管理
# ============================================


# GET /api/agent/config/roles/{role_id}/models —— 获取指定角色被授权使用的 Models（管理员接口，需 MODEL_ADMIN 权限）。
@router.get("/roles/{role_id}/models", response_model=RoleModelAssignment)
async def get_role_models(
    role_id: str,
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """获取角色的可用 Models"""
    storage = get_agent_config_storage()
    role_manager = get_role_manager()

    # 校验角色是否存在，不存在则抛 404
    role = await role_manager.get_role(role_id)
    if not role:
        from src.kernel.exceptions import NotFoundError

        raise NotFoundError(f"角色 '{role_id}' 不存在")

    # 读取该角色的模型白名单；None 表示从未配置（=不限制），非 None 表示已显式配置
    allowed_models = await storage.get_role_models(role_id)

    # configured 标志用于区分"未配置(None -> 不限制)"与"配置为空列表(-> 不允许任何模型)"；
    # allowed_models or [] 保证响应体中该字段始终为列表
    return RoleModelAssignment(
        role_id=role_id,
        role_name=role.name,
        allowed_models=allowed_models or [],
        configured=allowed_models is not None,
    )


# PUT /api/agent/config/roles/{role_id}/models —— 设置指定角色可用的 Models（管理员接口，需 MODEL_ADMIN 权限）。
# 请求体 assignment.allowed_models 为授权的模型列表；写入后 configured 恒为 True（表示已显式配置）。
@router.put("/roles/{role_id}/models", response_model=RoleModelAssignment)
async def update_role_models(
    role_id: str,
    assignment: RoleModelAssignmentUpdate,
    _: TokenPayload = Depends(require_permissions(Permission.MODEL_ADMIN.value)),
):
    """设置角色的可用 Models"""
    storage = get_agent_config_storage()
    role_manager = get_role_manager()

    # 校验角色是否存在，不存在则抛 404
    role = await role_manager.get_role(role_id)
    if not role:
        from src.kernel.exceptions import NotFoundError

        raise NotFoundError(f"角色 '{role_id}' 不存在")

    # 写入角色-Models 授权关系，返回持久化后的最新列表
    allowed_models = await storage.set_role_models(role_id, role.name, assignment.allowed_models)

    return RoleModelAssignment(
        role_id=role_id,
        role_name=role.name,
        allowed_models=allowed_models,
        configured=True,
    )


# ============================================
# 用户接口
# ============================================


# GET /api/agent/config/user/preference —— 获取当前用户的默认 Agent 偏好（需 agent:read 权限，普通用户即可）。
# 这是"用户偏好覆盖全局默认"机制的读取端：用户可设置自己的默认 Agent 以覆盖系统默认。
@router.get("/user/preference", response_model=UserAgentPreference)
async def get_user_preference(
    user: TokenPayload = Depends(require_permissions("agent:read")),
):
    """获取用户的默认 Agent 设置"""
    storage = get_agent_config_storage()
    # 按当前登录用户 id（user.sub）读取其偏好
    preference = await storage.get_user_preference(user.sub)

    # 未设置过偏好时返回 default_agent_id=None（由上层回退到系统默认 Agent）
    if not preference:
        return UserAgentPreference(default_agent_id=None)

    return preference


# PUT /api/agent/config/user/preference —— 设置当前用户的默认 Agent（需 agent:read 权限）。
# 请求体 preference.default_agent_id 为用户选择的默认 Agent id。
@router.put("/user/preference", response_model=UserAgentPreferenceResponse)
async def update_user_preference(
    preference: UserAgentPreferenceUpdate,
    user: TokenPayload = Depends(require_permissions("agent:read")),
):
    """设置用户的默认 Agent"""
    storage = get_agent_config_storage()
    # 以当前用户 id 为键写入其默认 Agent 偏好
    result = await storage.set_user_preference(user.sub, preference.default_agent_id)

    return UserAgentPreferenceResponse(
        default_agent_id=result.default_agent_id,
    )


# DELETE /api/agent/config/user/preference —— 清除当前用户的默认 Agent 偏好（需 agent:read 权限）。
# 删除后用户将回退到系统默认 Agent（响应中 default_agent_id 返回 None）。
@router.delete("/user/preference", response_model=UserAgentPreferenceResponse)
async def delete_user_preference(
    user: TokenPayload = Depends(require_permissions("agent:read")),
):
    """删除用户的默认 Agent 设置"""
    storage = get_agent_config_storage()
    # 按当前用户 id 删除其偏好记录
    await storage.delete_user_preference(user.sub)

    return UserAgentPreferenceResponse(default_agent_id=None)

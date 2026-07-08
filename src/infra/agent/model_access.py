"""Model access helpers shared by chat execution and public model listing."""

from __future__ import annotations

from src.kernel.schemas.user import TokenPayload

# 单个角色最多可映射的模型数量上限，防止 allowed 列表无限膨胀
ROLE_MODEL_ACCESS_LIMIT = 100


async def resolve_user_allowed_model_ids(user: TokenPayload) -> list[str] | None:
    """Return allowed model IDs for this user, or None when unrestricted.

    A missing role-model assignment means the role is not configured and remains
    unrestricted for backward compatibility. An existing assignment with an empty
    list means the role allows no models.
    """
    # 用户没有任何角色时不做限制，返回 None 表示"全部模型可用"
    if not user.roles:
        return None

    # 延迟导入：避免模块级循环依赖（config_storage / role.manager 反向依赖本模块）
    from src.infra.agent.config_storage import get_agent_config_storage
    from src.infra.role.manager import get_role_manager

    storage = get_agent_config_storage()
    role_manager = get_role_manager()
    # allowed 保存最终允许的模型 id（有序），seen 用于去重
    allowed: list[str] = []
    seen: set[str] = set()
    # 标记是否存在至少一个"已配置模型映射"的角色
    has_restricted_role = False

    # 遍历用户的所有角色，合并其允许的模型集合
    for role_name in user.roles:
        role = await role_manager.get_role_by_name(role_name)
        # 角色名在角色系统中不存在则跳过
        if not role:
            continue
        role_models = await storage.get_role_models(role.id)
        # role_models 为 None 表示该角色未配置映射 -> 向后兼容视为不受限，直接放行全部
        if role_models is None:
            return None
        has_restricted_role = True
        # 合并该角色的模型 id，去重并保持插入顺序
        for model_id in role_models:
            if model_id not in seen:
                seen.add(model_id)
                allowed.append(model_id)
                # 达到上限即提前返回，避免继续累积
                if len(allowed) >= ROLE_MODEL_ACCESS_LIMIT:
                    return allowed

    # 只要有一个角色做了限制就返回合并结果；否则视为不受限返回 None
    return allowed if has_restricted_role else None

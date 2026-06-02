"""Model access helpers shared by chat execution and public model listing."""

from __future__ import annotations

from src.kernel.schemas.user import TokenPayload

ROLE_MODEL_ACCESS_LIMIT = 100


async def resolve_user_allowed_model_ids(user: TokenPayload) -> list[str] | None:
    """Return allowed model IDs for this user, or None when unrestricted.

    A missing role-model assignment means the role is not configured and remains
    unrestricted for backward compatibility. An existing assignment with an empty
    list means the role allows no models.
    """
    if not user.roles:
        return None

    from src.infra.agent.config_storage import get_agent_config_storage
    from src.infra.role.manager import get_role_manager

    storage = get_agent_config_storage()
    role_manager = get_role_manager()
    allowed: list[str] = []
    seen: set[str] = set()
    has_restricted_role = False

    for role_name in user.roles:
        role = await role_manager.get_role_by_name(role_name)
        if not role:
            continue
        role_models = await storage.get_role_models(role.id)
        if role_models is None:
            return None
        has_restricted_role = True
        for model_id in role_models:
            if model_id not in seen:
                seen.add(model_id)
                allowed.append(model_id)
                if len(allowed) >= ROLE_MODEL_ACCESS_LIMIT:
                    return allowed

    return allowed if has_restricted_role else None

"""Team manager."""

import logging
from typing import Optional

from src.infra.persona_preset.manager import PersonaPresetManager
from src.infra.team.storage import TeamStorage
from src.kernel.exceptions import NotFoundError
from src.kernel.schemas.team import (
    TeamCreate,
    TeamListResponse,
    TeamMemberResponse,
    TeamPreferenceUpdate,
    TeamResponse,
    TeamUpdate,
)
from src.kernel.schemas.user import TokenPayload

logger = logging.getLogger(__name__)


class TeamManager:
    """Business logic for teams."""

    def __init__(
        self,
        storage: TeamStorage | None = None,
        persona_manager: PersonaPresetManager | None = None,
    ) -> None:
        # 依赖注入：允许测试传入 mock 的 storage/persona_manager，未传则用默认实现
        self.storage = storage or TeamStorage()
        self.persona_manager = persona_manager or PersonaPresetManager()

    # ── Internal helpers ──

    async def _hydrate_member_display_metadata(self, team: TeamResponse) -> TeamResponse:
        """Fill role_name, role_avatar, role_tags from persona presets."""
        # 团队成员存储时只落库 persona_preset_id，展示用的名称/头像/标签
        # 属于人设预设（persona preset）的实时属性，因此每次读取都要回填最新值，
        # 而不是在团队文档里保存一份可能过期的快照
        hydrated_members = []
        for member in team.members:
            try:
                preset = await self.persona_manager.storage.get_by_id(member.persona_preset_id)
                if preset:
                    member = TeamMemberResponse(
                        member_id=member.member_id,
                        persona_preset_id=member.persona_preset_id,
                        agent_id=member.agent_id,
                        model_id=member.model_id,
                        role_name=preset.get("name", member.role_name),
                        role_avatar=preset.get("avatar", member.role_avatar),
                        role_tags=preset.get("tags", member.role_tags),
                        role_instructions=member.role_instructions,
                        position=member.position,
                        enabled=member.enabled,
                    )
            except Exception:
                # 单个成员的人设预设查询失败（例如预设已被删除）不应影响其余成员，
                # 记录警告并保留该成员原有的（可能过期的）展示信息
                logger.warning(
                    "Failed to hydrate member %s (preset %s)",
                    member.member_id,
                    member.persona_preset_id,
                )
            hydrated_members.append(member)
        return team.model_copy(update={"members": hydrated_members})

    async def _validate_member_model_access(
        self,
        members: list,
        *,
        user: TokenPayload | None = None,
    ) -> None:
        """Validate optional per-member model overrides before persistence."""
        # 收集所有成员声明的自定义模型 id（去重），没有覆盖模型的成员直接跳过
        model_ids: list[str] = []
        seen: set[str] = set()
        for member in members:
            model_id = getattr(member, "model_id", None)
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            model_ids.append(model_id)

        if not model_ids:
            return

        from src.infra.agent.model_storage import get_model_storage

        storage = get_model_storage()
        models = {}
        for model_id in model_ids:
            model = await storage.get(model_id)
            # 模型不存在或已被禁用，都视为不可用，拒绝创建/更新
            if not model or not model.enabled:
                raise ValueError("team_member_model_unavailable")
            models[model_id] = model

        if user is None:
            # 无用户上下文（例如系统内部调用）时跳过"用户是否有权限使用该模型"的校验，
            # 只做上面的"模型是否存在且启用"校验
            return

        from src.infra.agent.model_access import resolve_user_allowed_model_ids

        allowed_model_ids = await resolve_user_allowed_model_ids(user)
        if allowed_model_ids is None:
            # None 表示该用户不受模型白名单限制（例如管理员），直接放行
            return

        allowed = set(allowed_model_ids)
        for model_id, model in models.items():
            # 同时比较 model_id 和 model.value，兼容"允许列表存的是内部 id 还是模型标识值"两种情况
            if model_id not in allowed and model.value not in allowed:
                raise ValueError("team_member_model_not_allowed")

    async def _validate_member_agent_access(
        self,
        members: list,
        *,
        user: TokenPayload | None = None,
    ) -> None:
        """Validate optional per-member agent mode overrides before persistence."""
        # 收集所有成员声明的自定义 agent 模式 id（去重）
        agent_ids: list[str] = []
        seen: set[str] = set()
        for member in members:
            agent_id = getattr(member, "agent_id", None)
            if not agent_id or agent_id in seen:
                continue
            seen.add(agent_id)
            agent_ids.append(agent_id)

        if not agent_ids:
            return

        # "team" agent 本身用于承载多 agent 团队编排，不允许团队成员自己再套一层 team，
        # 否则会产生递归嵌套
        if "team" in agent_ids:
            raise ValueError("team_member_agent_unavailable")

        from src.agents.core.base import AgentFactory

        registered_agent_ids = {agent["id"] for agent in AgentFactory.list_agents()}
        for agent_id in agent_ids:
            if agent_id not in registered_agent_ids:
                raise ValueError("team_member_agent_unavailable")

        # 若提供了用户上下文，进一步按用户所属角色计算出该用户实际可见/可用的 agent 集合，
        # 用于后续校验每个成员选用的 agent 是否在用户权限范围内
        role_ids: list[str] = []
        role_agent_map: dict[str, list[str] | None] = {}
        if user is not None:
            from src.infra.agent.config_storage import get_agent_config_storage
            from src.infra.role.manager import get_role_manager

            storage = get_agent_config_storage()
            role_manager = get_role_manager()
            for role_name in user.roles or []:
                role = await role_manager.get_role_by_name(role_name)
                if not role:
                    continue
                role_ids.append(role.id)
                role_agent_map[role.id] = await storage.get_role_agents(role.id)

        allowed_agents = await AgentFactory.get_filtered_agents(
            user_roles=role_ids,
            role_agent_map=role_agent_map,
        )
        allowed_ids = {agent["id"] for agent in allowed_agents}
        for agent_id in agent_ids:
            # 再次显式排除 "team"，双重防御，避免过滤逻辑变化后意外放行嵌套 team
            if agent_id not in allowed_ids or agent_id == "team":
                raise ValueError("team_member_agent_not_allowed")

    # ── CRUD ──

    async def create_team(
        self,
        team_data: TeamCreate,
        *,
        owner_user_id: str,
        user: TokenPayload | None = None,
    ) -> TeamResponse:
        """Create a new team."""
        # 创建前先校验每个成员的自定义模型/agent 是否可用、用户是否有权限使用，
        # 校验不通过会抛出 ValueError，中断创建流程
        await self._validate_member_model_access(team_data.members, user=user)
        await self._validate_member_agent_access(team_data.members, user=user)
        members_data = [m.model_dump(mode="json") for m in team_data.members]
        team = await self.storage.create_team(
            owner_user_id=owner_user_id,
            name=team_data.name,
            description=team_data.description,
            avatar=team_data.avatar,
            tags=team_data.tags,
            members=members_data,
            default_member_id=team_data.default_member_id,
            team_instructions=team_data.team_instructions,
            starter_prompts=[
                prompt.model_dump(mode="json") for prompt in team_data.starter_prompts
            ],
        )
        return await self._hydrate_member_display_metadata(team)

    async def get_team(
        self,
        team_id: str,
        *,
        owner_user_id: str,
    ) -> TeamResponse:
        """Get a team by ID."""
        team = await self.storage.get_team(team_id, owner_user_id=owner_user_id)
        if not team:
            raise NotFoundError("team_not_found")
        return await self._hydrate_member_display_metadata(team)

    async def list_teams(
        self,
        *,
        owner_user_id: str,
        skip: int = 0,
        limit: int = 100,
        favorite: bool | None = None,
        pinned: bool | None = None,
        q: str | None = None,
        tag: str | None = None,
    ) -> TeamListResponse:
        """List teams for an owner."""
        teams, total = await self.storage.list_teams(
            owner_user_id=owner_user_id,
            skip=skip,
            limit=limit,
            favorite=favorite,
            pinned=pinned,
            q=q,
            tag=tag,
        )
        # 列表中的每个团队都要单独回填成员展示信息（人设名称/头像/标签）
        hydrated = []
        for team in teams:
            hydrated.append(await self._hydrate_member_display_metadata(team))
        return TeamListResponse(teams=hydrated, total=total, skip=skip, limit=limit)

    async def update_preference(
        self,
        team_id: str,
        preference: TeamPreferenceUpdate,
        *,
        owner_user_id: str,
    ) -> TeamResponse:
        """Update the current user's favorite/pinned state for a team."""
        # 先确认团队存在（不存在会抛 NotFoundError），再更新用户维度的偏好，
        # 最后把偏好结果覆盖回团队响应对象上返回给调用方
        team = await self.get_team(team_id, owner_user_id=owner_user_id)
        update = preference.model_dump(mode="json")
        pref = await self.storage.update_user_preference(
            user_id=owner_user_id,
            team_id=team_id,
            update=update,
        )
        return team.model_copy(update=pref)

    async def update_team(
        self,
        team_id: str,
        team_data: TeamUpdate,
        *,
        owner_user_id: str,
        user: TokenPayload | None = None,
    ) -> TeamResponse:
        """Update a team."""
        # 只有当本次更新确实包含 members 字段时才需要重新校验模型/agent 访问权限
        if team_data.members is not None:
            await self._validate_member_model_access(team_data.members, user=user)
            await self._validate_member_agent_access(team_data.members, user=user)
        # exclude_unset=True：只序列化调用方显式传入的字段，未传字段不出现在 update 字典里，
        # 从而保证 storage 层"只更新传入字段"的语义生效
        update = team_data.model_dump(mode="json", exclude_unset=True)
        # Convert member models to dicts for storage
        if "members" in update and update["members"] is not None:
            update["members"] = [m if isinstance(m, dict) else m for m in update["members"]]
        team = await self.storage.update_team(
            team_id,
            owner_user_id=owner_user_id,
            update=update,
        )
        if not team:
            raise NotFoundError("team_not_found")
        return await self._hydrate_member_display_metadata(team)

    async def delete_team(
        self,
        team_id: str,
        *,
        owner_user_id: str,
    ) -> bool:
        """Delete a team."""
        deleted = await self.storage.delete_team(team_id, owner_user_id=owner_user_id)
        if not deleted:
            raise NotFoundError("team_not_found")
        return True

    async def clone_team(
        self,
        team_id: str,
        *,
        owner_user_id: str,
        new_name: str | None = None,
    ) -> TeamResponse:
        """Clone a team."""
        cloned = await self.storage.clone_team(
            team_id,
            owner_user_id=owner_user_id,
            new_name=new_name,
        )
        if not cloned:
            raise NotFoundError("team_not_found")
        return await self._hydrate_member_display_metadata(cloned)

    # ── Validation & resolution ──

    async def validate_team_members(
        self,
        team: TeamResponse,
    ) -> list[TeamMemberResponse]:
        """Return active members with validation. Logs warnings for missing presets."""
        # 只校验"已启用"的成员（active_members），已禁用的成员本来就不参与运行时编排；
        # 引用的人设预设若已被删除，跳过该成员但不影响团队其余成员正常使用
        validated = []
        for member in team.active_members:
            try:
                preset = await self.persona_manager.storage.get_by_id(member.persona_preset_id)
                if preset is None:
                    logger.warning(
                        "Member %s references missing preset %s",
                        member.member_id,
                        member.persona_preset_id,
                    )
                    continue
            except Exception:
                logger.warning(
                    "Failed to validate member %s (preset %s)",
                    member.member_id,
                    member.persona_preset_id,
                )
                continue
            validated.append(member)
        return validated

    async def resolve_team_for_runtime(
        self,
        team_id: str,
        *,
        owner_user_id: str,
    ) -> Optional[TeamResponse]:
        """Return team only if it exists and has active members."""
        # 供 agent 运行时使用：只有团队存在、且校验后至少还有一个有效成员时才返回，
        # 任何环节失败都返回 None，交由上层决定如何降级处理（而不是抛异常中断对话）
        try:
            team = await self.get_team(team_id, owner_user_id=owner_user_id)
        except NotFoundError:
            return None
        if not team.active_members:
            return None
        validated_members = await self.validate_team_members(team)
        if not validated_members:
            return None
        return team.model_copy(update={"members": validated_members})


_team_manager: Optional[TeamManager] = None


def get_team_manager() -> TeamManager:
    """Get singleton team manager."""
    # 进程内单例：避免每次请求都重新创建 TeamManager 及其内部依赖
    global _team_manager
    if _team_manager is None:
        _team_manager = TeamManager()
    return _team_manager

"""Persona preset manager."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 人设预设（persona preset）的业务门面层（Manager），位于 API 路由与存储层
# PersonaPresetStorage 之间，集中承载"权限判定 + 业务规则"：
#   - 作用域与权限：区分 USER（个人预设）与 GLOBAL（全局预设）两种 scope；
#     GLOBAL 仅管理员可创建/编辑，普通用户只能看到"已发布且公开"的全局预设；
#     查不到与无权限统一抛 NotFoundError，避免泄露资源是否存在。
#   - 与技能（skill）联动：预设保存一批 skill_names，但技能可能被删除/停用/
#     对该用户不可见，故 use_preset 生成运行时快照时会与"用户当前真正可用的
#     技能集合"求交集，缺失部分单独放入 missing_skill_names 供前端提示。
#   - 收藏/置顶等属于用户个人偏好，与预设本体解耦存储、读取时再合并返回。
# 通过 get_persona_preset_manager() 暴露进程级单例，避免每次请求重建存储连接。
# ============================================================================

from typing import Optional

from src.infra.persona_preset.storage import PersonaPresetStorage
from src.infra.skill.storage import SkillStorage
from src.infra.utils.datetime import utc_now
from src.kernel.exceptions import AuthorizationError, NotFoundError
from src.kernel.schemas.persona_preset import (
    PersonaPreset,
    PersonaPresetCreate,
    PersonaPresetScope,
    PersonaPresetSnapshot,
    PersonaPresetStatus,
    PersonaPresetUpdate,
    PersonaPresetVisibility,
)


class PersonaPresetManager:
    """Business logic for persona presets."""

    def __init__(
        self,
        storage: PersonaPresetStorage | None = None,
        skill_storage: SkillStorage | None = None,
    ) -> None:
        # storage: 人设预设自身的持久化层；skill_storage: 用于校验预设绑定的 skill 是否仍对用户可用。
        self.storage = storage or PersonaPresetStorage()
        self.skill_storage = skill_storage or SkillStorage()

    @staticmethod
    def _can_view(doc: dict, *, user_id: str, is_admin: bool) -> bool:
        # 可见性规则：
        # 1) USER 范围（个人预设）只有所有者本人可见；
        # 2) GLOBAL 范围（全局预设）对管理员始终可见；
        # 3) 对普通用户，GLOBAL 预设需同时满足“公开可见”且“已发布”才可见。
        if doc.get("scope") == PersonaPresetScope.USER.value:
            owner_user_id = doc.get("owner_user_id")
            if owner_user_id:
                return owner_user_id == user_id
            return doc.get("created_by") == user_id
        if is_admin:
            return doc.get("scope") == PersonaPresetScope.GLOBAL.value
        return (
            doc.get("scope") == PersonaPresetScope.GLOBAL.value
            and doc.get("visibility") == PersonaPresetVisibility.PUBLIC.value
            and doc.get("status") == PersonaPresetStatus.PUBLISHED.value
        )

    @staticmethod
    def _can_edit(doc: dict, *, user_id: str, is_admin: bool) -> bool:
        # 编辑权限规则：GLOBAL 预设仅管理员可编辑；USER 预设仅所有者本人可编辑。
        if doc.get("scope") == PersonaPresetScope.GLOBAL.value:
            return is_admin
        owner_user_id = doc.get("owner_user_id")
        if owner_user_id:
            return owner_user_id == user_id
        return doc.get("created_by") == user_id

    async def create_preset(
        self,
        preset_data: PersonaPresetCreate,
        *,
        user_id: str,
        is_admin: bool,
    ) -> PersonaPreset:
        # 只有管理员才允许创建 GLOBAL（全局）范围的预设，避免普通用户发布“系统级”人设。
        if preset_data.scope == PersonaPresetScope.GLOBAL and not is_admin:
            raise AuthorizationError("persona_preset_no_admin_permission")

        now = utc_now()
        data = preset_data.model_dump(mode="json")
        data.update(
            {
                # GLOBAL 预设不属于任何个人，owner_user_id 置空；USER 预设归属当前用户。
                "owner_user_id": None
                if preset_data.scope == PersonaPresetScope.GLOBAL
                else user_id,
                "version": 1,
                "usage_count": 0,
                "created_by": user_id,
                "updated_by": user_id,
                "created_at": now,
                "updated_at": now,
            }
        )
        created = await self.storage.create(data)
        return PersonaPreset(**created)

    async def batch_create_presets(
        self,
        items: list[PersonaPresetCreate],
        *,
        user_id: str,
        is_admin: bool,
    ) -> list[PersonaPreset]:
        # 批量创建：逐条校验权限，非管理员尝试创建 GLOBAL 预设的条目会被静默跳过而非报错。
        now = utc_now()
        docs = []
        for item in items:
            if item.scope == PersonaPresetScope.GLOBAL and not is_admin:
                continue
            data = item.model_dump(mode="json")
            data.update(
                {
                    "owner_user_id": None if item.scope == PersonaPresetScope.GLOBAL else user_id,
                    "version": 1,
                    "usage_count": 0,
                    "created_by": user_id,
                    "updated_by": user_id,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            docs.append(data)
        if not docs:
            return []
        inserted = await self.storage.insert_many(docs)
        return [PersonaPreset(**doc) for doc in inserted]

    async def get_preset(self, preset_id: str, *, user_id: str, is_admin: bool) -> PersonaPreset:
        # 查询单个预设；查不到或权限不足统一抛出 NotFoundError（不区分“不存在”与“无权限”，避免信息泄露）。
        doc = await self.storage.get_by_id(preset_id)
        if not doc or not self._can_view(doc, user_id=user_id, is_admin=is_admin):
            raise NotFoundError("persona_preset_not_found")
        return PersonaPreset(**doc)

    async def list_presets(
        self,
        *,
        user_id: str,
        is_admin: bool = False,
        scope: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        q: str | None = None,
        favorite: bool | None = None,
        pinned: bool | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[PersonaPreset]:
        # 列表查询委托给存储层的 list_visible，按可见性规则过滤 + 支持标签/关键字/收藏/置顶筛选与分页。
        docs = await self.storage.list_visible(
            user_id=user_id,
            include_admin=is_admin,
            scope=scope,
            status=status,
            tag=tag,
            q=q,
            favorite=favorite,
            pinned=pinned,
            skip=skip,
            limit=limit,
        )
        return [PersonaPreset(**doc) for doc in docs]

    async def update_preference(
        self,
        preset_id: str,
        *,
        user_id: str,
        is_admin: bool,
        is_favorite: bool | None = None,
        is_pinned: bool | None = None,
    ) -> PersonaPreset:
        # 收藏/置顶属于“用户个人偏好”，与预设本体解耦存储；先确认预设可见，再更新偏好并合并回预设对象返回。
        preset = await self.get_preset(preset_id, user_id=user_id, is_admin=is_admin)
        preference = await self.storage.update_user_preference(
            user_id=user_id,
            preset_id=preset_id,
            update={
                "is_favorite": is_favorite,
                "is_pinned": is_pinned,
            },
        )
        return preset.model_copy(update=preference)

    async def count_presets(
        self,
        *,
        user_id: str,
        is_admin: bool = False,
        scope: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        q: str | None = None,
        favorite: bool | None = None,
        pinned: bool | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> int:
        # 统计符合筛选条件的预设总数（用于分页），skip/limit 对计数无意义，直接丢弃。
        del skip, limit
        return await self.storage.count_visible(
            user_id=user_id,
            include_admin=is_admin,
            scope=scope,
            status=status,
            tag=tag,
            q=q,
            favorite=favorite,
            pinned=pinned,
        )

    async def update_preset(
        self,
        preset_id: str,
        preset_data: PersonaPresetUpdate,
        *,
        user_id: str,
        is_admin: bool,
    ) -> PersonaPreset:
        # 更新前先做存在性与编辑权限校验；仅传入的字段会被写入（exclude_unset）。
        doc = await self.storage.get_by_id(preset_id)
        if not doc:
            raise NotFoundError("persona_preset_not_found")
        if not self._can_edit(doc, user_id=user_id, is_admin=is_admin):
            raise AuthorizationError("persona_preset_no_edit_permission")

        update = preset_data.model_dump(mode="json", exclude_unset=True)
        target_scope = update.get("scope")
        # 若本次更新要把预设切换为 GLOBAL 范围，必须是管理员，并清空 owner_user_id；
        # 若切换为 USER 范围，则归属改为当前操作者。
        if target_scope == PersonaPresetScope.GLOBAL.value:
            if not is_admin:
                raise AuthorizationError("persona_preset_no_admin_permission")
            update["owner_user_id"] = None
        elif target_scope == PersonaPresetScope.USER.value:
            update["owner_user_id"] = user_id

        # 每次更新版本号自增，供 use_preset 时的调用方感知预设是否发生变化。
        update["version"] = int(doc.get("version", 1)) + 1
        update["updated_by"] = user_id
        updated = await self.storage.update(preset_id, update)
        if not updated:
            raise NotFoundError("persona_preset_not_found")
        return PersonaPreset(**updated)

    async def delete_preset(self, preset_id: str, *, user_id: str, is_admin: bool) -> bool:
        # 删除前同样校验编辑权限（编辑权限与删除权限规则一致）。
        doc = await self.storage.get_by_id(preset_id)
        if not doc:
            raise NotFoundError("persona_preset_not_found")
        if not self._can_edit(doc, user_id=user_id, is_admin=is_admin):
            raise AuthorizationError("persona_preset_no_delete_permission")
        return await self.storage.delete(preset_id)

    async def copy_preset(
        self,
        preset_id: str,
        *,
        user_id: str,
        is_admin: bool,
    ) -> PersonaPreset:
        # “复制为我的预设”：基于任意可见的源预设（包括全局预设）创建一份属于当前用户的私有草稿副本，
        # 不继承 usage_count 等运行时统计，同时记录 source_preset_id/copied_from_version 便于追溯来源。
        source = await self.get_preset(preset_id, user_id=user_id, is_admin=is_admin)
        now = utc_now()
        copied_data = {
            "scope": PersonaPresetScope.USER.value,
            "owner_user_id": user_id,
            "name": source.name,
            "description": source.description,
            "avatar": source.avatar,
            "tags": source.tags,
            "system_prompt": source.system_prompt,
            "starter_prompts": [
                prompt.model_dump(mode="json") for prompt in source.starter_prompts
            ],
            "skill_names": source.skill_names,
            "visibility": PersonaPresetVisibility.PRIVATE.value,
            "status": PersonaPresetStatus.DRAFT.value,
            "source_preset_id": source.id,
            "copied_from_version": source.version,
            "version": 1,
            "usage_count": 0,
            "created_by": user_id,
            "updated_by": user_id,
            "created_at": now,
            "updated_at": now,
        }
        created = await self.storage.create(copied_data)
        return PersonaPreset(**created)

    async def use_preset(
        self,
        preset_id: str,
        *,
        user_id: str,
        is_admin: bool,
    ) -> PersonaPresetSnapshot:
        # “应用该预设”：生成一份即时快照供前端会话使用。
        # 关键点：预设里记录的 skill_names 不能直接照单全收，因为 skill 可能已被删除/下线/对该用户不再可见，
        # 所以要与当前用户实际可用的 skill 集合做交集，缺失的部分单独返回 missing_skill_names 供前端提示。
        preset = await self.get_preset(preset_id, user_id=user_id, is_admin=is_admin)
        available = await self._get_available_skill_names(user_id)
        skill_names = [name for name in preset.skill_names if name in available]
        missing = [name for name in preset.skill_names if name not in available]

        # 记录一次使用：累加使用计数、更新“最近使用”偏好（用于排序/推荐）。
        await self.storage.increment_usage(preset_id)
        await self.storage.touch_user_preference(user_id=user_id, preset_id=preset_id)
        return PersonaPresetSnapshot(
            preset_id=preset.id,
            name=preset.name,
            system_prompt=preset.system_prompt,
            starter_prompts=preset.starter_prompts,
            skill_names=skill_names,
            missing_skill_names=missing,
            version=preset.version,
            avatar=preset.avatar,
        )

    async def _get_available_skill_names(self, user_id: str) -> set[str]:
        """Return skill names that can actually be loaded for this user."""
        # 优先使用更精确的 get_effective_skills（若 SkillStorage 提供该接口），
        # 它返回的是经过启用状态等过滤后“真正生效”的技能集合；否则回退到列出用户全部技能名。
        get_effective_skills = getattr(self.skill_storage, "get_effective_skills", None)
        if get_effective_skills is not None:
            effective = await get_effective_skills(user_id)
            if isinstance(effective, dict):
                skills = effective.get("skills")
                if isinstance(skills, dict):
                    return set(skills.keys())
                return set(effective.keys())

        return set(await self.skill_storage.get_all_user_skill_names(user_id))

    async def close(self) -> None:
        # 级联关闭依赖的存储层连接。
        await self.storage.close()
        await self.skill_storage.close()


# 进程级单例，避免每次请求都新建 Manager（及其内部的存储连接）。
_persona_preset_manager: Optional[PersonaPresetManager] = None


def get_persona_preset_manager() -> PersonaPresetManager:
    """Get singleton persona preset manager."""
    # 惰性初始化单例。
    global _persona_preset_manager
    if _persona_preset_manager is None:
        _persona_preset_manager = PersonaPresetManager()
    return _persona_preset_manager


async def close_persona_preset_manager() -> None:
    # 应用关闭时释放单例及其底层连接资源。
    global _persona_preset_manager
    manager = _persona_preset_manager
    _persona_preset_manager = None
    if manager is not None:
        await manager.close()

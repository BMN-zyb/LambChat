"""
Skill 管理器

门面类，封装 SkillStorage 操作。
"""

from typing import Optional

from src.infra.skill.storage import SkillStorage
from src.infra.user.storage import UserStorage
from src.kernel.config import settings


class SkillManager:
    """Skill 管理器"""

    def __init__(self, user_id: Optional[str] = None):
        # 业务门面：绑定当前用户；技能总开关关闭时不初始化存储层
        self.user_id = user_id
        self.storage = SkillStorage() if settings.ENABLE_SKILLS else None

    async def _get_disabled_skills(self) -> list[str]:
        """Get disabled_skills from user metadata"""
        # 禁用状态集中存放在用户 metadata.disabled_skills，而非技能记录本身
        if not self.user_id:
            return []
        try:
            user_storage = UserStorage()
            user_doc = await user_storage.get_by_id(self.user_id)
            if user_doc and user_doc.metadata:
                return user_doc.metadata.get("disabled_skills", [])
            return []
        except Exception:
            # 读取失败按“无禁用”处理，避免影响技能加载
            return []

    async def list_skills_async(self) -> list[dict]:
        """列出用户所有 Skills"""
        # 无用户或未启用技能则返回空列表
        if not self.user_id or not self.storage:
            return []
        try:
            # 传入禁用列表，让存储层据此计算每个技能的 enabled 标记
            disabled_skills = await self._get_disabled_skills()
            skills = await self.storage.list_user_skills(
                self.user_id, disabled_skills=disabled_skills
            )
            # 精简为前端/调用方所需的字段
            return [
                {
                    "name": s["skill_name"],
                    "enabled": s["enabled"],
                    "file_count": s["file_count"],
                    "installed_from": s.get("installed_from"),
                }
                for s in skills
            ]
        except Exception:
            return []

    async def get_skill_async(self, skill_name: str) -> Optional[dict]:
        """获取指定 Skill"""
        if not self.user_id or not self.storage:
            return None
        try:
            files = await self.storage.get_skill_files(skill_name, self.user_id)
            if not files:
                return None
            # Get metadata from __meta__ doc
            # 元信息取自 __meta__ 文档（安装来源等）
            meta = await self.storage.get_skill_meta(skill_name, self.user_id)
            # Compute enabled from disabled_skills
            # 启用状态由“是否在禁用集合中”反向推导
            disabled_skills = await self._get_disabled_skills()
            enabled = skill_name not in set(disabled_skills)
            return {
                "name": skill_name,
                "files": files,
                "enabled": enabled,
                "installed_from": meta.installed_from.value if meta else None,
            }
        except Exception:
            return None

    async def get_effective_skills(self) -> dict:
        """获取生效的 Skills"""
        # “生效技能”= 用户可见且未被禁用的技能集合，供 Agent 运行时加载
        if not self.user_id or not self.storage:
            return {}
        try:
            disabled_skills = await self._get_disabled_skills()
            result = await self.storage.get_effective_skills(
                self.user_id, disabled_skills=disabled_skills
            )
            return result.get("skills", {})
        except Exception:
            return {}

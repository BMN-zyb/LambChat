"""
技能注入模块

从数据库读取技能并注入到系统提示中。
支持用户级别的技能访问。

与 CompositeBackend 配合工作：
- CompositeBackend 自动处理 /skills/ 路径的读写
- LLM 可以直接通过 /skills/{skill_name}/ 读取技能文件
- LLM 写入 /skills/ 路径会自动更新 MongoDB
"""

from typing import Optional

from src.infra.logging import get_logger
from src.infra.skill.manager import SkillManager

logger = get_logger(__name__)


class SkillsMiddleware:
    """
    技能注入中间件

    从数据库读取技能内容，注入到 Agent 的系统提示中。
    支持用户级别的技能访问（系统技能 + 用户技能）。

    如果提供了 user_id，将使用用户级别的技能访问。
    """

    def __init__(self, user_id: Optional[str] = None):
        """
        初始化技能中间件

        Args:
            user_id: 用户 ID，用于获取用户级别的技能
        """
        # 记录用户身份，并据此构建技能管理器（决定可见哪些技能）
        self._user_id = user_id
        self._manager = SkillManager(user_id=user_id)

    async def inject_skills_async(self, system_prompt: str) -> str:
        """
        将技能内容注入到系统提示中（异步版本，包含 MongoDB）

        Args:
            system_prompt: 原始系统提示

        Returns:
            注入技能后的系统提示
        """
        # 先加载该用户生效的技能
        skills_content = await self.load_all_skills_async()

        # 无技能则原样返回，不改动系统提示
        if not skills_content:
            return system_prompt

        # 构建技能提示
        skills_prompt = await self._build_skills_prompt(skills_content)

        # 将技能插入到系统提示中
        # 优先替换占位符 {skills}，便于模板精确控制注入位置
        if "{skills}" in system_prompt:
            return system_prompt.replace("{skills}", skills_prompt)
        else:
            # 追加到系统提示末尾
            # 无占位符则直接追加到末尾
            return f"{system_prompt}\n\n{skills_prompt}"

    async def load_all_skills_async(self) -> list[dict]:
        """加载所有技能"""
        # 无用户身份无法确定技能可见范围，直接返回空
        if not self._user_id:
            logger.warning("No user_id provided, cannot load skills")
            return []

        try:
            # 取生效技能并统一转成 dict（兼容模型/对象/dict 三种来源）
            effective = await self._manager.get_effective_skills()
            skills = []
            for skill_name, skill in effective.items():
                if hasattr(skill, "model_dump"):
                    skill_dict = skill.model_dump()
                else:
                    skill_dict = dict(skill) if not isinstance(skill, dict) else skill
                # 确保 name 字段存在
                # 名称与 is_system 兜底
                skill_dict["name"] = skill_dict.get("name", skill_name)
                skill_dict["is_system"] = skill_dict.get("is_system", True)
                skills.append(skill_dict)
            # 仅返回启用的技能
            return [s for s in skills if s.get("enabled", True)]
        except Exception as e:
            # 加载失败降级为无技能，不阻断对话
            logger.warning(f"Failed to load skills for user {self._user_id}: {e}")
            return []

    async def _build_skills_prompt(self, skills: list[dict]) -> str:
        """
        Build skills prompt text with enhanced matching hints.

        Includes skill descriptions, usage triggers, and matching guidance
        to help the LLM select the most relevant skill for user queries.
        """
        if not skills:
            return ""

        # 提示头部：告知技能位置与读取方式，并统一主文件名为 SKILL.md
        lines = ["## Available Skills", ""]
        lines.append(
            "The following skills are available. Read skill files from `/skills/{skill_name}/` "
            "to get detailed instructions."
        )
        lines.append(
            "When creating or updating a skill's main instruction file, always use the canonical "
            "filename `SKILL.md` exactly; treat `skill.md`, `Skill.md`, and other case variants "
            "as `SKILL.md`."
        )
        lines.append("")

        # 逐个技能输出名称/描述/路径（描述用于 LLM 判断是否匹配当前任务）
        for skill in skills:
            name = skill.get("name", "unnamed skill")
            description = skill.get("description", "no description")

            lines.append(f"### {name}")
            lines.append(f"**Description**: {description}")
            lines.append(f"**Path**: `/skills/{name}/SKILL.md`")
            lines.append("")

        # 附上技能选择策略，引导 LLM 依意图匹配、按需读取、逐步执行
        lines.append("### Skill Selection Strategy")
        lines.append("1. Analyze the user's request for key intent and domain")
        lines.append("2. Match intent with skill descriptions above")
        lines.append("3. Read the skill's SKILL.md for detailed instructions")
        lines.append("4. Follow the skill's instructions step by step")
        lines.append("5. Save main skill instructions as `SKILL.md` exactly when editing skills")
        lines.append("6. If multiple skills might apply, ask the user to clarify")
        lines.append("")

        return "\n".join(lines)


def get_skills_middleware(
    user_id: Optional[str] = None,
) -> SkillsMiddleware:
    """
    获取技能中间件实例

    Args:
        user_id: 用户 ID，用于获取用户级别的技能
    """
    # 简单工厂：按 user_id 构建中间件实例
    return SkillsMiddleware(user_id=user_id)

"""
Fast Agent 上下文管理 - 无沙箱，支持工具和 Skills

不使用沙箱，但保留工具和技能支持。
"""

import uuid
from typing import TYPE_CHECKING, Any, List, Optional

from src.agents.core.tool_filter import (
    filter_disabled_tools,
    filter_mcp_tools_by_db_state,
    get_db_disabled_mcp_tool_names,
)
from src.infra.logging import get_logger
from src.infra.skill.manager import SkillManager
from src.infra.tool.human_tool import get_human_tool
from src.infra.tool.internal_registry import get_internal_tools_for_user
from src.infra.tool.mcp_global import get_global_mcp_tools
from src.infra.tool.reveal_file_tool import get_reveal_file_tool
from src.infra.tool.reveal_project_tool import get_reveal_project_tool
from src.infra.tool.transfer_file_tool import get_transfer_file_tool, get_transfer_path_tool
from src.kernel.config import settings

if TYPE_CHECKING:
    from src.infra.tool.deferred_manager import DeferredToolManager
    from src.infra.tool.mcp_client import MCPClientManager

logger = get_logger(__name__)


class FastAgentContext:
    """
    Fast Agent 上下文 - 无沙箱，支持工具和技能

    特点：
    - 不使用沙箱
    - 支持 Skills
    - 支持 MCP 工具
    """

    def __init__(
        self,
        session_id: str | None = None,
        agent_id: str = "fast",
        user_id: Optional[str] = None,
        disabled_tools: Optional[List[str]] = None,
        disabled_skills: Optional[List[str]] = None,
        enabled_skills: Optional[List[str]] = None,
        disabled_mcp_tools: Optional[List[str]] = None,
        auto_mode: bool = False,
    ):
        # session_id 缺省时自动生成 UUID，保证每个上下文实例可唯一定位工作区与 checkpoint。
        self.session_id = session_id or str(uuid.uuid4())
        self.agent_id = agent_id
        self.user_id = user_id
        # 以下四项为调用方（前端/API）下发的能力过滤名单，决定禁用或白名单启用哪些工具/技能/MCP 工具。
        self.disabled_tools = disabled_tools
        self.disabled_skills = disabled_skills
        self.enabled_skills = enabled_skills
        self.disabled_mcp_tools = disabled_mcp_tools
        # auto_mode：自动模式开关，透传给工具过滤逻辑（影响需人工确认类工具的放行策略）。
        self.auto_mode = auto_mode
        # MCP 客户端管理器：懒加载，首次取工具时才赋值；来自全局缓存，故 close() 不负责关闭它。
        self.mcp_manager: Optional[MCPClientManager] = None
        # 懒加载幂等标记：确保 MCP 工具只尝试加载一次，避免重复初始化的开销与副作用。
        self._mcp_loaded: bool = False
        # 已装配工具列表：setup() 先填充内置工具，懒加载时再追加 MCP 工具。
        self.tools: List[Any] = []
        # 已装配技能列表：每项为 dict 形态的技能定义（经白/黑名单过滤后保留）。
        self.skills: List[dict] = []
        # 延迟工具管理器：当 MCP 工具数量超过阈值时启用，用“工具搜索”按需暴露而非一次性全量注入。
        self.deferred_manager: Optional["DeferredToolManager"] = None

    def apply_skill_filters(self) -> None:
        """Apply whitelist/blacklist filters to loaded skills."""
        # 黑名单集合（禁用的技能名）；无论走哪条分支都要从结果中剔除。
        disabled_set = set(self.disabled_skills or [])
        # 白名单优先：一旦提供 enabled_skills，则只保留“在白名单内且不在黑名单内”的技能，
        # 白名单为空列表即表示全部禁用；提前 return，不再走下面的纯黑名单分支。
        if self.enabled_skills is not None:
            enabled_set = set(self.enabled_skills)
            self.skills = [
                s
                for s in self.skills
                if s.get("name") in enabled_set and s.get("name") not in disabled_set
            ]
            return

        # 未提供白名单时，仅按黑名单剔除被禁用技能。
        if disabled_set:
            self.skills = [s for s in self.skills if s.get("name") not in disabled_set]

    async def get_tools(self) -> List[Any]:
        """获取所有工具（懒加载 MCP 工具）"""
        await self._lazy_load_mcp_tools()
        return self.tools

    def filter_tools(self) -> List[Any]:
        """根据 disabled_tools 和 disabled_mcp_tools 过滤工具（使用共享过滤逻辑）"""
        # 委托共享过滤逻辑：剔除 disabled_tools / disabled_mcp_tools；auto_mode 会影响
        # 需人工确认类工具的放行。抽到公共函数是为了与其它 agent 的过滤行为保持一致。
        filtered = filter_disabled_tools(
            self.tools,
            disabled_tools=self.disabled_tools,
            disabled_mcp_tools=self.disabled_mcp_tools,
            auto_mode=self.auto_mode,
        )
        logger.debug(
            "[FastAgentContext] Tool filtering: %d/%d tools enabled (auto_mode=%s)",
            len(filtered),
            len(self.tools),
            self.auto_mode,
        )
        return filtered

    async def _lazy_load_mcp_tools(self) -> None:
        """懒加载 MCP 工具（仅在首次调用 get_tools 时初始化）"""
        # 幂等保护：无论上次是否加载成功，都不再重复尝试（成功或失败都只执行一次）。
        if self._mcp_loaded:
            return  # 已经尝试过加载

        # 先置标记再执行：即使下面中途抛异常，也不会二次进入加载流程。
        self._mcp_loaded = True

        # 全局开关关闭时直接跳过，不接入任何 MCP 工具。
        if not settings.ENABLE_MCP:
            logger.debug("[FastAgentContext] MCP is disabled (ENABLE_MCP=False)")
            return

        try:
            logger.info(f"[FastAgentContext] Lazy loading MCP tools for user {self.user_id}")
            # 使用全局缓存，避免重复初始化
            assert self.user_id is not None  # Already guarded above
            mcp_tools, self.mcp_manager = await get_global_mcp_tools(self.user_id)
            logger.info(f"[FastAgentContext] Loaded {len(mcp_tools)} MCP tools (before DB filter)")

            # 过滤数据库中标记为 system_disabled / user_disabled 的工具
            db_disabled = await get_db_disabled_mcp_tool_names(self.user_id)
            mcp_tools = filter_mcp_tools_by_db_state(mcp_tools, db_disabled)
            logger.info(
                f"[FastAgentContext] After DB filter: {len(mcp_tools)} MCP tools "
                f"(removed {len(db_disabled)} disabled names)"
            )

            # 按“暴露策略”把 MCP 工具拆成两类：inline（直接注入 prompt，模型可立即调用）
            # 与 deferred（延迟工具，先不进 prompt，靠工具搜索按需发现），依据来自各 server 的
            # _server_tool_policies 配置。这样能把默认可见的工具集控制得尽量小。
            from src.agents.core.mcp_tool_exposure import split_mcp_tools_for_exposure

            inline_mcp_tools, deferred_mcp_tools = split_mcp_tools_for_exposure(
                mcp_tools,
                getattr(self.mcp_manager, "_server_tool_policies", {}),
            )
            # 策略标记为 inline 的工具直接并入 self.tools，始终对模型可见。
            if inline_mcp_tools:
                self.tools.extend(inline_mcp_tools)
                logger.info(
                    "[FastAgentContext] Inlined %d MCP tool(s) by policy",
                    len(inline_mcp_tools),
                )

            # 仅当“已开启延迟加载 且 存在延迟工具 且 工具总数会超过阈值”时才启用延迟管理器：
            # 工具太多会撑大 system prompt、拖慢推理并稀释注意力，此时改用 DeferredToolManager +
            # 工具搜索按需暴露；未达阈值则没必要引入这层间接，直接全量内联更简单。
            if (
                settings.ENABLE_DEFERRED_TOOL_LOADING
                and deferred_mcp_tools
                and (len(self.tools) + len(deferred_mcp_tools)) > settings.DEFERRED_TOOL_THRESHOLD
            ):
                from src.infra.tool.deferred_manager import (
                    DeferredToolManager,
                    restore_discovered_tools,
                )

                # 恢复本会话此前已“搜索发现”的工具名，跨轮次保持这些工具持续可见。
                pre_discovered = await restore_discovered_tools(self.session_id)

                self.deferred_manager = DeferredToolManager(
                    all_deferred_tools=deferred_mcp_tools,
                    session_id=self.session_id,
                    disabled_tools=self.disabled_tools,
                    disabled_mcp_tools=self.disabled_mcp_tools,
                    pre_discovered_names=pre_discovered,
                    prompt_tool_limit=getattr(settings, "DEFERRED_TOOL_PROMPT_LIMIT", 40),
                )
                logger.info(
                    f"[FastAgentContext] Deferred {len(deferred_mcp_tools)} MCP tools "
                    f"(builtin={len(self.tools)}, threshold={settings.DEFERRED_TOOL_THRESHOLD}, "
                    f"pre_restored={len(pre_discovered)})"
                )
            # 未达阈值（或未启用延迟加载）：延迟工具也直接内联，全部对模型可见。
            else:
                self.tools.extend(deferred_mcp_tools)
        except Exception as e:
            logger.error(f"[FastAgentContext] Failed to load MCP tools: {e}", exc_info=True)

    async def setup(self) -> None:
        """初始化：工具 + 技能"""
        logger.info(
            f"[FastAgentContext] Starting setup, ENABLE_SKILLS={settings.ENABLE_SKILLS}, ENABLE_MCP={settings.ENABLE_MCP}"
        )

        # 基础工具
        # 以下五个是无论是否有沙箱都恒定加载的内置工具：
        # human（人机交互/征询用户）、reveal_file/reveal_project（向用户展示文件/项目）、
        # transfer_file/transfer_path（产物回传）。它们不依赖 MCP，故在 setup 阶段即注入。
        human_tool = get_human_tool(session_id=self.session_id)
        self.tools.append(human_tool)
        logger.info("[FastAgentContext] Added human tool")

        reveal_file_tool = get_reveal_file_tool()
        self.tools.append(reveal_file_tool)
        logger.info("[FastAgentContext] Added reveal_file tool")

        reveal_project_tool = get_reveal_project_tool()
        self.tools.append(reveal_project_tool)
        logger.info("[FastAgentContext] Added reveal_project tool")

        transfer_file_tool = get_transfer_file_tool()
        self.tools.append(transfer_file_tool)
        logger.info("[FastAgentContext] Added transfer_file tool")

        transfer_path_tool = get_transfer_path_tool()
        self.tools.append(transfer_path_tool)
        logger.info("[FastAgentContext] Added transfer_path tool")

        # 内部工具：需先解析用户的角色与是否管理员（resolve_user_mcp_access），
        # 再据此授权可见的内部工具集——不同角色/权限看到的工具不同。无 user_id 时按无权限处理。
        try:
            from src.infra.mcp.quota import resolve_user_mcp_access

            user_roles, is_admin = (
                await resolve_user_mcp_access(self.user_id) if self.user_id else ([], False)
            )
            internal_tools = await get_internal_tools_for_user(
                user_id=self.user_id,
                user_roles=user_roles,
                is_admin=is_admin,
            )
            self.tools.extend(internal_tools)
            logger.info(f"[FastAgentContext] Added {len(internal_tools)} internal tools")
        except Exception as e:
            logger.warning(f"[FastAgentContext] Failed to load internal tools: {e}")

        try:
            from src.infra.tool.env_var_tool import get_env_var_tools

            # 先收集已加载工具的名字集合，用于对环境变量工具按名去重，避免重复注入同名工具。
            existing_tool_names = {getattr(tool, "name", "") for tool in self.tools}
            env_var_tools = [
                tool
                for tool in get_env_var_tools()
                if getattr(tool, "name", "") not in existing_tool_names
            ]
            self.tools.extend(env_var_tools)
            logger.info(f"[FastAgentContext] Added {len(env_var_tools)} env var tools")
        except Exception as e:
            logger.warning(f"[FastAgentContext] Failed to load env var tools: {e}")

        # Memory 工具（原生 MongoDB 后端）
        if settings.ENABLE_MEMORY:
            try:
                from src.infra.memory.tools import get_all_memory_tools

                memory_tools = get_all_memory_tools()
                self.tools.extend(memory_tools)
                logger.info(f"[FastAgentContext] Added {len(memory_tools)} memory tools")
            except ImportError:
                logger.warning("[FastAgentContext] memory tools import failed, skipping")
            except Exception as e:
                logger.warning(f"[FastAgentContext] Failed to load memory tools: {e}")

        # MCP 工具延迟加载（不在 setup 时初始化）
        logger.info("[FastAgentContext] MCP tools will be lazy loaded on first use")

        # 沙箱 MCP 管理工具
        # 仅在全局开启沙箱时加载。注意 Fast Agent 自身 _supports_sandbox=False，
        # 常规部署下不会进入此分支；保留是为了与共享上下文实现兼容。
        if settings.ENABLE_SANDBOX:
            from src.infra.tool.sandbox_mcp_tool import get_sandbox_mcp_tools

            self.tools.extend(get_sandbox_mcp_tools())
            logger.info("[FastAgentContext] Added sandbox_mcp tools (sandbox mode)")

        # 加载技能（使用与 Search Agent 相同的方式，保持一致）
        if settings.ENABLE_SKILLS and self.user_id:
            try:
                manager = SkillManager(user_id=self.user_id)
                skills_data = await manager.get_effective_skills()
                # 把每个技能归一化为纯 dict（兼容 pydantic 模型/映射），默认视为系统技能（is_system=True），
                # 且只收录 enabled 的技能；随后再由 apply_skill_filters 施加白/黑名单。
                for skill_name, skill_data in skills_data.items():
                    skill_dict = (
                        skill_data.model_dump()
                        if hasattr(skill_data, "model_dump")
                        else (dict(skill_data) if not isinstance(skill_data, dict) else skill_data)
                    )
                    skill_dict["is_system"] = skill_dict.get("is_system", True)
                    if skill_dict.get("enabled", True):
                        self.skills.append(skill_dict)

                before_count = len(self.skills)
                self.apply_skill_filters()
                if self.enabled_skills is not None:
                    logger.info(
                        f"[FastAgentContext] Applied enabled_skills whitelist, {len(self.skills)}/{before_count} remaining"
                    )
                elif self.disabled_skills:
                    logger.info(
                        f"[FastAgentContext] Filtered out {len(self.disabled_skills)} disabled skills, {len(self.skills)} remaining"
                    )

                logger.info(
                    f"[FastAgentContext] Loaded {len(self.skills)} skills for user: {self.user_id}"
                )
            except Exception as e:
                logger.warning(f"[FastAgentContext] Failed to load skills: {e}")

        logger.info(f"[FastAgentContext] Setup complete, total {len(self.tools)} tools available")

    async def close(self) -> None:
        """清理（注意：不关闭 mcp_manager，因为它是全局缓存的）"""
        # mcp_manager 是全局缓存的，不应该在这里关闭
        # 它会在 mcp_global.py 的缓存过期/失效时自动清理
        self.mcp_manager = None

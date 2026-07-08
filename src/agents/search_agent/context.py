"""
Search Agent 上下文管理 - 支持工具和 Skills
"""

import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.agents.core.tool_filter import (
    filter_disabled_tools,
    filter_mcp_tools_by_db_state,
    get_db_disabled_mcp_tool_names,
)
from src.infra.logging import get_logger
from src.infra.skill import load_skill_files
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


class SearchAgentContext:
    """
    Search Agent 上下文 - 支持工具和技能

    特点：
    - 支持 Skills
    - 支持 MCP 工具
    """

    def __init__(
        self,
        session_id: str | None = None,
        agent_id: str = "search",
        user_id: Optional[str] = None,
        disabled_tools: Optional[List[str]] = None,
        disabled_skills: Optional[List[str]] = None,
        enabled_skills: Optional[List[str]] = None,
        disabled_mcp_tools: Optional[List[str]] = None,
        auto_mode: bool = False,
    ):
        """初始化上下文：记录会话 / 用户标识，以及各类工具、技能的启停配置。

        关键参数：
            session_id: 会话 ID（缺省时自动生成 uuid），后续用作 checkpoint 的 thread_id。
            agent_id: agent 标识，默认 "search"。
            user_id: 用户 ID，用于多租户隔离，以及按用户加载 MCP 工具 / 技能。
            disabled_tools / disabled_mcp_tools: 需要禁用的工具、MCP 工具名单。
            disabled_skills / enabled_skills: 技能黑 / 白名单（白名单存在时优先生效）。
            auto_mode: 自动模式开关，影响工具过滤策略。
        """
        self.session_id = session_id or str(uuid.uuid4())
        self.agent_id = agent_id
        self.user_id = user_id
        self.disabled_tools = disabled_tools
        self.disabled_skills = disabled_skills
        self.enabled_skills = enabled_skills
        self.disabled_mcp_tools = disabled_mcp_tools
        self.auto_mode = auto_mode
        # 以下为运行期内部状态（非构造参数）：
        # MCP 客户端管理器，懒加载 MCP 工具后被赋值
        self.mcp_manager: Optional[MCPClientManager] = None
        # 标记 MCP 工具是否已尝试加载，保证只懒加载一次
        self._mcp_loaded: bool = False
        # 已装配的工具列表（基础工具 + 内部工具 + MCP 工具等）
        self.tools: List[Any] = []
        # 已加载的技能元信息列表
        self.skills: List[dict] = []
        # 技能文件内容映射（路径 -> 数据）
        self.skill_files: Dict[str, Any] = {}
        # 延迟工具管理器：当工具总数超阈值时，改为按需检索式加载 MCP 工具
        self.deferred_manager: Optional["DeferredToolManager"] = None

    def apply_skill_filters(self) -> None:
        """Apply whitelist/blacklist filters to loaded skills and skill files."""
        # 先算出黑名单集合
        disabled_set = set(self.disabled_skills or [])
        # 白名单模式（enabled_skills 被显式提供）：只保留白名单内且不在黑名单里的技能；
        # 白名单优先于黑名单，处理完直接 return，不再走后面的纯黑名单分支
        if self.enabled_skills is not None:
            enabled_set = set(self.enabled_skills)
            self.skills = [
                s
                for s in self.skills
                if s.get("name") in enabled_set and s.get("name") not in disabled_set
            ]
            # skill_files 的 key 是文件路径，取首段目录名当作技能名，用同一套白 / 黑名单过滤
            if self.skill_files:
                self.skill_files = {
                    path: data
                    for path, data in self.skill_files.items()
                    if (skill_name := path.strip("/").split("/", 1)[0]) in enabled_set
                    and skill_name not in disabled_set
                }
            return

        # 黑名单模式（未提供白名单）：仅剔除被禁用的技能及其对应文件
        if disabled_set:
            self.skills = [s for s in self.skills if s.get("name") not in disabled_set]
            if self.skill_files:
                self.skill_files = {
                    path: data
                    for path, data in self.skill_files.items()
                    if path.strip("/").split("/", 1)[0] not in disabled_set
                }

    async def _lazy_load_mcp_tools(self) -> None:
        """懒加载 MCP 工具（仅在首次调用 get_tools 时初始化）"""
        # 幂等保护：已加载过直接返回，避免重复初始化 MCP
        if self._mcp_loaded:
            return  # 已经尝试过加载

        # 先置位再加载：即便下面加载失败也不再重试，防止每轮调用都反复尝试
        self._mcp_loaded = True

        if not settings.ENABLE_MCP:
            logger.debug("[SearchAgentContext] MCP is disabled (ENABLE_MCP=False)")
            return

        try:
            logger.info(f"[SearchAgentContext] Lazy loading MCP tools for user {self.user_id}")
            # 使用全局缓存，避免重复初始化
            assert self.user_id is not None  # Already guarded above
            # 走全局缓存获取该用户的 MCP 工具与管理器，避免重复建立连接
            mcp_tools, self.mcp_manager = await get_global_mcp_tools(self.user_id)
            logger.info(
                f"[SearchAgentContext] Loaded {len(mcp_tools)} MCP tools (before DB filter)"
            )

            # 过滤数据库中标记为 system_disabled / user_disabled 的工具
            db_disabled = await get_db_disabled_mcp_tool_names(self.user_id)
            mcp_tools = filter_mcp_tools_by_db_state(mcp_tools, db_disabled)
            logger.info(
                f"[SearchAgentContext] After DB filter: {len(mcp_tools)} MCP tools "
                f"(removed {len(db_disabled)} disabled names)"
            )

            from src.agents.core.mcp_tool_exposure import split_mcp_tools_for_exposure

            # 按各 MCP server 的暴露策略，把工具拆成"直接内联"与"延迟加载"两组
            inline_mcp_tools, deferred_mcp_tools = split_mcp_tools_for_exposure(
                mcp_tools,
                getattr(self.mcp_manager, "_server_tool_policies", {}),
            )
            # 内联组无条件并入工具表，模型可直接看到并调用
            if inline_mcp_tools:
                self.tools.extend(inline_mcp_tools)
                logger.info(
                    "[SearchAgentContext] Inlined %d MCP tool(s) by policy",
                    len(inline_mcp_tools),
                )

            # 延迟加载决策：工具总数超过阈值时延迟 MCP 工具
            if (
                settings.ENABLE_DEFERRED_TOOL_LOADING
                and deferred_mcp_tools
                and (len(self.tools) + len(deferred_mcp_tools)) > settings.DEFERRED_TOOL_THRESHOLD
            ):
                from src.infra.tool.deferred_manager import (
                    DeferredToolManager,
                    restore_discovered_tools,
                )

                # 恢复上次已发现的工具名（跨 turn 持久化）
                pre_discovered = await restore_discovered_tools(self.session_id)

                # 工具过多时不全量塞进提示，改由 DeferredToolManager 托管，
                # 后续通过工具检索（ToolSearchTool / ToolSearchMiddleware）按需暴露
                self.deferred_manager = DeferredToolManager(
                    all_deferred_tools=deferred_mcp_tools,
                    session_id=self.session_id,
                    disabled_tools=self.disabled_tools,
                    disabled_mcp_tools=self.disabled_mcp_tools,
                    pre_discovered_names=pre_discovered,
                    prompt_tool_limit=getattr(settings, "DEFERRED_TOOL_PROMPT_LIMIT", 40),
                )
                logger.info(
                    f"[SearchAgentContext] Deferred {len(deferred_mcp_tools)} MCP tools "
                    f"(builtin={len(self.tools)}, threshold={settings.DEFERRED_TOOL_THRESHOLD}, "
                    f"pre_restored={len(pre_discovered)})"
                )
            else:
                # 低于阈值或未启用延迟：走原有逻辑
                self.tools.extend(deferred_mcp_tools)

        except Exception as e:
            logger.error(f"[SearchAgentContext] Failed to load MCP tools: {e}", exc_info=True)

    async def get_tools(self) -> List[Any]:
        """获取所有工具（懒加载 MCP 工具）"""
        await self._lazy_load_mcp_tools()
        return self.tools

    def filter_tools(self) -> List[Any]:
        """根据 disabled_tools 和 disabled_mcp_tools 过滤工具（使用共享过滤逻辑）"""
        filtered = filter_disabled_tools(
            self.tools,
            disabled_tools=self.disabled_tools,
            disabled_mcp_tools=self.disabled_mcp_tools,
            auto_mode=self.auto_mode,
        )
        logger.debug(
            "[SearchAgentContext] Tool filtering: %d/%d tools enabled (auto_mode=%s)",
            len(filtered),
            len(self.tools),
            self.auto_mode,
        )
        return filtered

    async def setup(self) -> None:
        """初始化：工具 + 技能"""
        logger.info(
            f"[SearchAgentContext] Starting setup, ENABLE_SKILLS={settings.ENABLE_SKILLS}, ENABLE_MCP={settings.ENABLE_MCP}"
        )

        # 基础工具
        human_tool = get_human_tool(session_id=self.session_id)
        self.tools.append(human_tool)
        logger.info("[SearchAgentContext] Added human tool")

        reveal_file_tool = get_reveal_file_tool()
        self.tools.append(reveal_file_tool)
        logger.info("[SearchAgentContext] Added reveal_file tool")

        reveal_project_tool = get_reveal_project_tool()
        self.tools.append(reveal_project_tool)
        logger.info("[SearchAgentContext] Added reveal_project tool")

        transfer_file_tool = get_transfer_file_tool()
        self.tools.append(transfer_file_tool)
        logger.info("[SearchAgentContext] Added transfer_file tool")

        transfer_path_tool = get_transfer_path_tool()
        self.tools.append(transfer_path_tool)
        logger.info("[SearchAgentContext] Added transfer_path tool")

        try:
            # 内部工具：先按用户角色 / 是否管理员解析访问权限，再据此加载该用户可见的内部工具
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
            logger.info(f"[SearchAgentContext] Added {len(internal_tools)} internal tools")
        except Exception as e:
            logger.warning(f"[SearchAgentContext] Failed to load internal tools: {e}")

        try:
            # 环境变量工具：按名称去重，避免与已加入的工具重名
            from src.infra.tool.env_var_tool import get_env_var_tools

            existing_tool_names = {getattr(tool, "name", "") for tool in self.tools}
            env_var_tools = [
                tool
                for tool in get_env_var_tools()
                if getattr(tool, "name", "") not in existing_tool_names
            ]
            self.tools.extend(env_var_tools)
            logger.info(f"[SearchAgentContext] Added {len(env_var_tools)} env var tools")
        except Exception as e:
            logger.warning(f"[SearchAgentContext] Failed to load env var tools: {e}")

        # Memory 工具（原生 MongoDB 后端）
        if settings.ENABLE_MEMORY:
            try:
                from src.infra.memory.tools import get_all_memory_tools

                memory_tools = get_all_memory_tools()
                self.tools.extend(memory_tools)
                logger.info(f"[SearchAgentContext] Added {len(memory_tools)} memory tools")
            except ImportError:
                logger.warning("[SearchAgentContext] memory tools import failed, skipping")
            except Exception as e:
                logger.warning(f"[SearchAgentContext] Failed to load memory tools: {e}")

        # 沙箱专属工具
        if settings.ENABLE_SANDBOX:
            from src.infra.tool.sandbox_mcp_tool import get_sandbox_mcp_tools
            from src.infra.tool.upload_url_tool import get_upload_url_tool

            self.tools.append(get_upload_url_tool())
            logger.info("[SearchAgentContext] Added upload_url_to_sandbox tool (sandbox mode)")

            self.tools.extend(get_sandbox_mcp_tools())
            logger.info("[SearchAgentContext] Added sandbox_mcp tools (sandbox mode)")

        # MCP 工具延迟加载（不在 setup 时初始化）
        logger.info("[SearchAgentContext] MCP tools will be lazy loaded on first use")

        # 加载技能
        if settings.ENABLE_SKILLS:
            try:
                skill_result = await load_skill_files(self.user_id)
                self.skill_files = skill_result["files"]
                self.skills = skill_result["skills"]

                before_count = len(self.skills)
                # 应用技能黑 / 白名单过滤（详见 apply_skill_filters）
                self.apply_skill_filters()
                if self.enabled_skills is not None:
                    logger.info(
                        f"[SearchAgentContext] Applied enabled_skills whitelist, {len(self.skills)}/{before_count} remaining"
                    )
                elif self.disabled_skills:
                    logger.info(
                        f"[SearchAgentContext] Filtered out {len(self.disabled_skills)} disabled skills, {len(self.skills)} remaining"
                    )

                logger.info(
                    f"[SearchAgentContext] Loaded {len(self.skills)} skills, "
                    f"{len(self.skill_files)} skill files"
                )
            except Exception as e:
                logger.warning(f"[SearchAgentContext] Failed to load skills: {e}")

        logger.info(f"[SearchAgentContext] Setup complete, total {len(self.tools)} tools available")

    async def close(self) -> None:
        """清理

        注意：MCP 管理器是全局单例，不在这里关闭。
        如果需要清理全局缓存，使用 invalidate_global_cache()。
        """
        # MCP 管理器是全局单例，不在这里关闭
        # 如果需要清理，使用 src.infra.tool.mcp_global.invalidate_global_cache()
        pass

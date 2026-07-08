"""
共享工具过滤工具

提取自 FastAgentContext 和 SearchAgentContext 的重复代码。
提供统一的工具过滤逻辑，包括：
- 内置工具保护（不可被禁用）
- 精确名称匹配
- MCP 服务器前缀匹配（mcp:server_name 格式）
- 数据库持久化的 system_disabled / user_disabled 过滤
"""

# ============================================================================
# 与相邻模块的关系
# ----------------------------------------------------------------------------
# 本文件解决的是"这个工具要不要出现在 Agent 可用工具集里"（禁用/过滤），
# 与同目录 mcp_tool_exposure.py 解决的问题不同——那个文件是在"工具已确定
# 可用"的前提下，再决定它是立即出现在工具列表里（inline）还是延迟到
# 搜索发现时才注入（deferred）。两者是"要不要给"和"什么时候给"的关系，
# 过滤发生在更早的阶段：先用本文件排除掉被禁用的工具，剩下的才轮到
# mcp_tool_exposure.py 去做 inline/deferred 分流。
# 过滤依据分两路来源：运行时传入的禁用列表（filter_disabled_tools，
# 通常来自请求参数/会话配置）和数据库持久化的禁用状态
# （get_db_disabled_mcp_tool_names + filter_mcp_tools_by_db_state，
# 来自管理员/用户在设置页面里保存的偏好）。
# ============================================================================

from typing import Any, List, Optional, Set

# 不可被用户禁用的内置工具
# 这些工具是 Agent 正常运作的基础设施（人机交互确认、沙箱产物揭示、文件中转、
# 沙箱内 MCP 服务器管理等），一旦被随意禁用会导致相关功能整体不可用，
# 因此排除在用户可配置的禁用范围之外
BUILTIN_TOOLS = frozenset(
    [
        "ask_human",
        "reveal_file",
        "reveal_project",
        "transfer_file",
        "sandbox_mcp_add",
        "sandbox_mcp_update",
        "sandbox_mcp_remove",
    ]
)


def filter_disabled_tools(
    tools: List[Any],
    disabled_tools: Optional[List[str]] = None,
    disabled_mcp_tools: Optional[List[str]] = None,
    auto_mode: bool = False,
) -> List[Any]:
    """
    根据禁用列表过滤工具

    Args:
        tools: 所有可用工具列表
        disabled_tools: 禁用的工具名列表
        disabled_mcp_tools: 禁用的 MCP 工具名列表
        auto_mode: 自动模式下允许过滤 ask_human 等内置工具

    Returns:
        过滤后的工具列表

    过滤策略：
    1. BUILTIN_TOOLS 中的工具永远不被过滤（auto_mode 时除外）
    2. 精确名称匹配：如果工具名在 disabled 列表中，过滤掉
    3. MCP 服务器匹配：如果 disabled 列表中有 "mcp:server_name" 格式的条目，
       则该服务器下的所有工具都会被过滤掉
    4. MCP 工具的 server 属性匹配：如果工具有 server 属性且在禁用服务器列表中
    """
    # 三个条件都不满足时没有任何过滤需求，直接原样返回，避免无意义的集合构造开销
    if not disabled_tools and not disabled_mcp_tools and not auto_mode:
        return tools

    # 自动模式下需要过滤的内置工具
    # auto_mode（全自动无人值守运行）下不能用 ask_human 打断流程等待人工输入，
    # 所以即使它在 BUILTIN_TOOLS 保护名单里，这里也要单独过滤掉
    auto_mode_disabled_builtin = frozenset(["ask_human"])

    # 合并所有禁用名称
    disabled_set = set(disabled_tools or [])
    disabled_set.update(disabled_mcp_tools or [])

    mcp_servers = set()
    exact_names = set()

    # 禁用列表里的条目分两类：以 "mcp:" 开头的是"禁用整个 server"的粗粒度规则
    # （去掉前缀后剩下的就是 server 名），其余当作具体工具名的精确匹配规则
    for name in disabled_set:
        if name.startswith("mcp:"):
            mcp_servers.add(name[4:])
        else:
            exact_names.add(name)

    # 预先拼好 "server:" 形式的前缀元组，配合 str.startswith() 支持同时匹配多个前缀
    mcp_prefixes = tuple(f"{s}:" for s in mcp_servers) if mcp_servers else ()

    filtered = []
    for tool in tools:
        tool_name = getattr(tool, "name", str(tool))

        # 内置工具不过滤，除非 auto_mode 且在 AUTO_MODE_DISABLED_BUILTIN 中
        if tool_name in BUILTIN_TOOLS:
            if auto_mode and tool_name in auto_mode_disabled_builtin:
                continue
            filtered.append(tool)
            continue

        # 精确名称匹配
        if tool_name in exact_names:
            continue

        # MCP 服务器前缀匹配
        # 依赖工具名本身携带 "server:tool" 命名空间前缀
        if mcp_prefixes and tool_name.startswith(mcp_prefixes):
            continue

        # MCP server 属性匹配
        # 兜底：有些 MCP 工具对象名字里不带 server 前缀，但会额外挂一个
        # server 属性，两种约定都要覆盖到才能确保"禁用整个 server"规则生效
        if mcp_servers and hasattr(tool, "server") and tool.server in mcp_servers:
            continue

        filtered.append(tool)

    return filtered


async def get_db_disabled_mcp_tool_names(user_id: str) -> Set[str]:
    """
    从数据库查询所有被禁用的 MCP 工具名（合并 system_disabled 和 user_disabled）。

    返回的是全限定名集合（格式: "server_name:tool_name"），
    可直接用于在运行时过滤掉不应传给 agent 的工具。
    如果查询失败，返回空集合（不阻塞 MCP 工具加载）。
    """
    from src.infra.logging import get_logger

    logger = get_logger(__name__)

    try:
        from src.infra.mcp.storage import MCPStorage

        storage = MCPStorage()

        # system 级别：管理员在 system server 上禁用的工具
        system_disabled = await storage.get_system_disabled_tools()
        # user 级别：用户在自己的 server 上禁用的工具
        user_server_disabled = await storage.get_user_server_disabled_tools(user_id)
        # user 级别：用户在 tool_preferences 中禁用的工具（全限定名）
        user_tool_disabled = await storage.get_disabled_tool_names(user_id)

        disabled: Set[str] = set()

        # system 级别的禁用工具 → 构造全限定名
        for server_name, tool_names in system_disabled.items():
            for tool_name in tool_names:
                disabled.add(f"{server_name}:{tool_name}")

        # user server 级别的禁用工具 → 构造全限定名
        for server_name, tool_names in user_server_disabled.items():
            for tool_name in tool_names:
                disabled.add(f"{server_name}:{tool_name}")

        # user tool preference 级别（已经是全限定名）
        disabled.update(user_tool_disabled)

        if disabled:
            logger.info(
                "[tool_filter] DB disabled MCP tools for user %s: %s",
                user_id,
                disabled,
            )

        return disabled

    # 查询数据库失败（连接问题、存储层异常等）时不应该让整个 MCP 工具加载
    # 流程失败——宁可这次没能应用禁用规则，也不能因此让用户完全用不了工具
    except Exception as e:
        logger.warning(
            "[tool_filter] Failed to query DB disabled tools for user %s: %s",
            user_id,
            e,
            exc_info=True,
        )
        return set()


def filter_mcp_tools_by_db_state(
    mcp_tools: List[Any],
    disabled_names: Set[str],
) -> List[Any]:
    """
    根据 get_db_disabled_mcp_tool_names() 返回的禁用集合过滤 MCP 工具列表。

    匹配规则：
    - 精确匹配全限定名（"server:tool" 格式）
    - 短名匹配兜底（如果工具名不含 server 前缀）
    """
    if not disabled_names:
        return mcp_tools

    # 预计算短名集合，用于无前缀工具的兜底匹配
    # disabled_names 里存的都是 "server:tool" 全限定名，但传入的 mcp_tools
    # 里个别工具对象的 name 可能没带 server 前缀，因此额外维护一份去掉
    # server 前缀后的"短名"集合，供后面兜底比对
    short_disabled: Set[str] = set()
    for dn in disabled_names:
        if ":" in dn:
            short_disabled.add(dn.split(":", 1)[1])
        else:
            short_disabled.add(dn)

    before_count = len(mcp_tools)
    filtered: List[Any] = []
    for tool in mcp_tools:
        tool_name = getattr(tool, "name", str(tool))

        # 精确匹配全限定名
        if tool_name in disabled_names:
            continue

        # 短名匹配（工具名不含 server 前缀时）
        if ":" not in tool_name and tool_name in short_disabled:
            continue

        filtered.append(tool)

    removed = before_count - len(filtered)
    # 只有实际发生了过滤才记录日志，避免每次调用都产生一条无意义的"过滤了 0 个"日志
    if removed > 0:
        from src.infra.logging import get_logger

        get_logger(__name__).info(
            "[tool_filter] Filtered %d/%d MCP tools by DB state",
            removed,
            before_count,
        )

    return filtered

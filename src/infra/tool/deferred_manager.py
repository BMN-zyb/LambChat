"""
延迟工具管理器 — 管理按需加载的 MCP 工具生命周期。

启动时只保留轻量的工具名列表（通过系统提示告知 LLM），
当 LLM 通过 search_tools 搜索时，将匹配的工具提升为"已发现"状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from src.infra.logging import get_logger
from src.kernel.config import settings

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

logger = get_logger(__name__)


# 注入到系统提示中的"延迟工具搜索指南"：告知 LLM 有一批 MCP 工具尚未加载，
# 需要先调用 search_tools 载入完整参数 schema 后再正常使用；并强调 sandbox 工具不在此列
DEFERRED_TOOL_SEARCH_GUIDE = (
    "## MCP Tool Search Guide\n\n"
    "Deferred MCP tools are available but not yet loaded. "
    "If one of these tools would help with the current request, call `search_tools` "
    "first to load its full parameter schema, then use that tool normally. "
    "`search_tools` only searches deferred MCP tools listed in the dynamic "
    "`## MCP Tools (Deferred)` section; it does NOT search sandbox tools. "
    "Sandbox tools are NOT MCP tools — use `execute` with `mcporter` commands "
    "to discover and call them."
)


def _tool_sort_key(tool: "BaseTool") -> tuple[str, str]:
    # 稳定排序键：先按所属服务器再按工具名，保证提示输出顺序确定、可复现
    return (getattr(tool, "server", "") or "", getattr(tool, "name", "") or "")


@dataclass
class DeferredToolStub:
    """延迟工具的轻量描述（用于系统提示注入）"""

    name: str
    description: str  # 首行，截断
    server: str = ""
    is_mcp: bool = False


class DeferredToolManager:
    """管理延迟 MCP 工具的发现和提升

    内置 dirty flag 机制：stubs 和 prompt string 仅在 discover_tools() 后才重建，
    避免每次 LLM 调用时重复分配。
    """

    def __init__(
        self,
        all_deferred_tools: list["BaseTool"],
        session_id: str,
        disabled_tools: Optional[list[str]] = None,
        disabled_mcp_tools: Optional[list[str]] = None,
        pre_discovered_names: Optional[list[str]] = None,
        prompt_tool_limit: Optional[int] = None,
        parent: Optional["DeferredToolManager"] = None,
    ):
        # 应用 disabled_tools 过滤
        # 合并两类禁用清单：精确工具名 与 "mcp:server" 形式的整服务器禁用
        disabled_set = set(disabled_tools or [])
        disabled_set.update(disabled_mcp_tools or [])
        # 提取以 "mcp:" 前缀声明的被禁用服务器名
        mcp_servers = {t[4:] for t in disabled_set if t.startswith("mcp:")}
        # 剩下的即按精确工具名禁用的集合
        exact_disabled = disabled_set - {f"mcp:{s}" for s in mcp_servers}

        filtered: list["BaseTool"] = []
        for tool in all_deferred_tools:
            name = getattr(tool, "name", "")
            # 精确名禁用
            if name in exact_disabled:
                continue
            # mcp:server 前缀过滤
            # 按 server 属性整服务器禁用
            server = getattr(tool, "server", "")
            if server in mcp_servers:
                continue
            # 名称前缀过滤
            # 兜底：工具名以 "server:" 开头也视为属于被禁用服务器
            # （for-else：仅当未 break 时才保留该工具）
            for s in mcp_servers:
                if name.startswith(f"{s}:"):
                    break
            else:
                filtered.append(tool)

        # 全量工具（排序后）与名称索引
        self._all_tools: list["BaseTool"] = sorted(filtered, key=_tool_sort_key)
        self._tool_map: dict[str, "BaseTool"] = {t.name: t for t in filtered}
        # 恢复上次已发现工具（从 store 持久化的数据）
        # 与当前可用工具取交集，剔除已下线的历史工具名
        pre_set = set(pre_discovered_names or []) & set(self._tool_map.keys())
        self._discovered_names: set[str] = pre_set
        self._session_id = session_id
        # parent：用于子作用域 fork，向父管理器同步"已发现"集合
        self._parent = parent
        # 提示中最多展示的延迟工具数（超出仅提示数量，节省上下文）；<=0 视为不限
        configured_prompt_limit = prompt_tool_limit
        if configured_prompt_limit is None:
            configured_prompt_limit = getattr(settings, "DEFERRED_TOOL_PROMPT_LIMIT", 40)
        self._prompt_tool_limit = max(int(configured_prompt_limit or 0), 0) or None

        # Backward-compatible aggregate dirty flag.
        # stale 为聚合脏标记（兼容旧调用）；下面两个是分项脏标记
        self.stale: bool = True
        self._stubs_stale: bool = True
        self._prompt_stale: bool = True

        # 缓存
        # 分别缓存 stub 列表、提示块元组、拼接后的提示字符串，脏标记控制重建
        self._cached_stubs: list[DeferredToolStub] = []
        self._cached_prompt_blocks: tuple[str, ...] = ()
        self._cached_stubs_string: str = ""

        logger.info(
            "[DeferredToolManager] Created: %d deferred tools for session %s "
            "(%d pre-restored from store)",
            len(filtered),
            session_id,
            len(pre_set),
        )

    def fork_for_scope(self, scope: str) -> "DeferredToolManager":
        """Create an isolated manager for nested agent/tool-search scopes.

        The fork shares immutable tool objects but owns its discovery set, so a
        sub-agent can search and call tools without promoting them in the parent
        agent's tool list.
        """
        # 为嵌套作用域（子 agent/工具搜索）创建隔离管理器：
        # 共享同一批不可变工具对象，但拥有独立的"已发现"集合，
        # 使子作用域的发现不会污染父 agent 的工具列表
        safe_scope = scope.strip() or "isolated"
        return DeferredToolManager(
            all_deferred_tools=self._all_tools,
            session_id=f"{self._session_id}:{safe_scope}",
            pre_discovered_names=self.discovered_names,
            prompt_tool_limit=self._prompt_tool_limit,
            parent=self,
        )

    def _sync_parent_discoveries(self) -> None:
        # 从父管理器"下拉"新发现的工具：单向继承父的发现结果，
        # 保证子作用域至少能看到父已发现的工具；无父则空操作
        if self._parent is None:
            return

        parent_names = set(self._parent.discovered_names)
        # 只继承本作用域实际拥有的工具名
        inherited = parent_names & set(self._tool_map.keys())
        new_names = inherited - self._discovered_names
        if not new_names:
            return

        # 有新增继承项时，更新集合并置脏标记以触发缓存重建
        self._discovered_names.update(new_names)
        self.stale = True
        self._stubs_stale = True
        self._prompt_stale = True

    @property
    def total_deferred(self) -> int:
        """延迟工具总数"""
        return len(self._all_tools)

    @property
    def discovered_count(self) -> int:
        """已发现工具数"""
        # 读取前先同步父发现，保证计数最新
        self._sync_parent_discoveries()
        return len(self._discovered_names)

    @property
    def discovered_names(self) -> list[str]:
        """已发现工具名列表"""
        self._sync_parent_discoveries()
        return sorted(self._discovered_names)

    @property
    def remaining_count(self) -> int:
        """剩余未发现工具数"""
        self._sync_parent_discoveries()
        return self.total_deferred - self.discovered_count

    def get_deferred_stubs(self) -> list[DeferredToolStub]:
        """获取未发现工具的轻量描述列表（带脏标记缓存）"""
        self._sync_parent_discoveries()
        # 未变脏则直接返回缓存，避免每次 LLM 调用都重建
        if not self._stubs_stale:
            return self._cached_stubs

        stubs: list[DeferredToolStub] = []
        for tool in self._all_tools:
            # 已发现的工具不再出现在 stub 列表（它们已作为完整工具暴露）
            if tool.name in self._discovered_names:
                continue
            desc = getattr(tool, "description", "") or ""
            # 只取描述首行并截断到 120 字符，作为轻量提示
            hint = desc.split("\n")[0].strip()[:120]
            stubs.append(
                DeferredToolStub(
                    name=tool.name,
                    description=hint,
                    server=getattr(tool, "server", ""),
                    is_mcp=True,
                )
            )

        # 稳定排序并刷新缓存与脏标记
        self._cached_stubs = sorted(stubs, key=lambda stub: (stub.server, stub.name))
        self._stubs_stale = False
        self.stale = self._stubs_stale or self._prompt_stale
        return self._cached_stubs

    def get_deferred_prompt_blocks(self) -> tuple[str, ...]:
        """Return prompt blocks for deferred MCP guidance and visible tool stubs."""
        # 生成注入系统提示的文本块：搜索指南 + 可见的延迟工具清单
        self._sync_parent_discoveries()
        if not self._prompt_stale:
            return self._cached_prompt_blocks

        # 未发现工具（需要 search_tools）
        stubs = self.get_deferred_stubs()  # 调用后 stale=False 并更新缓存
        if stubs:
            visible_stubs = stubs
            hidden_count = 0
            # 超过展示上限时只列前 N 个，其余用一句说明代替，节省上下文
            if self._prompt_tool_limit is not None and len(stubs) > self._prompt_tool_limit:
                visible_stubs = stubs[: self._prompt_tool_limit]
                hidden_count = len(stubs) - len(visible_stubs)

            lines = "\n".join(f"- {s.name}: {s.description}" for s in visible_stubs)
            parts: list[str] = [
                DEFERRED_TOOL_SEARCH_GUIDE,
                "## MCP Tools (Deferred)\n\n" + lines,
            ]
            if hidden_count:
                # 提示还有多少工具未展示，并给出如何搜索/精确选择的方法
                noun = "tool" if hidden_count == 1 else "tools"
                parts.append(
                    f"\n\nNote: {hidden_count} more deferred MCP {noun} not shown here to save "
                    "context. Use `search_tools` with capability keywords, or `select:server:tool` "
                    "when you know the exact name."
                )
            result = tuple(parts)
        else:
            # 没有未发现工具时返回空元组（不注入任何提示块）
            result = ()

        # 同步刷新提示块缓存与拼接字符串缓存
        self._cached_prompt_blocks = result
        self._cached_stubs_string = "\n\n".join(result)
        self._prompt_stale = False
        self.stale = self._stubs_stale or self._prompt_stale
        return result

    def get_deferred_stubs_string(self) -> str:
        """返回可直接拼入系统提示的预格式化字符串（带脏标记缓存）。"""
        if not self._prompt_stale:
            return self._cached_stubs_string
        # 触发一次提示块重建即会顺带刷新字符串缓存
        self.get_deferred_prompt_blocks()
        return self._cached_stubs_string

    def get_discovered_tools(self) -> list["BaseTool"]:
        """获取已发现工具的完整 BaseTool 列表"""
        # 这些工具会以完整参数 schema 绑定给 LLM
        self._sync_parent_discoveries()
        return [self._tool_map[n] for n in sorted(self._discovered_names) if n in self._tool_map]

    def get_undiscovered_tools(self) -> list["BaseTool"]:
        """获取未发现工具的完整 BaseTool 列表（用于搜索）"""
        # search_tools 只在这批未发现工具中检索
        self._sync_parent_discoveries()
        return [t for t in self._all_tools if t.name not in self._discovered_names]

    def discover_tools(self, names: list[str]) -> list["BaseTool"]:
        """将工具从延迟状态提升为已发现。同时标记缓存为 stale。

        Args:
            names: 要提升的工具名称列表

        Returns:
            新发现的 BaseTool 列表
        """
        self._sync_parent_discoveries()
        newly_discovered: list["BaseTool"] = []
        for name in names:
            # 只提升"存在且尚未发现"的工具，忽略未知名与重复提升
            if name in self._tool_map and name not in self._discovered_names:
                self._discovered_names.add(name)
                newly_discovered.append(self._tool_map[name])

        if newly_discovered:
            # 发现集合变化 -> 置脏，使 stub/prompt 下次读取时重建
            self.stale = True
            self._stubs_stale = True
            self._prompt_stale = True
            logger.info(
                "[DeferredToolManager] Discovered %d tools: %s (session %s)",
                len(newly_discovered),
                [t.name for t in newly_discovered],
                self._session_id,
            )

        return newly_discovered

    def is_discovered(self, name: str) -> bool:
        """检查工具是否已发现"""
        self._sync_parent_discoveries()
        return name in self._discovered_names

    def get_tool(self, name: str) -> Optional["BaseTool"]:
        """按名称获取工具（无论是否已发现）"""
        return self._tool_map.get(name)

    def get_stats(self) -> dict:
        """返回统计信息"""
        self._sync_parent_discoveries()
        return {
            "total_deferred": self.total_deferred,
            "discovered": self.discovered_count,
            "remaining": self.remaining_count,
            "session_id": self._session_id,
        }


# ---------------------------------------------------------------------------
# Store persistence helpers
# 会话级持久化：把"已发现工具名"存入 BaseStore，使会话重启后仍能恢复发现状态
# ---------------------------------------------------------------------------

# BaseStore 命名空间与键前缀（按 session 隔离）
_DISCOVERED_TOOLS_NAMESPACE = ("deferred_tools",)
_DISCOVERED_TOOLS_KEY_PREFIX = "session:"


def _store_key_for_session(session_id: str) -> str:
    # 由 session_id 拼出存储键
    return f"{_DISCOVERED_TOOLS_KEY_PREFIX}{session_id}"


async def restore_discovered_tools(
    session_id: str,
) -> list[str]:
    """从 BaseStore 恢复上次已发现的工具名列表。失败时返回空列表。"""
    # 持久化是可选优化，任何异常都降级为"无历史记录"
    try:
        from src.infra.storage.mongodb_store import acreate_store

        store = await acreate_store()
        if store is None:
            return []

        item = await store.aget(
            _DISCOVERED_TOOLS_NAMESPACE,
            _store_key_for_session(session_id),
        )
        if item is None:
            return []

        value = item.value
        # value 格式: {"names": [...]}
        # 兼容两种历史存储格式：dict 包装或直接列表
        if isinstance(value, dict):
            names = value.get("names", [])
        elif isinstance(value, list):
            names = value
        else:
            return []
        # 仅保留字符串项，过滤脏数据
        return [n for n in names if isinstance(n, str)]
    except Exception:
        logger.warning(
            "[DeferredToolManager] Failed to restore discovered tools for session %s",
            session_id,
            exc_info=True,
        )
        return []


async def persist_discovered_tools(
    session_id: str,
    discovered_names: list[str],
) -> None:
    """将已发现工具名列表持久化到 BaseStore。失败时静默忽略。"""
    # 空列表无需写入
    if not discovered_names:
        return
    try:
        from src.infra.storage.mongodb_store import acreate_store

        store = await acreate_store()
        if store is None:
            return

        await store.aput(
            _DISCOVERED_TOOLS_NAMESPACE,
            _store_key_for_session(session_id),
            {"names": discovered_names},
        )
        logger.debug(
            "[DeferredToolManager] Persisted %d discovered tools for session %s",
            len(discovered_names),
            session_id,
        )
    except Exception:
        # 持久化失败不影响主流程，仅告警
        logger.warning(
            "[DeferredToolManager] Failed to persist discovered tools for session %s",
            session_id,
            exc_info=True,
        )


async def clear_discovered_tools(session_id: str) -> None:
    """清除指定 session 的已发现工具记录。"""
    try:
        from src.infra.storage.mongodb_store import acreate_store

        store = await acreate_store()
        if store is None:
            return

        await store.aput(
            _DISCOVERED_TOOLS_NAMESPACE,
            _store_key_for_session(session_id),
            None,  # type: ignore[arg-type]  # value=None means delete
        )
    except Exception:
        logger.warning(
            "[DeferredToolManager] Failed to clear discovered tools for session %s",
            session_id,
            exc_info=True,
        )

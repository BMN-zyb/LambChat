"""Sandbox Tools Prompt Builder - Injects sandbox tool descriptions into system prompt.

These are sandbox tools (managed via mcporter), NOT MCP tools.
The LLM must use the `execute` tool to invoke them.

Caches mcporter list output per-user to maximize KV cache hit rate.
The prompt section is appended at the END of the system prompt so that
changes only invalidate the tail of the KV cache, not the stable prefix.
"""
# 中文说明：本模块负责把"沙箱内已注册的 MCP 工具列表"渲染成一段系统提示词文本，
# 让 LLM 知道当前沙箱里有哪些工具可以通过 execute + mcporter 调用。
# 关键设计点：
#   1）按用户维度缓存渲染结果（mcporter list 需要真实执行沙箱命令，成本较高）；
#   2）缓存内容追加在系统提示词最末尾，这样内容变化时只影响 KV 缓存的尾部，
#      不会使前面大段稳定的系统提示词前缀失效，从而最大化 KV cache 命中率；
#   3）工具数量超过上限时进行截断，并追加提示语引导 LLM 用 `mcporter list` 自行发现。

import json
import time
from typing import Any

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger

logger = get_logger(__name__)

# Cache: user_id -> (prompt_sections, total_tool_count, timestamp)
# 中文：按用户维度缓存已渲染好的提示词分段、工具总数与写入时间戳
_sandbox_mcp_prompt_cache: dict[str, tuple[tuple[str, ...], int, float]] = {}

# Cache TTL in seconds
# 缓存存活时间（秒），超过该时长视为过期，需要重新拉取 mcporter 数据
_CACHE_TTL = 1800  # 30 minutes
# 缓存条目上限，防止用户量增长导致该内存缓存无限膨胀
_MAX_PROMPT_CACHE_ENTRIES = 500

# Max tools to inject into system prompt (beyond this, LLM uses bash to discover more)
# With descriptions + params, each tool uses ~60-120 tokens; 20 tools ≈ 1200-2400 tokens.
# 中文：注入系统提示词的工具数量上限，超出部分不会逐条列出，
# 而是提示 LLM 自行通过 mcporter list 命令发现，避免提示词过长浪费 token
_MAX_TOOLS_IN_PROMPT = 20

# mcporter timeout
# 执行 `mcporter list --json` 的超时时间（秒）
_MCPORTER_TIMEOUT = 15
# 探测 `mcporter --version` 是否可用的超时时间（秒），需要更短以快速失败
_MCPORTER_CHECK_TIMEOUT = 5


async def build_sandbox_mcp_prompt(
    backend: Any,
    user_id: str,
    force_refresh: bool = False,
) -> str:
    """Build a prompt section describing available sandbox MCP tools."""
    # 中文：兼容旧调用方的入口，内部拼接各分段为一整段字符串
    return "\n\n".join(await build_sandbox_mcp_prompt_sections(backend, user_id, force_refresh))


async def build_sandbox_mcp_prompt_sections(
    backend: Any,
    user_id: str,
    force_refresh: bool = False,
) -> tuple[str, ...]:
    """Build a prompt section describing available sandbox MCP tools.

    Args:
        backend: The sandbox backend (CompositeBackend) to run mcporter on.
        user_id: User ID for cache keying.
        force_refresh: If True, bypass cache and refresh.

    Returns:
        Formatted prompt string, or empty string if no tools available.
    """
    # Cleanup stale cache entries periodically
    # 每次调用时顺带清理过期/超量的缓存条目，避免额外起一个后台定时任务
    _cleanup_stale_cache()

    # Check cache
    # 未强制刷新且缓存未过期时，直接复用缓存结果，避免重复执行 mcporter 命令
    if not force_refresh and user_id in _sandbox_mcp_prompt_cache:
        prompt_sections, total_count, ts = _sandbox_mcp_prompt_cache[user_id]
        if time.time() - ts < _CACHE_TTL:
            logger.debug(f"[SandboxMCP Prompt] Cache hit for user {user_id}")
            return _maybe_append_overflow_hint_sections(prompt_sections, total_count)

    # Fetch from mcporter
    # 缓存未命中或强制刷新：真正执行 mcporter list 并重新渲染
    prompt_sections, total_count = await _fetch_and_format(backend)

    # Update cache (even if empty — avoids repeated mcporter calls when no servers exist)
    _sandbox_mcp_prompt_cache[user_id] = (prompt_sections, total_count, time.time())
    logger.info(
        f"[SandboxMCP Prompt] {'Cache miss' if not force_refresh else 'Force refresh'} "
        f"for user {user_id}, prompt length={sum(len(section) for section in prompt_sections)}, total_tools={total_count}"
    )

    return _maybe_append_overflow_hint_sections(prompt_sections, total_count)


def _cleanup_stale_cache() -> None:
    """Remove expired entries from the cache."""
    now = time.time()
    # 找出所有已超过 TTL 的用户缓存条目并逐一删除
    stale = [uid for uid, (_, _, ts) in _sandbox_mcp_prompt_cache.items() if now - ts > _CACHE_TTL]
    for uid in stale:
        del _sandbox_mcp_prompt_cache[uid]
    if stale:
        logger.debug(f"[SandboxMCP Prompt] Cleaned up {len(stale)} stale cache entries")
    # 过期清理之外，再按条目数上限做兜底清理（防止长期运行下缓存无限增长）
    removed = _cleanup_excess_prompt_cache_entries()
    if removed:
        logger.debug(f"[SandboxMCP Prompt] Cleaned up {removed} excess cache entries")


def _cleanup_excess_prompt_cache_entries() -> int:
    # 中文：当缓存条目数超过上限时，按写入时间从旧到新排序，
    # 淘汰最旧的若干条目，使缓存总量回落到上限以内；返回实际淘汰的条目数
    max_entries = max(int(_MAX_PROMPT_CACHE_ENTRIES), 1)
    if len(_sandbox_mcp_prompt_cache) <= max_entries:
        return 0

    to_remove = len(_sandbox_mcp_prompt_cache) - max_entries
    oldest = sorted(
        _sandbox_mcp_prompt_cache.items(),
        key=lambda item: item[1][2],
    )[:to_remove]
    for user_id, _entry in oldest:
        _sandbox_mcp_prompt_cache.pop(user_id, None)
    return len(oldest)


def invalidate_sandbox_mcp_prompt_cache(user_id: str) -> None:
    """Invalidate the cached prompt for a user.

    Call this after sandbox_mcp_add/update/remove operations.
    """
    # 中文：sandbox_mcp_add/update/remove 修改配置后必须调用本函数，
    # 否则该用户下次请求仍会命中旧缓存，看不到最新的 MCP 服务器列表
    if user_id in _sandbox_mcp_prompt_cache:
        del _sandbox_mcp_prompt_cache[user_id]
        logger.debug(f"[SandboxMCP Prompt] Cache invalidated for user {user_id}")


def _maybe_append_overflow_hint(prompt: str, total_count: int) -> str:
    """Append overflow hint to prompt if tools were truncated."""
    # 中文：只有工具总数超过展示上限时才追加提示语；未超限或提示词本身为空则原样返回
    if not prompt or total_count <= _MAX_TOOLS_IN_PROMPT:
        return prompt

    return (
        prompt
        + f"> **Note:** Only {_MAX_TOOLS_IN_PROMPT} of {total_count} tools are shown above. "
        + 'Use `execute(command="mcporter list")` to find the right service, then '
        + '`execute(command="mcporter list <service> --schema")` before the first call.\n'
    )


def _maybe_append_overflow_hint_sections(
    prompt_sections: tuple[str, ...], total_count: int
) -> tuple[str, ...]:
    """Append overflow hint as its own section when tools were truncated."""
    # 中文：与 _maybe_append_overflow_hint 逻辑一致，只是以“分段”形式追加，
    # 供按分段拼接系统提示词尾部的调用方使用
    if not prompt_sections or total_count <= _MAX_TOOLS_IN_PROMPT:
        return prompt_sections

    return prompt_sections + (
        f"> **Note:** Only {_MAX_TOOLS_IN_PROMPT} of {total_count} tools are shown above. "
        'Use `execute(command="mcporter list")` to find the right service, then '
        '`execute(command="mcporter list <service> --schema")` before the first call.\n',
    )


def _clean_description(desc: str) -> str:
    """Strip Args/COST WARNING sections, keep core one-line description."""
    if not desc:
        return ""
    # Remove Args section
    # 中文：MCP 工具描述里常带有详细的 Args/COST WARNING 说明，
    # 这些内容对系统提示词而言太长，只保留最前面的核心一句话描述
    for marker in ("\n\nArgs:", "\nArgs:"):
        idx = desc.find(marker)
        if idx != -1:
            desc = desc[:idx].strip()
    # Remove COST WARNING
    for marker in ("\n\nCOST WARNING:", "\nCOST WARNING:"):
        idx = desc.find(marker)
        if idx != -1:
            desc = desc[:idx].strip()
    # Collapse multi-line to single line
    desc = " ".join(desc.split())
    # Truncate long descriptions
    if len(desc) > 200:
        desc = desc[:197] + "..."
    return desc


def _format_params(schema: Any) -> str:
    """Format inputSchema properties into a concise parameter list.

    Example output:
      Params: query (string, required), limit (integer, default: 10)
    """
    if not isinstance(schema, dict):
        return ""

    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    if not properties:
        return ""

    parts = []
    # 中文：遍历 JSON Schema 的 properties，逐个拼出「参数名(类型, 是否必填, 默认值, 枚举值)」
    for name, info in properties.items():
        if not isinstance(info, dict):
            continue
        ptype = info.get("type", "any")
        tokens = [name, f"({ptype}"]
        if name in required:
            tokens.append(", required")
        if "default" in info:
            tokens.append(f", default: {info['default']}")
        # Add enum hint if present
        # 枚举值较多时只展示前 5 个，避免提示词过长
        if "enum" in info and isinstance(info["enum"], list):
            enum_vals = ", ".join(str(v) for v in info["enum"][:5])
            tokens.append(f", enum: [{enum_vals}]")
        tokens.append(")")
        parts.append("".join(tokens))

    if not parts:
        return ""
    return "Params: " + ", ".join(parts)


def _format_tools_list(data: Any) -> tuple[str, int]:
    """Backward-compatible string formatter for sandbox tool prompt."""
    # 中文：旧接口，内部委托给按分段返回的新实现，再拼接成一整段字符串
    sections, total_count = _format_tools_list_sections(data)
    return "\n\n".join(sections), total_count


def _format_tools_list_sections(data: Any) -> tuple[tuple[str, ...], int]:
    """Format mcporter list JSON output into a readable prompt section.

    Returns:
        Tuple of (formatted_prompt, total_tool_count).

    Actual mcporter list --json format:
    {
      "mode": "list",
      "servers": [
        {
          "name": "server_name",
          "status": "ok",
          "tools": [
            {
              "name": "tool_name",
              "description": "...",
              "inputSchema": { ... }
            }
          ]
        }
      ]
    }
    """
    if not isinstance(data, dict):
        return (), 0

    # mcporter returns servers as a list under "servers" key
    servers = data.get("servers", [])
    if not isinstance(servers, list):
        return (), 0

    # 中文：intro_lines 是固定的引导文案（静态不随数据变化），
    # 用于告知 LLM 这些是"沙箱工具"而非"MCP 工具"、必须通过 execute+mcporter 调用，
    # 以及调用前必须先用 --schema 探查参数结构，避免 LLM 瞎猜参数导致调用失败
    intro_lines = [
        "## Sandbox Tools (NOT MCP — DO NOT call directly)",
        "",
        "⚠️ **IMPORTANT**: The tools listed below are **sandbox tools**, NOT MCP tools. "
        "You do NOT have direct access to them. Do NOT attempt to call them as MCP tools "
        "— such calls will fail.",
        "",
        "**How to use**: You MUST use the `execute` tool with `mcporter` commands. "
        "The `execute` tool is your ONLY way to invoke sandbox tools.",
        "",
        "**Required first-use sequence**: before the first `mcporter call` for any sandbox tool, "
        "you must inspect its parameters via `execute`: first identify the service with "
        "`mcporter list`, then inspect that service with `mcporter list <service> --schema`.",
        "Do NOT jump straight to `mcporter call` just because a short params summary appears below. "
        "The summary tells you what exists, not the full tool shape.",
        "",
        "Example — find the service, inspect it, then call `server.my_tool` with arg `query=hello`:",
        "```",
        'execute(command="mcporter list")',
        "# find the target service, then inspect it:",
        'execute(command="mcporter list server --schema")',
        "# after confirming the tool and its parameters:",
        'execute(command="mcporter call server.my_tool query=hello")',
        "```",
        "",
        "**Discovery** — run via `execute`:",
        "- `mcporter list` — list configured services and their tools",
        "- `mcporter list <service> --schema` — inspect one service's tools and parameter schemas before first use",
        "",
        "**Repository search discipline**:",
        "- avoid repo-wide searches unless absolutely necessary.",
        "- When looking for code, use `ls` or `glob` first to narrow the area.",
        "- narrow `path` before `grep`; do not start by grepping from the repository root with a broad pattern.",
        "",
        "**Invocation** — call via `execute`: `mcporter call server.tool <args>`",
        "- Named args: `mcporter call server.tool key=value` (values with spaces MUST be quoted)",
        '- JSON payload: `mcporter call server.tool --args \'{"key": "value"}\'` (for complex params)',
        "",
        "Do NOT use `--flag value` syntax — that passes `value` as a positional arg.",
        "",
        "**Server Management**: `sandbox_mcp_add` / `sandbox_mcp_update` / `sandbox_mcp_remove` — "
        "changes are persisted and auto-restored on sandbox rebuild.",
        "",
    ]
    tool_lines: list[str] = []

    tool_count = 0
    total_count = 0

    # 中文：外层遍历每个已注册的 MCP 服务器
    for server in servers:
        if not isinstance(server, dict):
            continue

        server_name = server.get("name", "")
        server_status = server.get("status", "")
        tools = server.get("tools", [])
        if not tools:
            continue

        # Server header
        status_tag = f" ({server_status})" if server_status and server_status != "ok" else ""
        tool_lines.append(f"### {server_name}{status_tag}")

        # 内层遍历该服务器下的每个工具；
        # total_count 统计所有工具数（不受展示上限影响，用于溢出提示），
        # tool_count 统计实际渲染进提示词的工具数（受 _MAX_TOOLS_IN_PROMPT 限制）
        for tool in tools:
            total_count += 1

            if tool_count >= _MAX_TOOLS_IN_PROMPT:
                continue

            tool_name = tool.get("name", "")
            tool_desc = tool.get("description", "")

            if not tool_name:
                continue

            tool_count += 1

            # Build tool entry with description and parameters
            full_name = f"{server_name}.{tool_name}"

            # Clean description: strip Args/COST WARNING sections, keep core description
            tool_desc = _clean_description(tool_desc)

            tool_lines.append(f"- `{full_name}`")
            if tool_desc:
                tool_lines.append(f"  {tool_desc}")

            # Extract and format parameters
            param_line = _format_params(tool.get("inputSchema"))
            if param_line:
                tool_lines.append(f"  {param_line}")

            tool_lines.append(
                f'  → first inspect this service: `execute(command="mcporter list {server_name} --schema")`'
            )
            tool_lines.append(
                f'  → then call: `execute(command="mcporter call {full_name} <args>")`'
            )

        tool_lines.append("")

    if not tool_lines:
        return (), total_count
    # 中文：intro_lines 与 tool_lines 分别作为两个独立分段返回，
    # 便于调用方把它们分别拼接在系统提示词的不同位置
    return ("\n".join(intro_lines), "\n".join(tool_lines).rstrip()), total_count


async def _fetch_and_format(backend: Any) -> tuple[tuple[str, ...], int]:
    """Run mcporter list and format the output."""
    try:
        # 先探测 mcporter 是否可用，不可用则直接返回空，避免后续命令必然失败
        if not await _is_mcporter_available(backend):
            return (), 0

        result = await backend.aexecute("mcporter list --json", timeout=_MCPORTER_TIMEOUT)
        if result.exit_code != 0:
            logger.warning(f"[SandboxMCP Prompt] mcporter list failed: {result.output}")
            return (), 0

        try:
            # JSON 解析通过 run_blocking_io 放入线程池执行，避免阻塞事件循环
            data = await run_blocking_io(json.loads, result.output)
            logger.debug(f"[SandboxMCP Prompt] mcporter list output: {data}")
        except json.JSONDecodeError:
            logger.warning("[SandboxMCP Prompt] mcporter list returned invalid JSON")
            return (), 0

        return _format_tools_list_sections(data)

    except Exception as e:
        # 兜底：任何异常都不应该影响系统提示词的正常构建，降级为"无沙箱工具"
        logger.warning(f"[SandboxMCP Prompt] Failed to fetch tools: {e}")
        return (), 0


async def _is_mcporter_available(backend: Any) -> bool:
    """Check whether mcporter is installed in the current sandbox."""
    try:
        result = await backend.aexecute("mcporter --version", timeout=_MCPORTER_CHECK_TIMEOUT)
    except Exception as e:
        logger.info(f"[SandboxMCP Prompt] Failed to check mcporter availability: {e}")
        return False

    if result.exit_code != 0:
        logger.info(
            f"[SandboxMCP Prompt] mcporter not available (exit={result.exit_code}, output={result.output})"
        )
        return False

    return True

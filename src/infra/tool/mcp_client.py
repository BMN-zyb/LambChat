"""
MCP 客户端管理器

与 Model Context Protocol 服务器通信。
支持从数据库或文件读取配置。
"""

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import PrivateAttr

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.kernel.config import settings
from src.kernel.schemas.mcp import MCPRoleQuota, MCPToolPolicy

logger = get_logger(__name__)

# MCP 重试配置
# 单次工具调用最多尝试次数（含首次）
MCP_MAX_RETRIES = 3
MCP_RETRY_DELAY = 1.0  # 秒
MCP_TOOL_TIMEOUT = 300  # 单次工具调用超时（秒）
# 配置文件大小上限，防止读入超大 mcp.json 造成内存/解析问题
MCP_CONFIG_FILE_MAX_BYTES = 1024 * 1024

# 与 deepagents backend 冲突的工具名（需要过滤掉）
# 这些名字与内置文件/命令工具重名，若由外部 MCP 提供会造成路由歧义，故在装配时剔除
CONFLICTING_TOOL_NAMES = frozenset(
    [
        "read_file",
        "write_file",
        "edit_file",
        "ls",
        "glob",
        "grep",
        "bash",
    ]
)


def _get_effective_config_max_servers() -> int:
    # 读取"直传配置"允许加载的最大服务器数，非法值回退到默认 100，且至少为 1
    value = getattr(settings, "MCP_EFFECTIVE_CONFIG_MAX_SERVERS", 100)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 100


def _get_effective_config_max_tools() -> int:
    # 读取"直传配置"允许加载的最大工具数，非法值回退到默认 200，且至少为 1
    value = getattr(settings, "MCP_EFFECTIVE_CONFIG_MAX_TOOLS", 200)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 200


class MCPToolWithRetry(BaseTool):
    """
    MCP 工具包装器，添加重试逻辑

    包装原始 MCP 工具，在调用失败时自动重试。
    """

    # 以下均为 Pydantic 私有属性（不参与 schema），承载包装器自身的运行时状态
    _original_tool: BaseTool = PrivateAttr()
    _max_retries: int = PrivateAttr(default=MCP_MAX_RETRIES)
    _retry_delay: float = PrivateAttr(default=MCP_RETRY_DELAY)
    _user_id: str | None = PrivateAttr(default=None)
    _server_name: str | None = PrivateAttr(default=None)
    _user_roles: list[str] = PrivateAttr(default_factory=list)
    _is_admin: bool = PrivateAttr(default=False)
    # 角色配额：role -> 配额定义，用于调用前的配额检查与扣减
    _role_quotas: dict[str, MCPRoleQuota] = PrivateAttr(default_factory=dict)
    _quota_tool_name: str | None = PrivateAttr(default=None)

    def __init__(
        self,
        original_tool: BaseTool,
        max_retries: int = MCP_MAX_RETRIES,
        retry_delay: float = MCP_RETRY_DELAY,
        user_id: str | None = None,
        server_name: str | None = None,
        user_roles: list[str] | None = None,
        is_admin: bool = False,
        role_quotas: Mapping[str, MCPRoleQuota | dict[str, Any]] | None = None,
        quota_tool_name: str | None = None,
    ):
        # 复用原始工具的名称/描述/参数 schema，保证对上层（Agent）透明
        super().__init__(
            name=original_tool.name,
            description=original_tool.description,
            args_schema=original_tool.args_schema,
        )
        self._original_tool = original_tool
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._user_id = user_id
        self._server_name = server_name
        self._user_roles = list(user_roles or [])
        self._is_admin = is_admin
        # 归一化角色配额：允许传入 MCPRoleQuota 实例或原始 dict，统一转成模型对象
        self._role_quotas = {
            role_name: quota
            if isinstance(quota, MCPRoleQuota)
            else MCPRoleQuota.model_validate(quota)
            for role_name, quota in (role_quotas or {}).items()
        }
        self._quota_tool_name = quota_tool_name
        if server_name:
            # 绕过 Pydantic 的属性拦截，直接在实例上挂 server 字段，便于外部识别归属服务器
            object.__setattr__(self, "server", server_name)

    def _is_retryable_error(self, error: Exception) -> bool:
        """判断错误是否可重试"""
        error_str = str(error).lower()
        error_type = type(error).__name__.lower()

        # 可重试的错误模式
        # 按错误消息中的关键字匹配：限流/网关/超时/连接类错误通常是暂时性的
        retryable_patterns = [
            "429",  # rate limit
            "503",  # service unavailable
            "502",  # bad gateway
            "timeout",
            "connection",
            "network",
            "reset",
            "refused",
            "unavailable",
        ]

        # 可重试的异常类型
        # 按异常类名匹配，兜住不带上述关键字消息的连接类异常
        retryable_types = [
            "timeouterror",
            "connectionerror",
            "connectionreseterror",
            "brokenpipeerror",
        ]

        # 检查错误消息
        if any(pattern in error_str for pattern in retryable_patterns):
            return True

        # 检查异常类型
        if any(rt in error_type for rt in retryable_types):
            return True

        # 空错误消息可能是临时问题，也重试
        if not error_str or error_str == "(empty error message)":
            return True

        return False

    def _run(self, *args, **kwargs) -> Any:
        """同步运行（不支持重试，直接调用原始工具）"""
        return self._original_tool._run(*args, **kwargs)

    async def _arun(self, *args, config: Optional[RunnableConfig] = None, **kwargs) -> Any:
        """
        异步运行（带超时和重试，失败时返回错误信息而不是抛出异常）

        即使 MCP 工具最终失败，也不会中断 Agent 执行，
        而是返回包含错误信息的字符串，让 Agent 可以继续处理。

        Args:
            *args: 位置参数
            config: LangChain RunnableConfig（可选）
            **kwargs: 关键字参数
        """
        # 若配置了角色配额，先做配额检查与扣减；超额则直接返回结构化错误，不再执行工具
        if self._role_quotas:
            from src.infra.mcp.quota import (
                check_and_consume_mcp_quota,
                quota_error_json_async,
            )

            quota_result = await check_and_consume_mcp_quota(
                user_id=self._user_id,
                server_name=self._server_name,
                tool_name=self._quota_tool_name,
                user_roles=self._user_roles,
                role_quotas=self._role_quotas,
                is_admin=self._is_admin,
            )
            if not quota_result.allowed:
                return await quota_error_json_async(self._server_name or self.name, quota_result)

        last_error: Exception | None = None
        # 重试主循环：最多 _max_retries 次
        for attempt in range(self._max_retries):
            try:
                # 使用 wait_for 添加超时
                # 为单次调用加超时保护，防止某个 MCP 服务器长时间无响应拖住 Agent
                return await asyncio.wait_for(
                    self._original_tool._arun(*args, config=config, **kwargs),
                    timeout=MCP_TOOL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # 超时视为可重试：记录并在未达上限时退避重试，否则返回错误串
                last_error = TimeoutError(f"Tool timed out after {MCP_TOOL_TIMEOUT}s")
                logger.warning(
                    f"MCP tool '{self.name}' timed out (attempt {attempt + 1}/{self._max_retries})"
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(self._retry_delay)
                else:
                    error_msg = (
                        f"[MCP Tool Error] {self.name} timed out after {self._max_retries} attempts"
                    )
                    logger.error(error_msg)
                    return error_msg
            except Exception as e:
                last_error = e
                # 详细记录异常信息，帮助诊断问题
                error_type = type(e).__name__
                # 兜住空消息异常，避免日志里出现空白错误
                error_str = str(e) if str(e) else "(empty error message)"
                error_repr = repr(e)

                # 仅在"可重试且未达上限"时退避重试
                if self._is_retryable_error(e) and attempt < self._max_retries - 1:
                    logger.warning(
                        f"MCP tool '{self.name}' failed (attempt {attempt + 1}/{self._max_retries}): "
                        f"[{error_type}] {error_str}. Retrying in {self._retry_delay}s..."
                    )
                    await asyncio.sleep(self._retry_delay)
                else:
                    # 非 429 错误不重试，但记录详细错误信息
                    # 如果是第一次尝试就失败且不是可重试错误，也显示 "after 3 attempts" 以保持一致性
                    actual_attempts = attempt + 1
                    # 关键设计：失败时返回错误字符串而非抛异常，让 Agent 继续对话
                    error_msg = (
                        f"[MCP Tool Error] {self.name} failed after {actual_attempts} attempt(s): "
                        f"[{error_type}] {error_str}"
                    )
                    logger.error(
                        f"[MCP Tool Error] {self.name} failed: type={error_type}, "
                        f"str={error_str}, repr={error_repr}"
                    )
                    return error_msg
        # 不应该到达这里，但以防万一
        return f"[MCP Tool Error] {self.name} failed: {last_error}"


class MCPClientManager:
    """
    MCP 客户端管理器

    管理与多个 MCP 服务器的连接，提供工具发现和调用。
    使用 langchain-mcp-adapters 实现实际的 MCP 连接。

    支持两种配置源：
    1. 数据库（推荐）：用户级配置，支持数据隔离
    2. 文件（兼容）：从 mcp.json 读取配置
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        user_id: Optional[str] = None,
        use_database: bool = True,
    ):
        """
        初始化 MCP 客户端管理器

        Args:
            config_path: MCP 配置文件路径（仅当 use_database=False 时使用）
            user_id: 用户 ID（用于获取用户特定的 MCP 配置）
            use_database: 是否从数据库读取配置（默认 True）
        """
        self._config_path = config_path
        self._user_id = user_id
        self._use_database = use_database
        self._client: Optional[MultiServerMCPClient] = None
        self._tools: list[BaseTool] = []
        # (server_name, raw_tool_name) -> server_name：用于在多服务器同名工具时消歧
        self._tool_server_map: dict[tuple[str, str], str] = {}
        # tool_name -> server_name：便于按工具名反查所属服务器（含带前缀与不带前缀两种键）
        self._tool_name_server_map: dict[str, str] = {}
        # 每个服务器的角色配额与工具策略缓存，装配工具时用于配额与可见性控制
        self._server_role_quotas: dict[str, dict[str, MCPRoleQuota]] = {}
        self._server_tool_policies: dict[str, dict[str, MCPToolPolicy]] = {}
        self._user_roles: list[str] = []
        self._is_admin = False
        # 初始化幂等标记：避免重复建立连接
        self._initialized = False

    async def initialize(self) -> None:
        """初始化 MCP 连接"""
        # 幂等：已初始化则直接返回
        if self._initialized:
            return

        try:
            # 尝试从数据库读取配置
            if self._use_database:
                config = await self._load_config_from_database()
                if config:
                    logger.info(
                        f"MCP config loaded from database for user {self._user_id}: {list(config.get('mcpServers', {}).keys())}"
                    )
                    await self._initialize_with_config(config)
                    return
                else:
                    # 数据库查询成功但没有启用的服务器 - 这是用户的偏好，不应该回退到文件
                    # 关键语义：空配置代表"用户明确不启用任何服务器"，不同于"读取失败"
                    logger.info(
                        f"No enabled MCP servers for user {self._user_id}, skipping file fallback"
                    )
                    self._initialized = True
                    return

            # 从文件读取配置（向后兼容，仅当 use_database=False 时）
            config = await self._load_config_from_file()
            if config:
                await self._initialize_with_config(config)
            else:
                logger.warning("No MCP configuration found")
                self._initialized = True

        except Exception as e:
            # 初始化失败不抛出：置初始化标记为 True，保证 Agent 在无 MCP 工具时仍可运行
            logger.error(f"Failed to initialize MCP client: {e}")
            import traceback

            traceback.print_exc()
            self._initialized = True  # 不阻止后续执行

    async def _load_config_from_database(self) -> Optional[dict]:
        """
        从数据库加载 MCP 配置

        Returns:
            dict: 配置字典（可能包含空的 mcpServers）
            None: 数据库查询失败
        """
        try:
            from src.infra.mcp.storage import MCPStorage

            storage = MCPStorage()

            # 如果指定了 user_id，获取用户特定的配置
            # 否则只获取系统配置（用于系统级初始化）
            if self._user_id:
                # Resolve user's MCP access for role-based server filtering
                # 先解析用户角色与管理员标记，供按角色过滤服务器/工具使用
                from src.infra.mcp.quota import resolve_user_mcp_access

                user_roles, is_admin = await resolve_user_mcp_access(self._user_id)
                self._user_roles = user_roles
                self._is_admin = is_admin
                # 获取"有效配置"：已按用户角色/管理员权限过滤后的服务器集合
                config = await storage.get_effective_config(
                    self._user_id,
                    user_roles=user_roles,
                    is_admin=is_admin,
                )
                logger.info(
                    f"Loaded MCP config for user {self._user_id}: {len(config.get('mcpServers', {}))} servers"
                )
            else:
                # 没有用户 ID 时，只获取系统配置
                # 仅装配已启用的系统级服务器（用于无用户上下文的系统初始化）
                system_servers = await storage.list_system_servers()
                servers = {}
                for server in system_servers:
                    if server.enabled:
                        servers[server.name] = self._server_to_config_dict(server)
                config = {"mcpServers": servers}
                logger.info(f"Loaded system MCP config: {len(servers)} servers")

            # 返回配置（即使是空的），让调用者决定如何处理
            return config

        except Exception as e:
            # 数据库读取失败返回 None（区别于"空配置"），上层据此可决定是否回退
            logger.warning(f"Failed to load MCP config from database: {e}")
            import traceback

            traceback.print_exc()
            return None

    def _load_config_from_file_sync(self) -> Optional[dict]:
        """从文件加载 MCP 配置（同步版本，内部使用）"""
        config_path = self._config_path
        if not config_path or not os.path.exists(config_path):
            logger.warning(f"MCP config file not found: {config_path}")
            return None

        # 文件大小硬限制，防御性拒绝超大配置文件
        file_size = os.path.getsize(config_path)
        if file_size > MCP_CONFIG_FILE_MAX_BYTES:
            logger.warning(
                "MCP config file too large: %s bytes (max %s): %s",
                file_size,
                MCP_CONFIG_FILE_MAX_BYTES,
                config_path,
            )
            return None

        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        if not config.get("mcpServers"):
            logger.warning("No MCP servers configured in mcp.json")
            return None

        return config

    async def _load_config_from_file(self) -> Optional[dict]:
        """从文件加载 MCP 配置（异步版本）"""
        # 文件 I/O 放到线程池执行，避免阻塞事件循环
        return await run_blocking_io(self._load_config_from_file_sync)

    def _server_to_config_dict(self, server) -> dict:
        """将服务器对象转换为配置字典"""
        # 把存储层的服务器实体裁剪为 langchain-mcp-adapters 需要的最小连接配置
        result = {"transport": server.transport.value}

        if server.url:
            result["url"] = server.url
        if server.headers:
            result["headers"] = server.headers

        return result

    async def _initialize_with_config(self, config: dict) -> None:
        """使用配置初始化 MCP 客户端"""
        mcp_servers = config.get("mcpServers", {})
        if not mcp_servers:
            self._initialized = True
            return

        # 直接创建 MCP 客户端（不使用缓存，避免循环调用）
        # 全局缓存逻辑在 mcp_global.py 的 get_global_mcp_tools 中处理
        tools, client = await self._create_mcp_client(config)
        self._tools = tools
        self._client = client
        self._initialized = True

    async def _create_mcp_client(
        self, config: dict
    ) -> tuple[list[BaseTool], Optional[MultiServerMCPClient]]:
        """
        创建 MCP 客户端并获取工具

        Args:
            config: MCP 配置字典

        Returns:
            tuple: (tools, client) - 工具列表和客户端
        """
        mcp_servers = config.get("mcpServers", {})
        if not mcp_servers:
            return [], None
        # 服务器数量上限保护：超出只加载前 max_servers 个
        max_servers = _get_effective_config_max_servers()
        if len(mcp_servers) > max_servers:
            logger.warning(
                "[MCP] Direct config contains %s server(s); only loading first %s",
                len(mcp_servers),
                max_servers,
            )

        # 转换配置格式以适配 langchain-mcp-adapters
        # Use dict[str, Any] to allow flexible key-value pairs for different transport types
        server_configs: dict[str, dict[str, Any]] = {}
        # 每次重建前清空上一轮缓存的配额/策略，避免残留旧服务器数据
        self._server_role_quotas.clear()
        self._server_tool_policies.clear()
        for server_name, server_config in list(mcp_servers.items())[:max_servers]:
            transport = server_config.get("transport", "streamable_http")
            # 归一化角色配额：允许 MCPRoleQuota 或 dict 两种输入
            role_quotas = server_config.get("role_quotas") or {}
            self._server_role_quotas[server_name] = {
                role_name: quota
                if isinstance(quota, MCPRoleQuota)
                else MCPRoleQuota.model_validate(quota)
                for role_name, quota in role_quotas.items()
            }
            # 归一化工具策略：同上
            tool_policies = server_config.get("tool_policies") or {}
            self._server_tool_policies[server_name] = {
                tool_name: policy
                if isinstance(policy, MCPToolPolicy)
                else MCPToolPolicy.model_validate(policy)
                for tool_name, policy in tool_policies.items()
            }

            if transport in ("sse", "streamable_http"):
                # HTTP 传输：SSE 或 streamable HTTP
                # 仅支持 HTTP 类传输（当前不装配 stdio 等本地传输）
                server_configs[server_name] = {
                    "url": server_config.get("url"),
                    "transport": transport,
                }
                # 添加 headers（如 Authorization）
                if server_config.get("headers"):
                    server_configs[server_name]["headers"] = server_config["headers"]
            else:
                # 不支持的传输类型直接跳过
                logger.debug(
                    f"Skipping MCP server '{server_name}': unsupported transport '{transport}'"
                )
                continue

        # 创建 MultiServerMCPClient
        client = MultiServerMCPClient(server_configs)  # type: ignore[arg-type]

        # 并行加载所有服务器的工具，失败的服务器不影响其他服务器
        async def _load_server_tools(
            server_name: str,
        ) -> tuple[str, list[BaseTool] | Exception]:
            """加载单个服务器的工具，返回 (server_name, tools) 或 (server_name, exception)

            重要：只传递 connection 参数，不传递 session。
            这样工具在调用时会自己创建会话，避免会话提前关闭的问题。
            """
            try:
                from langchain_mcp_adapters.tools import load_mcp_tools

                # 获取服务器的 connection 配置
                connection = server_configs.get(server_name)
                if not connection:
                    return (
                        server_name,
                        ValueError(f"No connection config for server '{server_name}'"),
                    )

                # 关键：只传递 connection，不传递 session
                # 这样工具会保存 connection，在每次调用时创建新的会话
                # 避免共享长连接会话在并发调用下过早关闭导致的报错
                tools = await load_mcp_tools(
                    session=None,  # 不传递 session
                    connection=connection,  # type: ignore[arg-type]
                    server_name=server_name,
                )
                return (server_name, tools)
            except Exception as e:
                # 单服务器加载失败以异常形式回传，交由汇总阶段统一处理，不中断其他服务器
                return (server_name, e)

        server_names = list(server_configs)
        results: list[tuple[str, list[BaseTool] | Exception]] = []
        # next_index + lock 构成一个简单的"工作队列游标"，供多个 worker 抢占取任务
        next_index = 0
        lock = asyncio.Lock()
        # 并发度：受配置 MCP_SERVER_LOAD_CONCURRENCY 与服务器数量共同约束
        concurrency = min(
            max(1, int(getattr(settings, "MCP_SERVER_LOAD_CONCURRENCY", 4) or 1)),
            len(server_names),
        )

        async def _load_worker() -> None:
            # 工作协程：循环从游标取下一个服务器加载，直到取尽
            nonlocal next_index
            while True:
                async with lock:
                    if next_index >= len(server_names):
                        return
                    server_name = server_names[next_index]
                    next_index += 1
                results.append(await _load_server_tools(server_name))

        # 启动 concurrency 个 worker 并行拉取，限制同时建立的连接数
        await asyncio.gather(*(_load_worker() for _ in range(concurrency)))

        # 处理结果
        all_tools: list[BaseTool] = []
        failed_servers: list[str] = []
        # Track which server each tool belongs to: (server_name, raw_tool_name) -> server_name
        # 汇总前清空映射，重新建立"工具 -> 服务器"归属关系
        self._tool_server_map.clear()
        self._tool_name_server_map.clear()
        # 全局工具数量上限
        max_tools = _get_effective_config_max_tools()

        for server_name, result in results:
            if isinstance(result, Exception):
                # 加载失败：记录到 failed_servers，并按错误类型给出更友好的日志
                failed_servers.append(server_name)
                error_msg = str(result)
                if "ValidationError" in error_msg or "JSONRPCMessage" in error_msg:
                    # 典型症状：目标其实不是合规的 MCP 服务器
                    logger.warning(
                        f"[MCP] Server '{server_name}' returned invalid JSON-RPC response. "
                        f"This server may not be MCP-compliant."
                    )
                else:
                    logger.warning(
                        f"[MCP] Failed to load tools from server '{server_name}': {result}"
                    )
            else:
                # 成功：按剩余额度截断，避免总工具数超过 max_tools
                remaining = max_tools - len(all_tools)
                if remaining <= 0:
                    logger.warning(
                        "[MCP] Tool load limit reached (%s); skipping %s tool(s) from server '%s'",
                        max_tools,
                        len(result),
                        server_name,
                    )
                    continue
                kept_tools = result[:remaining]
                skipped_count = len(result) - len(kept_tools)
                for tool in kept_tools:
                    # Map (server_name, raw_tool_name) to avoid conflicts when
                    # multiple servers expose tools with the same name
                    raw_name = tool.name
                    # langchain-mcp-adapters may prefix with "server_name:"
                    # 去掉可能的 "server_name:" 前缀，得到原始工具名，便于策略匹配与反查
                    if raw_name.startswith(f"{server_name}:"):
                        raw_name = raw_name[len(server_name) + 1 :]
                    # 同时登记 (server, raw_name) 与两种工具名，确保后续都能查到归属服务器
                    self._tool_server_map[(server_name, raw_name)] = server_name
                    self._tool_name_server_map[tool.name] = server_name
                    self._tool_name_server_map[raw_name] = server_name
                all_tools.extend(kept_tools)
                if skipped_count:
                    logger.warning(
                        "[MCP] Loaded %s/%s tools from server '%s' due to total tool limit %s",
                        len(kept_tools),
                        len(result),
                        server_name,
                        max_tools,
                    )
                else:
                    logger.info(f"[MCP] Loaded {len(result)} tools from server '{server_name}'")

        if failed_servers:
            logger.warning(
                f"[MCP] {len(failed_servers)} server(s) failed: {failed_servers}. "
                f"Loaded {len(all_tools)} tools from {len(server_configs) - len(failed_servers)} server(s)."
            )
        else:
            logger.info(
                f"[MCP] Successfully loaded {len(all_tools)} tools from {len(server_configs)} server(s)"
            )

        return all_tools, client

    def _server_for_tool(self, tool: BaseTool) -> str | None:
        # 推断某工具归属的服务器名，多路兜底：
        # 1) 工具对象上挂的 server 属性；2) 工具名映射表；3) "server:" 前缀解析
        server = getattr(tool, "server", None)
        if isinstance(server, str) and server:
            return server
        name = getattr(tool, "name", "")
        if name in self._tool_name_server_map:
            return self._tool_name_server_map[name]
        if ":" in name:
            candidate = name.split(":", 1)[0]
            if candidate in self._server_role_quotas:
                return candidate
        return None

    async def _connect_server(self, name: str, config: dict) -> None:
        """连接到单个 MCP 服务器（已弃用，由 MultiServerMCPClient 统一管理）"""
        pass

    async def get_tools(self) -> list[BaseTool]:
        """
        获取所有可用的 MCP 工具（带重试包装）

        每个工具都被包装在 MCPToolWithRetry 中，提供自动重试能力。
        会过滤掉与 deepagents backend 冲突的工具名。
        """
        if not self._initialized:
            await self.initialize()

        # 过滤掉与 deepagents backend 冲突的工具
        filtered_tools = []
        skipped_tools = []
        for tool in self._tools:
            # 剔除保留名工具，避免与内置文件/命令工具冲突
            if tool.name in CONFLICTING_TOOL_NAMES:
                skipped_tools.append(tool.name)
                continue

            # 验证工具的 args_schema 是否可以生成 JSON schema
            # 如果工具的参数 schema 包含 Pydantic 无法处理的类型（如 Callable），则会失败
            try:
                if hasattr(tool, "args_schema") and tool.args_schema:
                    if not isinstance(tool.args_schema, dict):
                        # 尝试生成 schema，如果失败则跳过该工具
                        # 提前触发一次 schema 生成，把"无法序列化"的工具在装配阶段挡掉，
                        # 而不是等到 LLM 绑定工具时才崩溃
                        tool.args_schema.schema()
            except Exception as e:
                # Pydantic 无法为该工具生成 schema，跳过该工具
                logger.warning(
                    f"[MCP] Skipping tool '{tool.name}' due to schema generation error: {e}"
                )
                skipped_tools.append(tool.name)
                continue

            filtered_tools.append(tool)

        if skipped_tools:
            logger.info(f"[MCP] Filtered out {len(skipped_tools)} tools: {skipped_tools}")

        # 包装工具以添加重试逻辑
        wrapped_tools: list[BaseTool] = []
        for tool in filtered_tools:
            # 确定归属服务器与原始工具名，用于匹配工具策略/配额
            server_name = self._server_for_tool(tool)
            raw_tool_name = getattr(tool, "name", "")
            if server_name and raw_tool_name.startswith(f"{server_name}:"):
                raw_tool_name = raw_tool_name[len(server_name) + 1 :]
            policy = self._server_tool_policies.get(server_name or "", {}).get(raw_tool_name)
            # 命中"禁用"或"当前用户角色不被允许"的策略则跳过该工具
            if policy and (policy.disabled or not self._is_tool_allowed(policy.allowed_roles)):
                continue
            # 统一包裹重试/超时/配额逻辑；配额优先取工具级策略，否则回退服务器级配额
            wrapped_tools.append(
                MCPToolWithRetry(
                    tool,
                    user_id=self._user_id,
                    server_name=server_name,
                    user_roles=self._user_roles,
                    is_admin=self._is_admin,
                    role_quotas=(
                        policy.role_quotas
                        if policy
                        else self._server_role_quotas.get(server_name or "", {})
                    ),
                    quota_tool_name=raw_tool_name if policy else None,
                )
            )
        return wrapped_tools

    def _is_tool_allowed(self, allowed_roles: list[str] | None) -> bool:
        # 判断当前用户是否可见某工具：管理员/未限定角色一律放行，否则要求角色有交集
        if self._is_admin:
            return True
        if not allowed_roles:
            return True
        return bool(set(self._user_roles).intersection(allowed_roles))

    def _is_mcp_retryable_error(self, error: Exception) -> bool:
        """判断 MCP 错误是否可重试"""
        error_str = str(error).lower()
        # 参数错误、超时、连接错误等可重试
        # 注意：此处 call_tool 路径较保守，仅对 429 限流做重试
        retryable_patterns = [
            "429",  # rate limit
        ]
        return any(pattern in error_str for pattern in retryable_patterns)

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """
        调用 MCP 工具（带重试，失败时返回错误信息）

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            工具执行结果，或错误信息字符串
        """
        # 在已加载工具中按名查找并调用；这里操作的是未包装的原始工具
        for tool in self._tools:
            if tool.name == tool_name:
                last_error = None
                for attempt in range(MCP_MAX_RETRIES):
                    try:
                        return await tool.ainvoke(arguments)
                    except Exception as e:
                        last_error = e
                        # 仅限流类错误重试，其余错误直接返回错误信息
                        if self._is_mcp_retryable_error(e) and attempt < MCP_MAX_RETRIES - 1:
                            logger.warning(
                                f"MCP tool '{tool_name}' failed (attempt {attempt + 1}/{MCP_MAX_RETRIES}): {e}. "
                                f"Retrying in {MCP_RETRY_DELAY}s..."
                            )
                            await asyncio.sleep(MCP_RETRY_DELAY)
                        else:
                            # 所有重试都失败，返回错误信息
                            error_msg = f"[MCP Tool Error] {tool_name} failed after {MCP_MAX_RETRIES} attempts: {last_error}"
                            logger.error(error_msg)
                            return error_msg
                # 不应该到达这里
                return f"[MCP Tool Error] {tool_name} failed: {last_error}"
        # 未找到同名工具
        return f"[MCP Tool Error] Tool '{tool_name}' not found"

    async def close(self) -> None:
        """关闭所有 MCP 连接"""
        # MultiServerMCPClient 的连接会在对象销毁时自动清理
        # 这里只清空本地引用与缓存状态，并复位初始化标记
        self._client = None
        self._tools.clear()
        self._tool_server_map.clear()
        self._tool_name_server_map.clear()
        self._server_role_quotas.clear()
        self._server_tool_policies.clear()
        self._initialized = False


class MCPClient:
    """
    MCP 客户端（简化版）

    与单个 MCP 服务器通信。包装 MCPClientManager 提供简化的 API。
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        user_id: Optional[str] = None,
        use_database: bool = True,
    ):
        # 内部委托给 MCPClientManager，本类只做一层更简单的门面 API
        self._manager = MCPClientManager(config_path, user_id, use_database)
        self._initialized = False

    async def connect(self, server_name: str) -> None:
        """连接到 MCP 服务器"""
        # 首次连接时惰性初始化底层管理器（server_name 在当前实现中未按需区分）
        if not self._initialized:
            await self._manager.initialize()
            self._initialized = True

    async def disconnect(self, server_name: str) -> None:
        """断开与 MCP 服务器的连接"""
        await self._manager.close()
        self._initialized = False

    async def list_tools(self, server_name: Optional[str] = None) -> list[dict]:
        """列出 MCP 服务器提供的工具"""
        if not self._initialized:
            await self._manager.initialize()
            self._initialized = True

        tools = self._manager._tools
        if server_name:
            # 过滤特定服务器的工具
            # 说明：当前实现两个分支返回相同结果（未真正按 server_name 过滤）
            return [{"name": t.name, "description": t.description} for t in tools]
        return [{"name": t.name, "description": t.description} for t in tools]

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict,
    ) -> Any:
        """调用 MCP 工具"""
        if not self._initialized:
            await self._manager.initialize()
            self._initialized = True
        return await self._manager.call_tool(tool_name, arguments)

    async def list_resources(self, server_name: str) -> list[dict]:
        """列出 MCP 服务器提供的资源"""
        # langchain-mcp-adapters 不直接暴露 resources API
        # 占位实现：当前适配器不支持 resources，返回空列表
        return []

    async def read_resource(
        self,
        server_name: str,
        resource_uri: str,
    ) -> Any:
        """读取 MCP 资源"""
        # langchain-mcp-adapters 不直接暴露 resources API
        # 占位实现：同上，暂不支持读取资源
        return None

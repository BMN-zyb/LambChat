"""
Agent 路由

提供 Agent 列表和流式聊天接口。
每个 Agent 就是一个 Graph，流式请求接入 graph 后输出 SSE 事件。
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Optional, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src.agents.core.base import AgentFactory
from src.api.deps import get_current_user_optional, get_current_user_required
from src.api.routes.chat import validate_agent_model_access
from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.kernel.config import settings
from src.kernel.exceptions import AuthorizationError
from src.kernel.schemas.agent import (
    AgentRequest,
    ToolInfo,
    ToolParamInfo,
    ToolsListResponse,
)
from src.kernel.schemas.user import TokenPayload

router = APIRouter()
logger = get_logger(__name__)

# 查询用户角色对应 agents 时的并发上限（避免一次性发起过多并发查询压垮下游）
AGENT_ROLE_FETCH_CONCURRENCY = 8
# 单次请求处理的角色数量上限（去重后最多取前 100 个角色，防止异常的超大角色列表）
AGENT_ROLE_LOOKUP_LIMIT = 100
# 单条 SSE 事件 data 字段的最大字节数（256KB）；超过则改发 error 事件，防止超大 payload
AGENT_SSE_DATA_MAX_BYTES = 256 * 1024
# 泛型类型变量，供下面的并发工具函数复用
T = TypeVar("T")


# 并发上限受控的 gather：给定一组"返回 awaitable 的工厂函数"，用固定数量的 worker 并发执行，
# 最多同时运行 limit 个，结果按原始顺序回填。用于并行查询角色-agents 又不至于压垮下游服务。
async def _gather_limited(
    factories: list[Callable[[], Awaitable[T]]],
    *,
    limit: int = AGENT_ROLE_FETCH_CONCURRENCY,
) -> list[T]:
    if not factories:
        return []

    # 预分配结果数组，按索引回填以保持与输入相同的顺序
    results: list[T | None] = [None] * len(factories)
    # 共享游标：下一个待处理任务的下标，由锁保护
    next_index = 0
    lock = asyncio.Lock()
    # worker 数量不超过任务数，且至少为 1
    worker_count = min(max(1, limit), len(factories))

    # 每个 worker 循环领取任务下标并执行，直到没有剩余任务
    async def _worker() -> None:
        nonlocal next_index
        while True:
            async with lock:
                if next_index >= len(factories):
                    return
                index = next_index
                next_index += 1
            results[index] = await factories[index]()

    # 启动 worker_count 个 worker 并发消费任务队列
    await asyncio.gather(*(_worker() for _ in range(worker_count)))
    # 过滤掉 None（工厂返回 None 的结果不纳入）
    return [result for result in results if result is not None]


# 对角色名列表做"去重 + 限量"：保持原有顺序去掉重复项，最多保留 AGENT_ROLE_LOOKUP_LIMIT 个，
# 防止异常的超长角色列表拖垮后续查询。
def _bounded_role_names(role_names: list[str] | None) -> list[str]:
    if not role_names:
        return []
    bounded = []
    seen = set()
    # 按出现顺序去重，达到上限即停止
    for role_name in role_names:
        if role_name in seen:
            continue
        seen.add(role_name)
        bounded.append(role_name)
        if len(bounded) >= AGENT_ROLE_LOOKUP_LIMIT:
            break
    return bounded


# 把"角色ID -> agents 列表"映射转换为"角色ID -> agents 数量"，仅用于日志统计（None 视为 0）。
def _role_agent_counts(role_agent_map: dict[str, list[str] | None]) -> dict[str, int]:
    return {role_id: len(agents or []) for role_id, agents in role_agent_map.items()}


# 估算数据序列化为 JSON 后的字节数（不真正生成完整字符串），用于在序列化前快速判断是否超过 SSE 大小上限。
# 各分支近似计入引号、括号、逗号、冒号等结构字符的开销（str +2 引号；dict/list 起始 +2 括号、元素间 +1 逗号；键额外 +3）。
def _estimated_json_data_bytes(data: object) -> int:
    if data is None or isinstance(data, (bool, int, float)):
        return len(json.dumps(data).encode("utf-8"))
    if isinstance(data, str):
        return len(data.encode("utf-8")) + 2
    # 字典：2 个花括号，元素间逗号，键含引号与冒号（+3），值递归估算
    if isinstance(data, dict):
        total = 2
        for index, (key, value) in enumerate(data.items()):
            if index:
                total += 1
            total += len(str(key).encode("utf-8")) + 3
            total += _estimated_json_data_bytes(value)
        return total
    # 列表/元组：2 个方括号，元素间逗号，元素递归估算
    if isinstance(data, (list, tuple)):
        total = 2
        for index, item in enumerate(data):
            if index:
                total += 1
            total += _estimated_json_data_bytes(item)
        return total
    # 兜底：其他类型按其字符串形式加引号估算
    return len(str(data).encode("utf-8")) + 2


# 生成一个"payload 过大"的 SSE 错误事件文本，用于替换超过大小上限的原始事件。
def _agent_sse_payload_too_large_event() -> str:
    return 'event: error\ndata: {"error":"event_payload_too_large"}\n\n'


# 将 data 序列化为 JSON 字符串，但带大小上限：超过 AGENT_SSE_DATA_MAX_BYTES 时返回 None（交由上层改发错误事件）。
# 先用估算值快速拦截；再用增量编码器（iterencode）边生成边累计字节数，一旦超限立即中止，避免生成超大字符串。
def _json_dumps_agent_sse_data_limited(data: object) -> str | None:
    if _estimated_json_data_bytes(data) > AGENT_SSE_DATA_MAX_BYTES:
        return None

    # ensure_ascii=False 以保留中文等非 ASCII 字符原样输出
    encoder = json.JSONEncoder(ensure_ascii=False)
    chunks: list[str] = []
    total = 0
    # 边增量编码边累计字节数，超限则提前放弃
    for chunk in encoder.iterencode(data):
        total += len(chunk.encode("utf-8"))
        if total > AGENT_SSE_DATA_MAX_BYTES:
            return None
        chunks.append(chunk)
    return "".join(chunks)


# 把内部事件字典 {"event": ..., "data": ...} 格式化为标准 SSE 帧文本："event: <名>\ndata: <内容>\n\n"。
# data 为字符串则直接使用（超限改发错误事件）；否则序列化为 JSON（同样受大小上限约束）。
def _format_agent_sse_event(event: dict) -> str:
    data = event["data"]
    # data 已是字符串：仅做大小检查后直接作为 data 输出
    if isinstance(data, str):
        if len(data.encode("utf-8")) > AGENT_SSE_DATA_MAX_BYTES:
            return _agent_sse_payload_too_large_event()
        data_str = data
    # data 是对象：序列化为受大小上限约束的 JSON；超限则改发错误事件
    else:
        rendered_data = _json_dumps_agent_sse_data_limited(data)
        if rendered_data is None:
            return _agent_sse_payload_too_large_event()
        data_str = rendered_data
    return f"event: {event['event']}\ndata: {data_str}\n\n"


# 内置工具定义（带参数）
BUILTIN_TOOLS = [
    ToolInfo(
        name="read_file",
        description="读取文件内容",
        category="sandbox",
        parameters=[
            ToolParamInfo(name="file_path", type="string", description="文件路径", required=True),
        ],
    ),
    ToolInfo(
        name="write_file",
        description="写入文件",
        category="sandbox",
        parameters=[
            ToolParamInfo(name="file_path", type="string", description="文件路径", required=True),
            ToolParamInfo(name="content", type="string", description="文件内容", required=True),
        ],
    ),
    ToolInfo(
        name="edit_file",
        description="编辑文件",
        category="sandbox",
        parameters=[
            ToolParamInfo(name="file_path", type="string", description="文件路径", required=True),
            ToolParamInfo(
                name="old_string",
                type="string",
                description="要替换的内容",
                required=True,
            ),
            ToolParamInfo(name="new_string", type="string", description="新内容", required=True),
        ],
    ),
    ToolInfo(
        name="ls",
        description="列出目录内容",
        category="sandbox",
        parameters=[
            ToolParamInfo(name="path", type="string", description="目录路径", required=False),
        ],
    ),
    ToolInfo(
        name="glob",
        description="按模式搜索文件",
        category="sandbox",
        parameters=[
            ToolParamInfo(name="pattern", type="string", description="glob 模式", required=True),
            ToolParamInfo(name="path", type="string", description="搜索路径", required=False),
        ],
    ),
    ToolInfo(
        name="grep",
        description="在文件中搜索内容；避免全局搜索，先缩小路径范围再搜",
        category="sandbox",
        parameters=[
            ToolParamInfo(
                name="pattern",
                type="string",
                description="正则表达式模式",
                required=True,
            ),
            ToolParamInfo(name="path", type="string", description="搜索路径", required=False),
        ],
    ),
    ToolInfo(
        name="bash",
        description="执行 shell 命令",
        category="sandbox",
        parameters=[
            ToolParamInfo(name="command", type="string", description="要执行的命令", required=True),
        ],
    ),
]

# Sandbox MCP 管理工具定义
SANDBOX_MCP_TOOLS = [
    ToolInfo(
        name="sandbox_mcp_add",
        description="在沙箱中注册新的 MCP 服务器，并持久化到数据库",
        category="sandbox",
        parameters=[
            ToolParamInfo(
                name="server_name",
                type="string",
                description="服务器名称",
                required=True,
            ),
            ToolParamInfo(
                name="command",
                type="string",
                description="stdio 启动命令, 如 'npx @anthropic/mcp-server-fetch'",
                required=True,
            ),
            ToolParamInfo(
                name="env_keys",
                type="string",
                description="环境变量 KEY 名称，逗号分隔",
                required=False,
            ),
        ],
    ),
    ToolInfo(
        name="sandbox_mcp_update",
        description="更新沙箱中 MCP 服务器的命令或环境变量，并持久化到数据库",
        category="sandbox",
        parameters=[
            ToolParamInfo(
                name="server_name",
                type="string",
                description="服务器名称",
                required=True,
            ),
            ToolParamInfo(
                name="command",
                type="string",
                description="新的 stdio 命令（省略则不变更）",
                required=False,
            ),
            ToolParamInfo(
                name="env_keys",
                type="string",
                description="环境变量 KEY 名称，逗号分隔（省略则不变更）",
                required=False,
            ),
        ],
    ),
    ToolInfo(
        name="sandbox_mcp_remove",
        description="从沙箱中移除 MCP 服务器，并从数据库删除",
        category="sandbox",
        parameters=[
            ToolParamInfo(
                name="server_name",
                type="string",
                description="服务器名称",
                required=True,
            ),
        ],
    ),
]

# Human 工具定义
HUMAN_TOOLS = [
    ToolInfo(
        name="ask_human",
        description="请求人工输入",
        category="human",
        parameters=[
            ToolParamInfo(name="message", type="string", description="提示信息", required=True),
        ],
    ),
]


# 从工具对象中提取参数信息（ToolParamInfo 列表），供前端展示工具入参。
# 兼容两类工具：MCP 工具的 args_schema 可能直接是 dict，LangChain 工具则是 Pydantic 模型（需 .schema() 生成 JSON Schema）。
def extract_tool_parameters(tool) -> list[ToolParamInfo]:
    """从 LangChain 工具中提取参数信息"""
    parameters: list[ToolParamInfo] = []
    try:
        if hasattr(tool, "args_schema") and tool.args_schema:
            # MCP tools may have args_schema as a dict directly, while LangChain tools have Pydantic models
            if isinstance(tool.args_schema, dict):
                schema = tool.args_schema
            else:
                try:
                    schema = tool.args_schema.schema()
                except Exception as e:
                    # Pydantic may fail to generate schema for types like Callable
                    logger.warning(f"Failed to generate schema for tool {tool.name}: {e}")
                    return parameters
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))

            # 逐个参数：把 JSON Schema 的类型映射为前端展示用的简化类型（array->list、object->dict、integer/number->number 等）
            for param_name, param_info in properties.items():
                param_type = "string"
                if isinstance(param_info, dict):
                    param_type = param_info.get("type", "string")
                    if param_type == "array":
                        param_type = "list"
                    elif param_type == "object":
                        param_type = "dict"
                    elif param_type == "integer" or param_type == "number":
                        param_type = "number"
                    elif param_type == "boolean":
                        param_type = "boolean"

                parameters.append(
                    ToolParamInfo(
                        name=param_name,
                        type=param_type,
                        description=(
                            param_info.get("description", "")
                            if isinstance(param_info, dict)
                            else ""
                        ),
                        required=param_name in required,
                        default=(
                            param_info.get("default") if isinstance(param_info, dict) else None
                        ),
                    )
                )
    except Exception as e:
        logger.warning(f"Failed to extract parameters for tool {tool.name}: {e}")

    return parameters


# GET /api/agents —— 列出当前登录用户可用的 Agent 列表（认证可选：未登录则返回空列表）。
# 逻辑：取用户角色 -> 并行查询每个角色被授权的 agents 映射 -> 结合用户默认 Agent 偏好与模型授权，
# 交由 AgentFactory.get_filtered_agents 过滤出最终可见列表。响应含 agents、count、default_agent、allowed_model_ids。
@router.get("/agents")
async def list_agents(
    optional_user: Optional[TokenPayload] = Depends(get_current_user_optional),
):
    """列出当前用户可用的 Agent（按名称排序，默认 agent 排在最前面）"""
    from src.infra.agent.config_storage import get_agent_config_storage
    from src.infra.user.storage import UserStorage

    # 如果用户未登录，返回空列表
    if not optional_user:
        return {
            "agents": [],
            "count": 0,
            "default_agent": settings.DEFAULT_AGENT,
        }

    # 从数据库获取最新用户信息（包括角色）
    user_storage = UserStorage()
    db_user = await user_storage.get_by_id(optional_user.sub)

    # 使用数据库中的角色
    user_roles = _bounded_role_names(db_user.roles if db_user else optional_user.roles)
    logger.info(
        "[Agents API] user_id=%s, db_user_found=%s, user_role_count=%s, token_role_count=%s",
        optional_user.sub,
        db_user is not None,
        len(user_roles),
        len(optional_user.roles or []),
    )

    storage = get_agent_config_storage()

    # 获取用户的默认 agent 设置
    user_preference = await storage.get_user_preference(optional_user.sub)
    # 用户偏好优先：有偏好则用其默认 Agent，否则回退到系统默认（settings.DEFAULT_AGENT）
    default_agent = user_preference.default_agent_id if user_preference else settings.DEFAULT_AGENT

    # 获取用户角色的可用 agents 映射（使用角色ID作为key）
    role_agent_map: dict[str, list[str] | None] = {}
    role_ids = []  # 用户角色ID列表
    if user_roles:
        from src.infra.role.manager import get_role_manager

        role_manager = get_role_manager()

        # 并行查询所有角色信息，避免 N+1 问题
        # 单个角色的查询：先按名字查到角色，再取该角色被授权的 agents，返回 (角色ID, agents, 角色名)
        async def _fetch_role(role_name: str):
            role = await role_manager.get_role_by_name(role_name)
            if role:
                role_agents = await storage.get_role_agents(role.id)
                return role.id, role_agents, role_name
            return None

        role_factories: list[Callable[[], Awaitable[tuple[str, list[str], str] | None]]] = []
        for role_name in user_roles:

            # 用默认参数 role_name=role_name 绑定当前循环变量，避免闭包延迟绑定导致所有任务都用最后一个 role_name
            async def _fetch_current_role(
                role_name: str = role_name,
            ) -> tuple[str, list[str], str] | None:
                return await _fetch_role(role_name)

            role_factories.append(_fetch_current_role)

        role_results = await _gather_limited(role_factories)
        for result in role_results:
            if result is not None:
                rid, role_agents, role_name = result
                role_ids.append(rid)
                role_agent_map[rid] = role_agents
                logger.info(
                    "[Agents API] role_name=%s, role_id=%s, role_agent_count=%s",
                    role_name,
                    rid,
                    len(role_agents or []),
                )

    logger.info(
        "[Agents API] final role_count=%s, role_agent_counts=%s",
        len(role_ids),
        _role_agent_counts(role_agent_map),
    )

    from src.infra.agent.model_access import resolve_user_allowed_model_ids

    # 解析该用户（用数据库角色覆盖 token 中的角色）被授权的模型 id 集合，一并返回给前端做过滤
    allowed_model_ids = await resolve_user_allowed_model_ids(
        optional_user.model_copy(update={"roles": user_roles})
    )

    # 获取过滤后的 agents
    agents = await AgentFactory.get_filtered_agents(
        user_roles=role_ids,  # 传入角色ID列表
        role_agent_map=role_agent_map,
        default_agent_id=default_agent,
    )

    return {
        "agents": agents,
        "count": len(agents),
        "default_agent": default_agent,
        "allowed_model_ids": allowed_model_ids,
    }


# POST /api/{agent_id}/chat —— 非流式聊天（需登录认证）。
# 按 agent_id 取到对应 Agent，调用其 invoke() 一次性返回完整结果；未提供 session_id 时自动生成一个。
@router.post("/{agent_id}/chat")
async def chat(
    agent_id: str,
    request: AgentRequest,
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    非流式聊天接口

    调用 Agent.invoke() 并返回最终结果。
    """
    # 按 id 获取 Agent 实例（每个 Agent 即一个已编译的 Graph）
    agent = await AgentFactory.get(agent_id)
    # 同步式调用：等待整个流程结束再返回；session_id 缺省则用随机 uuid
    response = await agent.invoke(
        request.message,
        request.session_id or str(uuid.uuid4()),
    )
    return {"response": response}


# POST /api/{agent_id}/stream —— 流式聊天（需登录认证），以 SSE(text/event-stream) 持续推送事件。
# 每个 Agent 就是一个 CompiledGraph；agent.stream() 逐个产出事件，经 _format_agent_sse_event 格式化为 SSE 帧下发。
# session 绑定到当前用户；下方会先校验模型访问权限（无权则 403），再返回 StreamingResponse。
@router.post("/{agent_id}/stream")
async def chat_stream(
    agent_id: str,
    request_body: AgentRequest,
    request: Request,
    user: TokenPayload = Depends(get_current_user_required),
):
    """
    流式聊天接口

    调用 Agent.stream()，每个 Agent 就是一个 CompiledGraph。
    流式请求接入 graph，节点通过 config 获取 Presenter 输出 SSE 事件。
    需要认证，session 会绑定到当前用户。
    """
    # 按 id 获取 Agent 实例（每个 Agent 即一个已编译的 Graph）；session_id 缺省则生成随机 uuid
    agent = await AgentFactory.get(agent_id)
    session_id = request_body.session_id or str(uuid.uuid4())
    user_id = user.sub  # 在闭包外部捕获

    # 获取 base_url（用于生成完整的文件 URL）
    # 优先 APP_BASE_URL 环境变量，fallback 到 request.base_url
    base_url = getattr(settings, "APP_BASE_URL", "").rstrip("/")
    if not base_url:
        base_url = str(request.base_url).rstrip("/")
        if base_url == "http://None":
            base_url = ""

    # Pass all agent_options to the agent
    agent_options = request_body.agent_options or {}
    request_body.agent_options = agent_options
    logger.info(f"[API] request.agent_options: {request_body.agent_options}")
    logger.info(f"[API] agent_options to pass: {agent_options}")
    logger.info(f"[API] disabled_tools: {request_body.disabled_tools}")

    # 校验用户是否有权使用 agent_options 中指定的模型；无权则返回 403
    try:
        await validate_agent_model_access(agent_options, user)
    except AuthorizationError as e:
        raise HTTPException(status_code=403, detail=str(e))

    # SSE 事件生成器：异步迭代 agent.stream() 产出的事件并逐条格式化下发
    async def event_generator():
        try:
            async for event in agent.stream(
                request_body.message,
                session_id,
                user_id=user_id,
                disabled_tools=request_body.disabled_tools,
                agent_options=agent_options,
                base_url=base_url,
                disabled_skills=request_body.disabled_skills,
                disabled_mcp_tools=request_body.disabled_mcp_tools,
            ):
                # event 格式: {"event": "xxx", "data": {...}}
                # 确保 data 被正确序列化为 JSON
                # 在线程池中执行（可能较重的）JSON 序列化/格式化，避免阻塞事件循环
                yield await run_blocking_io(_format_agent_sse_event, event)
        finally:
            # 清理请求上下文，防止 contextvars 泄漏
            from src.infra.logging.context import TraceContext

            TraceContext.clear_request_context()

    # 以 text/event-stream 返回，前端按 SSE 协议持续接收事件流
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )


# GET /api/tools —— 获取当前用户可用的工具列表（需登录认证），响应模型 ToolsListResponse。
# 可选 query 参数 agent_id 用于判断该 Agent 是否支持沙箱，从而过滤沙箱类工具。
# Agent 发现机制：若 agent_id 尚未在 _AGENT_REGISTRY 中，则触发一次 discover_agents() 懒加载扫描并注册。
# 主体是汇总 MCP 工具：连接服务器拉取工具列表，并叠加系统级/用户级禁用状态与参数信息。
@router.get("/tools", response_model=ToolsListResponse)
async def list_tools(
    user: TokenPayload = Depends(get_current_user_required),
    agent_id: Optional[str] = Query(None, description="当前选中的 Agent ID，用于判断是否支持沙箱"),
):
    """
    获取当前用户可用的所有工具列表

    返回 Skill 工具、Human 工具和 MCP 工具的完整列表。
    MCP 工具会实际连接服务器获取工具列表、描述和参数。
    当传入 agent_id 时，根据该 Agent 是否支持沙箱来过滤沙箱类工具。
    """
    if agent_id:
        from src.agents.core.base import _AGENT_REGISTRY

        # Agent 发现机制：注册表中没有该 agent 时，懒加载扫描并注册所有 Agent
        if agent_id not in _AGENT_REGISTRY:
            from src.agents import discover_agents

            await run_blocking_io(discover_agents)
        agent_cls = _AGENT_REGISTRY.get(agent_id)
        if not agent_cls:
            logger.warning(
                f"[Tools API] Unknown agent_id={agent_id}, defaulting sandbox support to True"
            )

    tools: list[ToolInfo] = []

    # 1. MCP 工具 - 使用全局单例（分布式优化）
    if settings.ENABLE_MCP:
        try:
            from src.infra.mcp.storage import MCPStorage
            from src.infra.tool.mcp_global import get_global_mcp_tools

            # 使用全局单例，避免重复初始化
            mcp_tools, manager = await get_global_mcp_tools(user.sub)

            # 获取服务器名称映射（从 manager 的 _tool_server_map 或从工具名推断）
            tool_server_map = getattr(manager, "_tool_server_map", {}) if manager else {}

            # 获取系统级禁用的工具列表（管理员控制）
            mcp_storage = MCPStorage()
            system_disabled_tools = await mcp_storage.get_system_disabled_tools()

            # 获取用户服务器的 disabled_tools（创建者直接禁用）
            user_server_disabled_tools = await mcp_storage.get_user_server_disabled_tools(user.sub)

            # 合并系统级和用户服务器的禁用列表
            all_server_disabled = {
                **system_disabled_tools,
                **user_server_disabled_tools,
            }

            # 获取用户禁用的工具列表（从 user_mcp_tool_preferences 读取）
            user_disabled_tools = await mcp_storage.get_disabled_tool_names(user.sub)

            mcp_start_idx = len(tools)  # HUMAN tools are already in the list

            # 逐个 MCP 工具：解析所属服务器名、判断系统/用户是否禁用、提取描述与参数
            for tool in mcp_tools:
                tool_name = tool.name
                server_name = None

                # 1. 从 manager 的 tool_server_map 获取服务器名
                # 工具名可能是 "server_name:tool_name" 格式
                raw_name = tool_name
                if ":" in tool_name:
                    parts = tool_name.split(":", 1)
                    candidate_server = parts[0]
                    candidate_tool = parts[1]
                    # 在 map 中查找 (candidate_server, candidate_tool)
                    if (candidate_server, candidate_tool) in tool_server_map:
                        server_name = tool_server_map[(candidate_server, candidate_tool)]
                        raw_name = candidate_tool
                    else:
                        server_name = candidate_server
                        raw_name = candidate_tool
                elif tool_server_map:
                    # 工具名无 server 前缀时，从 tool_server_map 反查所属服务器
                    for (srv, raw), mapped_srv in tool_server_map.items():
                        if raw == tool_name:
                            server_name = mapped_srv
                            raw_name = raw
                            break

                # 2. 检查工具是否被系统禁用或用户禁用
                qualified_name = f"{server_name}:{raw_name}" if server_name else tool_name

                # 系统禁用检查（包括系统服务器和用户服务器的 disabled_tools）
                is_system_disabled = False
                if server_name and server_name in all_server_disabled:
                    if raw_name in all_server_disabled[server_name]:
                        is_system_disabled = True

                # 用户禁用检查
                is_user_disabled = (
                    qualified_name in user_disabled_tools or tool_name in user_disabled_tools
                )

                # 提取工具描述
                description = tool.description if hasattr(tool, "description") else ""

                # 提取参数信息
                parameters = extract_tool_parameters(tool)

                tools.append(
                    ToolInfo(
                        name=tool_name,
                        description=description,
                        category="mcp",
                        server=server_name,
                        parameters=parameters,
                        system_disabled=is_system_disabled,
                        user_disabled=is_user_disabled,
                    )
                )

            # 按 MCP 工具名称排序（首字母排序），HUMAN 工具保持在前
            tools[mcp_start_idx:] = sorted(tools[mcp_start_idx:], key=lambda t: t.name.lower())

            logger.info(
                f"[Tools API] Got {len(mcp_tools)} MCP tools from global cache for user {user.sub}"
            )

        except Exception as e:
            logger.warning(f"[Tools API] Failed to get MCP tools: {e}")

    return ToolsListResponse(tools=tools, count=len(tools))

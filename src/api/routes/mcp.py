"""
MCP (Model Context Protocol) API router

Provides endpoints for managing MCP server configurations.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import require_permissions
from src.infra.logging import get_logger
from src.infra.mcp.storage import MCPStorage
from src.kernel.schemas.mcp import (
    MCPExportResponse,
    MCPImportRequest,
    MCPImportResponse,
    MCPServerCreate,
    MCPServerMoveRequest,
    MCPServerMoveResponse,
    MCPServerResponse,
    MCPServersResponse,
    MCPServerToggleResponse,
    MCPServerUpdate,
    MCPToolDiscoveryResponse,
    MCPToolInfo,
    MCPToolPolicy,
    MCPToolPolicyUpdate,
    MCPToolToggleRequest,
    MCPToolToggleResponse,
)
from src.kernel.schemas.user import TokenPayload

logger = get_logger(__name__)

# 用户端 MCP 路由（挂载在 /api/mcp）：普通用户管理自建服务器、查看可见的系统服务器与探测工具
router = APIRouter()
# 管理端 MCP 路由（挂载在 /api/admin/mcp）：管理员管理系统级服务器与全局工具策略
admin_router = APIRouter()

# 单次导入 MCP 服务器配置的数量上限，防止一次导入过多
MCP_IMPORT_MAX_SERVERS = 100


# Dependency to get MCPStorage
# FastAPI 依赖：构造并注入 MCP 服务器配置存储实例
async def get_mcp_storage() -> MCPStorage:
    return MCPStorage()


# 统一处理服务器列表：按名称排序 -> 可选按关键字 q 过滤 -> 分页切片并返回总数
def _paginate_servers(
    servers: list[MCPServerResponse],
    *,
    skip: int,
    limit: int,
    q: str | None,
) -> MCPServersResponse:
    servers = sorted(servers, key=lambda server: server.name.lower())
    if q:
        lowered = q.lower()
        servers = [server for server in servers if lowered in server.name.lower()]
    total = len(servers)
    return MCPServersResponse(
        servers=servers[skip : skip + limit],
        total=total,
        skip=skip,
        limit=limit,
    )


def _is_admin(user: TokenPayload) -> bool:
    """Check if user has admin permissions"""
    return "mcp:admin" in (user.permissions or [])


# 判断是否为内置（internal）MCP 服务器：其工具由代码内置注册表提供，仅管理员可见
def _is_internal_server(name: str) -> bool:
    from src.infra.tool.internal_registry import INTERNAL_MCP_SERVER_NAME

    return name == INTERNAL_MCP_SERVER_NAME


# 校验用户是否有权创建/使用指定 transport 类型的服务器（管理员放行所有类型）
def _has_permission_for_transport(user: TokenPayload, transport: str) -> bool:
    """
    Check if user has permission for a specific transport type.

    Permissions:
    - mcp:admin: can create any transport type
    - mcp:write_sse: can create SSE transport
    - mcp:write_http: can create streamable_http transport
    - mcp:write_sandbox: can create sandbox transport
    """
    if _is_admin(user):
        return True

    permissions = user.permissions or []

    if transport == "sse":
        return "mcp:write_sse" in permissions
    elif transport == "streamable_http":
        return "mcp:write_http" in permissions
    elif transport == "sandbox":
        return "mcp:write_sandbox" in permissions

    return False


# ==========================================
# User API Endpoints - Static routes first
# ==========================================


# GET /api/mcp/ —— 列出当前用户可见的 MCP 服务器（系统服务器 + 用户自建）
# 需要 mcp:read 权限；管理员会额外附加内置服务器；支持分页(skip/limit)与名称搜索(q)
@router.get("/", response_model=MCPServersResponse)
async def list_servers(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    q: str | None = None,
    user: TokenPayload = Depends(require_permissions("mcp:read")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Get all visible MCP servers (system + user's own)"""
    storage_limit = skip + limit + 1
    servers = await storage.get_visible_servers(
        user.sub,
        is_admin=_is_admin(user),
        user_roles=user.roles,
        limit=storage_limit,
    )
    if _is_admin(user):
        from src.infra.tool.internal_registry import build_internal_server_response

        servers.append(build_internal_server_response())
    return _paginate_servers(servers, skip=skip, limit=limit, q=q)


# POST /api/mcp/ —— 创建一个用户自建的 MCP 服务器
# 需要对应 transport 类型的写权限（mcp:write_sse/http/sandbox）或 mcp:admin
# 名称不能与该用户已有服务器或系统服务器重名
@router.post("/", response_model=MCPServerResponse, status_code=201)
async def create_server(
    data: MCPServerCreate,
    user: TokenPayload = Depends(require_permissions("mcp:read")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Create a new MCP server (requires transport-specific permission)"""
    # Check permission for specific transport type
    if not _has_permission_for_transport(user, data.transport.value):
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied. Requires 'mcp:write_{data.transport.value}' or 'mcp:admin' permission.",
        )

    # Check if name already exists in user's servers
    existing = await storage.get_user_server(data.name, user.sub)
    if existing:
        raise HTTPException(status_code=400, detail=f"Server '{data.name}' already exists")

    # Also check system servers (users can't override with same name unless admin)
    system_existing = await storage.get_system_server(data.name)
    if system_existing:
        raise HTTPException(
            status_code=400,
            detail=f"Server '{data.name}' already exists as a system server",
        )

    server = await storage.create_user_server(data, user.sub)
    return MCPServerResponse(
        name=server.name,
        transport=server.transport,
        enabled=server.enabled,
        url=server.url,
        headers=server.headers,
        command=server.command,
        env_keys=server.env_keys,
        is_system=False,
        can_edit=True,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


# POST /api/mcp/import —— 从 JSON 配置批量导入 MCP 服务器
# 需要对每个服务器的 transport 类型具备写权限；超过数量上限返回 413；同名已存在则跳过
@router.post("/import", response_model=MCPImportResponse)
async def import_servers(
    data: MCPImportRequest,
    user: TokenPayload = Depends(require_permissions("mcp:read")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Import MCP servers from JSON configuration (requires transport-specific permission)"""
    # Check permissions for each server's transport type
    servers = data.get_servers()
    if len(servers) > MCP_IMPORT_MAX_SERVERS:
        raise HTTPException(
            status_code=413,
            detail=f"Import contains too many MCP servers (max {MCP_IMPORT_MAX_SERVERS})",
        )

    for server_name, server_config in servers.items():
        transport = server_config.get("transport", "streamable_http")
        if not _has_permission_for_transport(user, transport):
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied for server '{server_name}'. Requires 'mcp:write_{transport}' or 'mcp:admin' permission.",
            )

    imported, skipped, errors = await storage.import_servers(data, user.sub, is_admin=False)

    message = f"Imported {imported} server(s)"
    if skipped > 0:
        message += f", skipped {skipped} existing server(s)"

    return MCPImportResponse(
        message=message,
        imported_count=imported,
        skipped_count=skipped,
        errors=errors,
    )


# GET /api/mcp/export —— 导出当前用户的 MCP 服务器为 JSON 配置；需要 mcp:read 权限
@router.get("/export", response_model=MCPExportResponse)
async def export_servers(
    user: TokenPayload = Depends(require_permissions("mcp:read")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Export user's MCP servers as JSON configuration"""
    config = await storage.export_user_servers(user.sub)
    return MCPExportResponse(servers=config.get("mcpServers", {}))


# ==========================================
# User API Endpoints - Dynamic routes (with path parameters)
# MUST come after static routes to avoid route shadowing
# ==========================================


# GET /api/mcp/{name} —— 获取指定 MCP 服务器详情
# 需要 mcp:read 权限；查找顺序：内置 -> 用户自建 -> 系统服务器
# 系统服务器受基于角色的访问控制；敏感字段(url/headers/command/env_keys)仅创建者可见
@router.get("/{name}", response_model=MCPServerResponse)
async def get_server(
    name: str,
    user: TokenPayload = Depends(require_permissions("mcp:read")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Get a specific MCP server"""
    if _is_internal_server(name):
        if not _is_admin(user):
            raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
        from src.infra.tool.internal_registry import build_internal_server_response

        return build_internal_server_response()

    # Try user server first
    server = await storage.get_user_server(name, user.sub)
    if server:
        return MCPServerResponse(
            name=server.name,
            transport=server.transport,
            enabled=server.enabled,
            url=server.url,
            headers=server.headers,
            command=server.command,
            env_keys=server.env_keys,
            is_system=False,
            can_edit=True,
            created_at=server.created_at,
            updated_at=server.updated_at,
        )

    # Try system server
    system_server = await storage.get_system_server(name)
    if system_server:
        # Role-based access control: check if user can see this system server
        # 基于角色的访问控制：系统服务器若限定了 allowed_roles，非管理员且角色不匹配则视为不可见（404）
        if system_server.allowed_roles and not _is_admin(user):
            if not set(user.roles).intersection(system_server.allowed_roles):
                raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

        # Only the creator can see sensitive fields (url, headers, command, env_keys)
        # 仅创建者可见敏感字段（连接地址/请求头/启动命令/环境变量键名）；其他用户返回 None 脱敏
        is_creator = (system_server.created_by or system_server.updated_by) == user.sub
        return MCPServerResponse(
            name=system_server.name,
            transport=system_server.transport,
            enabled=system_server.enabled,
            url=system_server.url if is_creator else None,
            headers=system_server.headers if is_creator else None,
            command=system_server.command if is_creator else None,
            env_keys=system_server.env_keys if is_creator else None,
            is_system=True,
            can_edit=False,  # System servers are managed through admin routes
            allowed_roles=system_server.allowed_roles,
            role_quotas=system_server.role_quotas,
            created_at=system_server.created_at,
            updated_at=system_server.updated_at,
        )

    raise HTTPException(status_code=404, detail=f"Server '{name}' not found")


# PUT /api/mcp/{name} —— 更新用户自建的 MCP 服务器
# 需要 mcp:read 权限；若变更 transport 需具备新类型的写权限；仅能修改自己拥有的服务器
@router.put("/{name}", response_model=MCPServerResponse)
async def update_server(
    name: str,
    data: MCPServerUpdate,
    user: TokenPayload = Depends(require_permissions("mcp:read")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Update a user-owned MCP server"""
    # If changing transport, check permission for the new transport type
    if data.transport is not None and not _has_permission_for_transport(user, data.transport.value):
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied. Requires 'mcp:write_{data.transport.value}' or 'mcp:admin' permission.",
        )

    server = await storage.update_user_server(name, data, user.sub)
    if not server:
        raise HTTPException(
            status_code=404, detail=f"Server '{name}' not found or not owned by user"
        )

    return MCPServerResponse(
        name=server.name,
        transport=server.transport,
        enabled=server.enabled,
        url=server.url,
        headers=server.headers,
        command=server.command,
        env_keys=server.env_keys,
        is_system=False,
        can_edit=True,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


# DELETE /api/mcp/{name} —— 删除用户自建的 MCP 服务器
# 需要 mcp:read 权限；仅能删除自己拥有的服务器，否则返回 404
@router.delete("/{name}")
async def delete_server(
    name: str,
    user: TokenPayload = Depends(require_permissions("mcp:read")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Delete a user-owned MCP server"""
    deleted = await storage.delete_user_server(name, user.sub)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"Server '{name}' not found or not owned by user"
        )

    return {"message": f"Server '{name}' deleted successfully"}


# PATCH /api/mcp/{name}/toggle —— 切换服务器启用状态
# 需要 mcp:read 权限；用户自建服务器直接切换，系统服务器切换的是"当前用户的启用偏好"
@router.patch("/{name}/toggle", response_model=MCPServerToggleResponse)
async def toggle_server(
    name: str,
    user: TokenPayload = Depends(require_permissions("mcp:read")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Toggle a server's enabled status (user servers: direct toggle; system servers: toggle user preference)"""
    server = await storage.toggle_server(
        name,
        user.sub,
        user_roles=user.roles,
        is_admin=_is_admin(user),
    )

    if not server:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    status_text = "enabled" if server.enabled else "disabled"
    return MCPServerToggleResponse(
        server=server,
        message=f"Server '{name}' has been {status_text}",
    )


# ==========================================
# Tool Discovery & Tool Toggle Endpoints
# ==========================================


# GET /api/mcp/{name}/tools —— 动态探测某个 MCP 服务器提供的工具列表
# 需要 mcp:read 权限；实时连接服务器列出工具（不走缓存）；内置服务器改走内置注册表
@router.get("/{name}/tools", response_model=MCPToolDiscoveryResponse)
async def discover_server_tools(
    name: str,
    user: TokenPayload = Depends(require_permissions("mcp:read")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """
    Dynamically discover tools available from a specific MCP server.

    Connects to the server and lists its available tools with descriptions and parameters.
    This endpoint does NOT use cache - it always probes the server directly.
    """
    if _is_internal_server(name):
        if not _is_admin(user):
            raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
        from src.infra.tool.internal_registry import get_internal_tool_infos

        internal_tools = await get_internal_tool_infos(
            user_id=user.sub,
            user_roles=user.roles,
            is_admin=True,
        )
        return MCPToolDiscoveryResponse(
            server_name=name,
            tools=internal_tools,
            count=len(internal_tools),
            error=None,
        )
    else:
        tools, error = await storage.discover_server_tools(
            name,
            user.sub,
            user_roles=user.roles,
            is_admin=_is_admin(user),
        )

    return MCPToolDiscoveryResponse(
        server_name=name,
        tools=[MCPToolInfo(**t) for t in tools],
        count=len(tools),
        error=error,
    )


# PATCH /api/mcp/{name}/tools/{tool_name} —— 切换单个工具的启用状态
# 需要 mcp:read 权限；具体级别由请求体 level 决定（详见下方 docstring）：
#   level=system 为服务器级禁用（系统服务器需创建者，对所有人隐藏）；level=user 为个人偏好
@router.patch("/{name}/tools/{tool_name}", response_model=MCPToolToggleResponse)
async def toggle_tool(
    name: str,
    tool_name: str,
    data: MCPToolToggleRequest,
    user: TokenPayload = Depends(require_permissions("mcp:read")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """
    Toggle a specific tool's enabled status.

    Two levels controlled by the `level` field:
    - level=system (default): Server-level disable. System servers require creator.
      Sets system_disabled — tool is invisible to ALL users everywhere.
    - level=user: Per-user preference. Works for any server the user can see.
      Sets user_disabled — tool hidden from chat input but visible in preferences (re-enableable).
    """
    # 内置服务器：仅管理员可操作，直接写入工具策略（对所有用户生效）
    if _is_internal_server(name):
        if not _is_admin(user):
            raise HTTPException(status_code=403, detail="Admin permission required")
        await storage.set_tool_policy(
            server_name=name,
            tool_name=tool_name,
            disabled=not data.enabled,
            updated_by=user.sub,
        )
    # 用户级偏好：任何该用户可见的服务器都适用，仅影响该用户自己（可在偏好里重新启用）
    elif data.level == "user":
        if not await storage.can_access_server(
            name,
            user.sub,
            user_roles=user.roles,
            is_admin=_is_admin(user),
        ):
            raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

        # User-level preference: works for any server
        await storage.set_tool_preference(tool_name, name, user.sub, data.enabled)
    else:
        if not await storage.can_access_server(
            name,
            user.sub,
            user_roles=user.roles,
            is_admin=_is_admin(user),
        ):
            raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

        # System-level: only creators can toggle
        # 系统级禁用：区分用户自建/系统服务器，且仅创建者可操作（对所有用户全局生效）
        user_server = await storage.get_user_server(name, user.sub)
        if user_server:
            try:
                await storage.set_user_server_tool_disabled(
                    name, tool_name, user.sub, not data.enabled
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        else:
            system_server = await storage.get_system_server(name)
            if not system_server:
                raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

            is_creator = (system_server.created_by or system_server.updated_by) == user.sub
            if not is_creator:
                raise HTTPException(
                    status_code=403, detail="Only the creator can toggle tools on this server"
                )

            try:
                await storage.set_system_tool_disabled(name, tool_name, not data.enabled)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

    status_text = "enabled" if data.enabled else "disabled"
    return MCPToolToggleResponse(
        server_name=name,
        tool_name=tool_name,
        enabled=data.enabled,
        message=f"Tool '{tool_name}' from server '{name}' has been {status_text}",
    )


# ==========================================
# Admin API Endpoints - Static routes first
# ==========================================


# GET /api/admin/mcp/ —— 管理员列出所有 MCP 服务器（含全部系统服务器，绕过角色过滤）
# 需要 mcp:admin 权限；同样附加内置服务器
@admin_router.get("/", response_model=MCPServersResponse)
async def admin_list_servers(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    q: str | None = None,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Get all MCP servers (admin view - includes all system servers, bypasses role filter)"""
    storage_limit = skip + limit + 1
    servers = await storage.get_visible_servers(
        user.sub,
        is_admin=True,
        user_roles=user.roles,
        limit=storage_limit,
    )
    from src.infra.tool.internal_registry import build_internal_server_response

    servers.append(build_internal_server_response())
    return _paginate_servers(servers, skip=skip, limit=limit, q=q)


# POST /api/admin/mcp/ —— 创建系统级 MCP 服务器（仅管理员）
# 需要 mcp:admin 权限；名称不能与已有系统服务器重复
@admin_router.post("/", response_model=MCPServerResponse, status_code=201)
async def admin_create_server(
    data: MCPServerCreate,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Create a new system MCP server (admin only)"""
    existing = await storage.get_system_server(data.name)
    if existing:
        raise HTTPException(status_code=400, detail=f"System server '{data.name}' already exists")

    server = await storage.create_system_server(data, user.sub)
    return MCPServerResponse(
        name=server.name,
        transport=server.transport,
        enabled=server.enabled,
        url=server.url,
        headers=server.headers,
        command=server.command,
        env_keys=server.env_keys,
        is_system=True,
        can_edit=True,
        allowed_roles=server.allowed_roles,
        role_quotas=server.role_quotas,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


# GET /api/admin/mcp/export —— 导出所有系统 MCP 服务器为 JSON 配置（仅管理员）
@admin_router.get("/export", response_model=MCPExportResponse)
async def admin_export_servers(
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Export all system MCP servers as JSON configuration (admin only)"""
    config = await storage.export_all_servers()
    return MCPExportResponse(servers=config.get("mcpServers", {}))


# GET /api/admin/mcp/{name}/tools —— 探测系统或内置 MCP 服务器的工具（仅管理员）
@admin_router.get("/{name}/tools", response_model=MCPToolDiscoveryResponse)
async def admin_discover_server_tools(
    name: str,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Discover tools for a system or internal MCP server (admin only)."""
    if _is_internal_server(name):
        from src.infra.tool.internal_registry import get_internal_tool_infos

        internal_tools = await get_internal_tool_infos(
            user_id=user.sub,
            user_roles=user.roles,
            is_admin=True,
        )
        return MCPToolDiscoveryResponse(
            server_name=name,
            tools=internal_tools,
            count=len(internal_tools),
            error=None,
        )

    tools, error = await storage.discover_server_tools(
        name,
        user.sub,
        user_roles=user.roles,
        is_admin=True,
    )
    return MCPToolDiscoveryResponse(
        server_name=name,
        tools=[MCPToolInfo(**t) for t in tools],
        count=len(tools),
        error=error,
    )


# ==========================================
# Admin API Endpoints - Dynamic routes (with path parameters)
# MUST come after static routes to avoid route shadowing
# ==========================================


# GET /api/admin/mcp/{name} —— 获取系统 MCP 服务器详情（仅管理员，可见全部敏感字段）
@admin_router.get("/{name}", response_model=MCPServerResponse)
async def admin_get_server(
    name: str,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Get a system MCP server (admin only)"""
    if _is_internal_server(name):
        from src.infra.tool.internal_registry import build_internal_server_response

        return build_internal_server_response()

    server = await storage.get_system_server(name)
    if not server:
        raise HTTPException(status_code=404, detail=f"System server '{name}' not found")

    return MCPServerResponse(
        name=server.name,
        transport=server.transport,
        enabled=server.enabled,
        url=server.url,
        headers=server.headers,
        command=server.command,
        env_keys=server.env_keys,
        is_system=True,
        can_edit=True,
        allowed_roles=server.allowed_roles,
        role_quotas=server.role_quotas,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


# PUT /api/admin/mcp/{name} —— 更新系统 MCP 服务器（仅管理员）
@admin_router.put("/{name}", response_model=MCPServerResponse)
async def admin_update_server(
    name: str,
    data: MCPServerUpdate,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Update a system MCP server (admin only)"""
    server = await storage.update_system_server(name, data, user.sub)
    if not server:
        raise HTTPException(status_code=404, detail=f"System server '{name}' not found")

    return MCPServerResponse(
        name=server.name,
        transport=server.transport,
        enabled=server.enabled,
        url=server.url,
        headers=server.headers,
        command=server.command,
        env_keys=server.env_keys,
        is_system=True,
        can_edit=True,
        allowed_roles=server.allowed_roles,
        role_quotas=server.role_quotas,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


# DELETE /api/admin/mcp/{name} —— 删除系统 MCP 服务器（仅管理员）
@admin_router.delete("/{name}")
async def admin_delete_server(
    name: str,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Delete a system MCP server (admin only)"""
    deleted = await storage.delete_system_server(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"System server '{name}' not found")

    return {"message": f"System server '{name}' deleted successfully"}


# PATCH /api/admin/mcp/{name}/toggle —— 切换系统服务器的启用状态（仅管理员）
@admin_router.patch("/{name}/toggle", response_model=MCPServerToggleResponse)
async def admin_toggle_server(
    name: str,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Toggle a system server's enabled status (admin only)"""
    server = await storage.toggle_system_server(name)

    if not server:
        raise HTTPException(status_code=404, detail=f"System server '{name}' not found")

    status_text = "enabled" if server.enabled else "disabled"
    return MCPServerToggleResponse(
        server=server,
        message=f"System server '{name}' has been {status_text}",
    )


# PATCH /api/admin/mcp/{name}/tools/{tool_name} —— 全局切换工具的系统级禁用状态（仅管理员）
# disabled 后对所有用户生效，且个人无法自行重新启用
@admin_router.patch("/{name}/tools/{tool_name}", response_model=MCPToolToggleResponse)
async def admin_toggle_tool(
    name: str,
    tool_name: str,
    data: MCPToolToggleRequest,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """
    Toggle a tool's system-level disabled status (admin only).

    This affects all users globally. When disabled=True, the tool is blocked
    for everyone and cannot be re-enabled by individual users.
    """
    if _is_internal_server(name):
        await storage.set_tool_policy(
            server_name=name,
            tool_name=tool_name,
            disabled=not data.enabled,
            updated_by=user.sub,
        )
    else:
        try:
            await storage.set_system_tool_disabled(name, tool_name, not data.enabled)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    status_text = "enabled" if data.enabled else "disabled"
    return MCPToolToggleResponse(
        server_name=name,
        tool_name=tool_name,
        enabled=data.enabled,
        message=f"Tool '{tool_name}' from server '{name}' has been {status_text} globally",
    )


# PUT /api/admin/mcp/{name}/tools/{tool_name}/policy —— 更新单个 MCP 工具的角色访问与配额策略（仅管理员）
@admin_router.put("/{name}/tools/{tool_name}/policy", response_model=MCPToolPolicy)
async def admin_update_tool_policy(
    name: str,
    tool_name: str,
    data: MCPToolPolicyUpdate,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """Update role access and quotas for one MCP tool (admin only)."""
    if not _is_internal_server(name):
        server = await storage.get_system_server(name)
        if not server:
            raise HTTPException(status_code=404, detail=f"System server '{name}' not found")

    return await storage.set_tool_policy(
        server_name=name,
        tool_name=tool_name,
        disabled=data.disabled,
        inline_exposure=data.inline_exposure,
        allowed_roles=data.allowed_roles,
        role_quotas=data.role_quotas,
        updated_by=user.sub,
    )


# ==========================================
# Server Type Conversion (Admin only)
# ==========================================


# POST /api/admin/mcp/{name}/promote —— 将用户自建服务器提升为系统服务器（仅管理员）
# 请求体需提供 target_user_id 指明要提升哪个用户的服务器
@admin_router.post("/{name}/promote", response_model=MCPServerMoveResponse)
async def promote_server(
    name: str,
    data: MCPServerMoveRequest,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """
    Promote a user server to system server (admin only).

    Requires the owner's user_id in request body to identify which user's server to promote.
    """
    if not data.target_user_id:
        raise HTTPException(
            status_code=400,
            detail="target_user_id is required to identify the user server",
        )

    server = await storage.promote_to_system_server(name, data.target_user_id, user.sub)

    if not server:
        raise HTTPException(
            status_code=404,
            detail=f"User server '{name}' not found or system server with same name exists",
        )

    return MCPServerMoveResponse(
        server=MCPServerResponse(
            name=server.name,
            transport=server.transport,
            enabled=server.enabled,
            url=server.url,
            headers=server.headers,
            command=server.command,
            env_keys=server.env_keys,
            is_system=True,
            can_edit=True,
            allowed_roles=server.allowed_roles,
            role_quotas=server.role_quotas,
            created_at=server.created_at,
            updated_at=server.updated_at,
        ),
        message=f"Server '{name}' has been promoted to system server",
        from_type="user",
        to_type="system",
    )


# POST /api/admin/mcp/{name}/demote —— 将系统服务器降级为用户服务器（仅管理员）
# 请求体需提供 target_user_id 指明降级后归属的用户
@admin_router.post("/{name}/demote", response_model=MCPServerMoveResponse)
async def demote_server(
    name: str,
    data: MCPServerMoveRequest,
    user: TokenPayload = Depends(require_permissions("mcp:admin")),
    storage: MCPStorage = Depends(get_mcp_storage),
):
    """
    Demote a system server to user server (admin only).

    Requires target_user_id in request body to specify who will own the server.
    """
    if not data.target_user_id:
        raise HTTPException(
            status_code=400,
            detail="target_user_id is required to specify the new owner",
        )

    server = await storage.demote_to_user_server(name, data.target_user_id, user.sub)

    if not server:
        raise HTTPException(
            status_code=404,
            detail=f"System server '{name}' not found or user already has server with same name",
        )

    return MCPServerMoveResponse(
        server=MCPServerResponse(
            name=server.name,
            transport=server.transport,
            enabled=server.enabled,
            url=server.url,
            headers=server.headers,
            command=server.command,
            env_keys=server.env_keys,
            is_system=False,
            can_edit=True,
            created_at=server.created_at,
            updated_at=server.updated_at,
        ),
        message=f"System server '{name}' has been demoted to user server",
        from_type="system",
        to_type="user",
    )

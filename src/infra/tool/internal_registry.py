"""Registry for LambChat internal tools exposed through the MCP UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, get_args, get_origin

from langchain_core.tools import BaseTool

from src.infra.mcp.storage import MCPStorage
from src.infra.role.storage import RoleStorage
from src.infra.tool.audio_transcribe_tool import get_audio_transcribe_tool
from src.infra.tool.env_var_tool import get_env_var_tools
from src.infra.tool.image_analysis_tool import get_image_analysis_tool
from src.infra.tool.image_generation_tool import (
    get_image_generation_tool,
    get_reference_image_generation_tool,
)
from src.infra.tool.mcp_client import MCPToolWithRetry
from src.infra.tool.persona_preset_tool import get_persona_preset_tools
from src.infra.tool.scheduled_task import get_scheduled_task_tools
from src.infra.tool.team_tool import get_team_tools
from src.kernel.config import settings
from src.kernel.schemas.mcp import (
    MCPServerResponse,
    MCPToolInfo,
    MCPToolPolicy,
    MCPTransport,
)
from src.kernel.types import Permission

# 内置（虚拟）MCP 服务器的名称：LambChat 自带的一批工具会以这个"服务器"名义
# 统一暴露到 /mcp 管理界面，与真实的外部 MCP 服务器并列展示
INTERNAL_MCP_SERVER_NAME = "lambchat_internal"

# 定时任务相关工具所需的业务权限映射：工具名 -> 所需 Permission
# 用于在角色权限之外，再叠加一层"按业务动作粒度"的访问控制
_SCHEDULED_TASK_TOOL_PERMISSIONS = {
    "scheduled_task_create": Permission.SCHEDULED_TASK_WRITE.value,
    "scheduled_task_list": Permission.SCHEDULED_TASK_READ.value,
    "scheduled_task_update": Permission.SCHEDULED_TASK_WRITE.value,
    "scheduled_task_delete": Permission.SCHEDULED_TASK_DELETE.value,
}


def build_internal_tools() -> list[BaseTool]:
    """Build the internal tool set that LambChat exposes to agents."""
    # 延迟到函数内导入 logger，避免包加载期的循环依赖
    from src.infra.logging import get_logger

    logger = get_logger(__name__)
    tools: list[BaseTool] = []

    # 以下各工具是否装配，全部由全局配置开关（settings.ENABLE_*）控制，
    # 便于按部署环境裁剪能力集
    if settings.ENABLE_IMAGE_ANALYSIS:
        tools.append(get_image_analysis_tool())

    if settings.ENABLE_IMAGE_GENERATION:
        # 图像生成同时提供"文生图"与"参考图生图"两个工具
        tools.append(get_image_generation_tool())
        tools.append(get_reference_image_generation_tool())

    if settings.ENABLE_AUDIO_TRANSCRIPTION:
        tools.append(get_audio_transcribe_tool())

    if settings.ENABLE_SCHEDULED_TASK:
        # 定时任务工具加载可能失败（依赖调度器等），这里单独 try 包裹，
        # 失败只记录日志而不影响其余内置工具的装配
        try:
            scheduled_tools = get_scheduled_task_tools()
            tools.extend(scheduled_tools)
            logger.info(
                "[InternalRegistry] ENABLE_SCHEDULED_TASK=True, added %d scheduled task tools: %s",
                len(scheduled_tools),
                [t.name for t in scheduled_tools],
            )
        except Exception as e:
            logger.error(
                "[InternalRegistry] Failed to load scheduled task tools: %s", e, exc_info=True
            )
    else:
        logger.info("[InternalRegistry] ENABLE_SCHEDULED_TASK=False, skipping scheduled task tools")

    # 环境变量、人设预设、团队协作工具默认始终装配（无独立开关）
    tools.extend(get_env_var_tools())
    tools.extend(get_persona_preset_tools())
    tools.extend(get_team_tools())

    logger.info(
        "[InternalRegistry] Total %d internal tools built: %s",
        len(tools),
        [t.name for t in tools],
    )
    return tools


def build_internal_server_response() -> MCPServerResponse:
    """Build the virtual server row for the /mcp UI."""
    # 构造一条"虚拟服务器"记录：它并非真实进程，而是为了让内置工具在
    # MCP 管理界面上有一个可配置（策略/配额）的宿主条目
    return MCPServerResponse(
        name=INTERNAL_MCP_SERVER_NAME,
        transport=MCPTransport.SANDBOX,
        enabled=True,
        url=None,
        headers=None,
        command=None,
        env_keys=None,
        # is_system / is_internal 标记它是系统内置、不可被普通外部服务器逻辑处理
        is_system=True,
        is_internal=True,
        can_edit=True,
        allowed_roles=[],
        role_quotas={},
        created_at=None,
        updated_at=None,
    )


def _policy_for_tool(
    policies: Mapping[str, MCPToolPolicy],
    tool_name: str,
) -> MCPToolPolicy | None:
    # 从策略表中取出某工具的策略；不存在则返回 None（表示未显式配置策略）
    policy = policies.get(tool_name)
    return policy if policy is not None else None


def _is_tool_allowed(
    *,
    policy: MCPToolPolicy | None,
    user_roles: list[str] | None,
    is_admin: bool,
) -> bool:
    # 基于"角色可见性"判断工具是否对当前用户可用
    # 管理员无条件放行
    if is_admin:
        return True
    # 未配置策略：默认允许
    if policy is None:
        return True
    # 策略显式禁用：拒绝
    if policy.disabled:
        return False
    # 策略未限定角色：对所有人开放
    if not policy.allowed_roles:
        return True
    # 否则要求用户角色与策略允许角色存在交集
    return bool(set(user_roles or []).intersection(policy.allowed_roles))


async def _resolve_permissions_for_roles(user_roles: list[str] | None) -> set[str]:
    # 将用户的角色名列表解析为其拥有的全部业务权限（字符串）集合
    if not user_roles:
        return set()

    storage = RoleStorage()
    permissions: set[str] = set()
    for role_name in user_roles:
        # 单个角色查询失败/不存在时跳过，保证整体权限解析尽力而为
        try:
            role = await storage.get_by_name(role_name)
        except Exception:
            continue
        if not role:
            continue
        for permission in role.permissions:
            # 权限可能是枚举也可能是字符串，统一归一化为字符串
            permissions.add(permission if isinstance(permission, str) else permission.value)
    return permissions


def _is_tool_allowed_by_business_permission(
    tool_name: str,
    *,
    user_permissions: set[str],
) -> bool:
    # 基于"业务权限"判断工具是否可用（在角色可见性之外的第二道校验）
    required_permission = _SCHEDULED_TASK_TOOL_PERMISSIONS.get(tool_name)
    # 不在权限映射表内的工具默认放行
    if required_permission is None:
        return True
    return required_permission in user_permissions


def _schema_type_from_annotation(annotation: Any) -> str:
    # 将 Python 类型注解粗略映射为 JSON Schema 类型字符串，供前端展示参数
    origin = get_origin(annotation)
    # 过滤掉 Optional 中的 NoneType，只保留真实类型参数
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    if origin is not None and args:
        # 容器类型（list/tuple/set）统一映射为 array
        if origin in (list, tuple, set):
            return "array"
        # 其余泛型（如 Optional[X]）递归取第一个实际类型
        return _schema_type_from_annotation(args[0])
    if annotation in (list, tuple, set):
        return "array"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"
    if annotation is dict:
        return "object"
    # 兜底：无法识别的类型一律当作 string
    return "string"


def _extract_tool_parameters(tool: BaseTool) -> list[dict[str, Any]]:
    # 从 LangChain 工具的参数 schema 中提取可展示的参数列表
    args_schema = getattr(tool, "args_schema", None)
    if not args_schema:
        return []

    # 优先走 JSON Schema 路径：args_schema 可能本身就是 dict，或可 .schema() 导出
    try:
        schema = args_schema if isinstance(args_schema, dict) else args_schema.schema()
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        parameters = []
        for param_name, param_info in properties.items():
            # 跳过框架注入的 runtime 参数与非字典项（对用户不可见）
            if param_name == "runtime" or not isinstance(param_info, dict):
                continue
            parameters.append(
                {
                    "name": param_name,
                    "type": param_info.get("type", "string"),
                    "description": param_info.get("description", ""),
                    "required": param_name in required,
                    "default": param_info.get("default"),
                }
            )
        return parameters
    except Exception:
        # JSON Schema 路径失败则回退到 Pydantic model_fields 反射
        pass

    # 回退路径：直接读取 Pydantic 模型字段元信息
    model_fields = getattr(args_schema, "model_fields", {})
    parameters = []
    for param_name, field in model_fields.items():
        # 同样跳过框架注入的 runtime 参数
        if param_name == "runtime":
            continue
        # 必填字段无默认值；可选字段取其默认值
        default = None if field.is_required() else field.default
        parameters.append(
            {
                "name": param_name,
                "type": _schema_type_from_annotation(field.annotation),
                "description": field.description or "",
                "required": field.is_required(),
                "default": default,
            }
        )
    return parameters


async def get_internal_tool_policies() -> dict[str, MCPToolPolicy]:
    """Load explicit tool policies for the internal virtual server."""
    # 从存储加载内置虚拟服务器的每工具策略；失败时返回空表（等价于无策略）
    try:
        return await MCPStorage().list_tool_policies(INTERNAL_MCP_SERVER_NAME)
    except Exception:
        return {}


async def get_internal_tools_for_user(
    *,
    user_id: str | None,
    user_roles: list[str] | None,
    is_admin: bool,
) -> list[BaseTool]:
    """Return internal tools filtered and wrapped by per-tool policy."""
    # 面向具体用户返回可用的内置工具：先按策略/权限过滤，再包裹重试与配额逻辑
    tools = build_internal_tools()
    if not tools:
        return []

    policies = await get_internal_tool_policies()
    user_permissions = await _resolve_permissions_for_roles(user_roles)
    wrapped: list[BaseTool] = []
    for tool in tools:
        policy = _policy_for_tool(policies, tool.name)
        # 第一道：角色可见性过滤
        if not _is_tool_allowed(policy=policy, user_roles=user_roles, is_admin=is_admin):
            continue
        # 第二道：业务权限过滤（如定时任务的增删改查权限）
        if not _is_tool_allowed_by_business_permission(
            tool.name,
            user_permissions=user_permissions,
        ):
            continue

        # 通过过滤的工具统一用 MCPToolWithRetry 包裹，注入用户上下文、
        # 角色配额与配额计量所用的工具名，从而获得重试与配额限制能力
        wrapped.append(
            MCPToolWithRetry(
                tool,
                user_id=user_id,
                server_name=INTERNAL_MCP_SERVER_NAME,
                user_roles=user_roles,
                is_admin=is_admin,
                role_quotas=(policy.role_quotas if policy else None),
                quota_tool_name=tool.name,
            )
        )
    return wrapped


async def get_internal_tool_infos(
    *,
    user_id: str | None,
    user_roles: list[str] | None,
    is_admin: bool,
) -> list[MCPToolInfo]:
    """Return tool metadata for the virtual internal server."""
    # 面向 /mcp 管理界面返回内置工具的元数据（含策略/配额展示信息）
    # user_id 在本函数中不需要，显式 del 表明这是有意忽略的入参
    del user_id
    policies = await get_internal_tool_policies()
    user_permissions = await _resolve_permissions_for_roles(user_roles)
    infos: list[MCPToolInfo] = []
    for tool in build_internal_tools():
        policy = _policy_for_tool(policies, tool.name)
        # 与 get_internal_tools_for_user 保持一致的两道过滤，确保展示与实际可用一致
        if not _is_tool_allowed(policy=policy, user_roles=user_roles, is_admin=is_admin):
            continue
        if not _is_tool_allowed_by_business_permission(
            tool.name,
            user_permissions=user_permissions,
        ):
            continue

        parameters = _extract_tool_parameters(tool)

        # 组装展示用元数据：把策略中的禁用状态、允许角色、角色配额等一并回填
        infos.append(
            MCPToolInfo(
                name=tool.name,
                description=getattr(tool, "description", ""),
                parameters=parameters,
                system_disabled=bool(policy.disabled) if policy else False,
                user_disabled=False,
                allowed_roles=list(policy.allowed_roles) if policy else [],
                role_quotas=dict(policy.role_quotas) if policy else {},
                policy_configured=policy is not None,
                inline_exposure=bool(policy.inline_exposure) if policy else False,
            )
        )
    return infos

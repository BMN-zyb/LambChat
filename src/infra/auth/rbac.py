"""
基于角色的访问控制 (RBAC)

提供权限检查和角色管理功能。
"""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Callable, List, Set

from src.kernel.exceptions import AuthorizationError
from src.kernel.types import Permission

# 仅用于类型标注的导入：运行时不加载，避免循环依赖
if TYPE_CHECKING:
    from src.kernel.schemas.role import Role


def check_permission(
    user_permissions: List[str],
    required_permission: str,
) -> bool:
    """
    检查用户是否拥有指定权限

    Args:
        user_permissions: 用户权限列表
        required_permission: 需要的权限

    Returns:
        是否拥有权限
    """
    # 权限模型为“显式授予”：所需权限必须出现在用户权限列表中才算通过
    return required_permission in user_permissions


def get_user_permissions(
    roles: List["Role"],
) -> Set[str]:
    """
    获取用户的所有权限（合并所有角色的权限）

    Args:
        roles: 用户的角色列表

    Returns:
        权限集合
    """
    # 用户最终权限 = 其所有角色权限的并集；用 set 天然去重
    permissions: Set[str] = set()
    for role in roles:
        for perm in role.permissions:
            # perm 是 Permission 枚举，取 .value 存为字符串形式
            permissions.add(perm.value)
    return permissions


def require_permissions(
    *required_permissions: str,
) -> Callable:
    """
    权限检查装饰器

    用法:
        @require_permissions("chat:read", "chat:write")
        async def chat_endpoint(...):
            ...

    Args:
        required_permissions: 需要的权限列表

    Returns:
        装饰器函数
    """

    # 两层闭包：外层接收所需权限，decorator 包裹目标函数，wrapper 执行实际校验
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # 从 kwargs 中获取当前用户
            # 约定：被装饰的接口需通过依赖注入把 current_user 放入关键字参数
            current_user = kwargs.get("current_user")
            if not current_user:
                raise AuthorizationError("未认证的用户")

            # 取出当前用户已具备的权限集合
            user_permissions = set(current_user.get("permissions", []))

            # 要求“全部满足”：任一所需权限缺失即拒绝
            for perm in required_permissions:
                if perm not in user_permissions:
                    raise AuthorizationError(f"缺少权限: {perm}")

            # 校验通过后放行，执行原始异步处理函数
            return await func(*args, **kwargs)

        return wrapper

    return decorator


class RBACManager:
    """
    RBAC 管理器

    提供角色和权限的管理功能。
    """

    def __init__(self):
        # 角色缓存占位（预留用于减少重复查询）
        self._role_cache: dict = {}

    def validate_permission(self, permission: str) -> bool:
        """
        验证权限是否有效

        Args:
            permission: 权限字符串

        Returns:
            是否有效
        """
        # 借助 Permission 枚举的构造校验：非法字符串会抛 ValueError
        try:
            Permission(permission)
            return True
        except ValueError:
            return False

    def get_default_roles(self) -> List[dict]:
        """
        获取默认角色配置

        Returns:
            默认角色列表
        """
        # 系统初始化时写入的三种内置角色：admin / user / guest
        # 权限粒度形如 "资源:动作"（如 chat:read），由 Permission 枚举统一定义
        return [
            {
                "name": "admin",
                "description": "系统管理员 - 拥有所有权限",
                # 管理员直接授予枚举中的全部权限
                "permissions": [p.value for p in Permission],
                "limits": None,  # 无限制
                # is_system=True 标记为系统角色，通常不允许删除/改名
                "is_system": True,
            },
            {
                "name": "user",
                "description": "普通用户 - 可使用聊天、会话、技能、MCP、反馈功能",
                # 普通用户：按业务模块（聊天/会话/技能/MCP/反馈/渠道/团队/市场等）
                # 授予读写删的常规权限，但不含系统级管理能力
                "permissions": [
                    # Chat
                    Permission.CHAT_READ.value,
                    Permission.CHAT_WRITE.value,
                    # Session
                    Permission.SESSION_READ.value,
                    Permission.SESSION_WRITE.value,
                    Permission.SESSION_DELETE.value,
                    Permission.SESSION_SHARE.value,
                    # Skill
                    Permission.SKILL_READ.value,
                    Permission.SKILL_WRITE.value,
                    Permission.SKILL_DELETE.value,
                    # MCP
                    Permission.MCP_READ.value,
                    Permission.MCP_WRITE_SSE.value,
                    Permission.MCP_WRITE_HTTP.value,
                    Permission.MCP_DELETE.value,
                    # Feedback
                    Permission.FEEDBACK_WRITE.value,
                    Permission.FEEDBACK_READ.value,
                    # Channel
                    Permission.CHANNEL_READ.value,
                    Permission.CHANNEL_WRITE.value,
                    Permission.CHANNEL_DELETE.value,
                    # Agent
                    Permission.AGENT_READ.value,
                    # Team
                    Permission.TEAM_READ.value,
                    Permission.TEAM_WRITE.value,
                    Permission.TEAM_DELETE.value,
                    # Marketplace
                    Permission.MARKETPLACE_READ.value,
                    Permission.MARKETPLACE_PUBLISH.value,
                    # Persona Preset
                    Permission.PERSONA_PRESET_READ.value,
                    Permission.PERSONA_PRESET_WRITE.value,
                    # Scheduled Task
                    Permission.SCHEDULED_TASK_READ.value,
                    Permission.SCHEDULED_TASK_WRITE.value,
                    Permission.SCHEDULED_TASK_DELETE.value,
                    # Usage
                    Permission.USAGE_READ.value,
                ],
                # limits 为角色级配额限制，此处限制普通用户最多创建 10 个渠道
                "limits": {"max_channels": 10},
                "is_system": False,
            },
            {
                "name": "guest",
                "description": "访客 - 只读访问",
                # 访客：仅授予各模块的只读权限，用于未登录/受限体验场景
                "permissions": [
                    Permission.CHAT_READ.value,
                    Permission.SESSION_READ.value,
                    Permission.SKILL_READ.value,
                    Permission.MCP_READ.value,
                    # Channel - read only
                    Permission.CHANNEL_READ.value,
                    # Agent
                    Permission.AGENT_READ.value,
                    # Team
                    Permission.TEAM_READ.value,
                    # Marketplace
                    Permission.MARKETPLACE_READ.value,
                    # Persona Preset
                    Permission.PERSONA_PRESET_READ.value,
                ],
                "limits": {"max_channels": 0},  # 不能创建渠道
                "is_system": False,
            },
        ]

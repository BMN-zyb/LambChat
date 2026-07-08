"""
认证授权模块

提供 JWT 认证、密码处理和 RBAC 权限控制。
"""

# 从 jwt 子模块导入令牌相关能力：签发访问/刷新令牌、解码与校验
from src.infra.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_token,
)
# 从 password 子模块导入密码哈希与校验（基于 bcrypt）
from src.infra.auth.password import (
    hash_password,
    verify_password,
)
# 从 rbac 子模块导入权限检查、权限聚合与权限装饰器
from src.infra.auth.rbac import (
    check_permission,
    get_user_permissions,
    require_permissions,
)

# __all__ 显式声明本包对外暴露的公共 API，便于 from ... import * 与文档生成
__all__ = [
    # JWT
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "verify_token",
    # Password
    "hash_password",
    "verify_password",
    # RBAC
    "check_permission",
    "get_user_permissions",
    "require_permissions",
]

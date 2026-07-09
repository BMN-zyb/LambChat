"""
角色管理器

提供角色管理的业务逻辑。
"""

# ---------------------------------------------------------------------------
# 模块说明：角色业务管理层（Manager）+ 进程级单例
#
# 本模块是角色（Role）功能的业务门面：对上给路由层提供角色的增删改查、
# 计数与默认角色初始化等方法，对下把全部读写委托给 RoleStorage。
# 真正的持久化与「Redis 缓存」逻辑都在 RoleStorage 内部，Manager 只做转发，
# 因此这里几乎没有分支逻辑。
# 模块末尾的 get_role_manager() 以懒加载方式返回全进程共享的单例，
# 避免每次请求都重建 Manager / Storage 实例。
# ---------------------------------------------------------------------------

from typing import Optional

from src.infra.role.storage import RoleStorage
from src.kernel.schemas.role import Role, RoleCreate, RoleUpdate


# 角色业务管理器：无状态门面，构造时持有一个 RoleStorage（内部带 Redis 缓存），
# 所有方法均为异步，逐一转发到存储层
class RoleManager:
    """
    角色管理器

    提供角色 CRUD 功能。
    """

    def __init__(self):
        # 业务门面：所有操作委托给带缓存的 RoleStorage
        self.storage = RoleStorage()

    async def create_role(self, role_data: RoleCreate) -> Role:
        """
        创建角色

        Args:
            role_data: 角色创建数据

        Returns:
            创建的角色
        """
        return await self.storage.create(role_data)

    async def get_role(self, role_id: str) -> Optional[Role]:
        """
        获取角色

        Args:
            role_id: 角色 ID

        Returns:
            角色或 None
        """
        return await self.storage.get_by_id(role_id)

    async def get_role_by_name(self, name: str) -> Optional[Role]:
        """
        通过名称获取角色

        Args:
            name: 角色名称

        Returns:
            角色或 None
        """
        return await self.storage.get_by_name(name)

    async def update_role(self, role_id: str, role_data: RoleUpdate) -> Optional[Role]:
        """
        更新角色

        Args:
            role_id: 角色 ID
            role_data: 更新数据

        Returns:
            更新后的角色
        """
        return await self.storage.update(role_id, role_data)

    async def delete_role(self, role_id: str) -> bool:
        """
        删除角色

        Args:
            role_id: 角色 ID

        Returns:
            是否删除成功
        """
        return await self.storage.delete(role_id)

    async def list_roles(
        self,
        skip: int = 0,
        limit: int = 100,
        q: str | None = None,
    ) -> list[Role]:
        """
        列出角色

        Args:
            skip: 跳过数量
            limit: 返回数量

        Returns:
            角色列表
        """
        return await self.storage.list_roles(skip, limit, q)

    async def count_roles(self, q: str | None = None) -> int:
        """Count roles matching an optional search query."""
        return await self.storage.count_roles(q)

    async def init_default_roles(self) -> None:
        """
        初始化默认角色
        """
        await self.storage.init_default_roles()


# 单例实例
# 全进程共享一个 RoleManager，避免重复创建存储层实例
_role_manager: Optional[RoleManager] = None


def get_role_manager() -> RoleManager:
    """
    获取角色管理器单例

    Returns:
        角色管理器实例
    """
    # 懒加载单例
    global _role_manager
    if _role_manager is None:
        _role_manager = RoleManager()
    return _role_manager

"""Project storage layer for session organization."""
# 中文说明：本模块实现"项目/文件夹"（Project）功能——用于把用户的多个会话
# （session）归类到不同的项目下，类似文件夹的概念（模块目录名为 folder，
# 领域模型/数据库集合命名为 Project，二者是同一个概念的两种叫法）。
# 每个用户还会有一个特殊的 "favorites"（收藏）类型项目，用于置顶常用会话，
# ensure_favorites_project 保证这个特殊项目总是存在。

from typing import Optional

from bson import ObjectId

from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.project import Project, ProjectCreate, ProjectUpdate

# 单个用户最多返回/展示的项目数量上限
PROJECT_LIST_LIMIT = 100
# 未指定图标时使用的默认项目图标（emoji）
DEFAULT_PROJECT_ICON = "💬"


class ProjectStorage:
    """
    Project storage class using MongoDB.

    Manages projects for organizing user sessions, including the special "favorites" project.
    """

    PROJECT_COLLECTION = "projects"

    def __init__(self):
        self._collection = None

    @property
    def collection(self):
        """Lazy-load MongoDB collection."""
        # 中文：懒加载 MongoDB 集合引用，避免在模块导入阶段就建立数据库连接
        if self._collection is None:
            from src.infra.storage.mongodb import get_mongo_client

            client = get_mongo_client()
            db = client[settings.MONGODB_DB]
            self._collection = db[self.PROJECT_COLLECTION]
        return self._collection

    async def create(self, project_data: ProjectCreate, user_id: str) -> Project:
        """Create a new project."""
        now = utc_now()

        project_dict = {
            "name": project_data.name,
            "type": project_data.type,
            "icon": project_data.icon or "💬",
            "sort_order": project_data.sort_order,
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
        }

        result = await self.collection.insert_one(project_dict)
        # MongoDB 生成的 _id 转成字符串形式的 id，与 Pydantic 模型 Project.id 对齐
        project_dict["id"] = str(result.inserted_id)

        return Project(**project_dict)

    async def get_by_id(self, project_id: str, user_id: str) -> Optional[Project]:
        """Get a project by ID for a specific user."""
        try:
            # 查询条件同时带上 user_id，防止跨用户读取到别人的项目
            project_dict = await self.collection.find_one(
                {"_id": ObjectId(project_id), "user_id": user_id}
            )
        except Exception:
            # project_id 格式非法（无法转换为 ObjectId）时也归为"未找到"
            return None

        if not project_dict:
            return None

        project_dict["id"] = str(project_dict.pop("_id"))
        return Project(**project_dict)

    async def get_by_type(self, user_id: str, project_type: str) -> Optional[Project]:
        """Get a project by type for a specific user (e.g., 'favorites')."""
        project_dict = await self.collection.find_one({"user_id": user_id, "type": project_type})

        if not project_dict:
            return None

        project_dict["id"] = str(project_dict.pop("_id"))
        return Project(**project_dict)

    async def list_projects(self, user_id: str) -> list[Project]:
        """List all projects for a user, sorted by sort_order."""
        # 按 sort_order 升序排列（值越小越靠前），并限制返回数量
        cursor = (
            self.collection.find({"user_id": user_id})
            .sort("sort_order", 1)
            .limit(PROJECT_LIST_LIMIT)
        )
        projects = []

        for project_dict in await cursor.to_list(length=PROJECT_LIST_LIMIT):
            project_dict["id"] = str(project_dict.pop("_id"))
            projects.append(Project(**project_dict))

        return projects

    async def update(
        self, project_id: str, user_id: str, project_data: ProjectUpdate
    ) -> Optional[Project]:
        """Update a project."""
        update_dict: dict = {"updated_at": utc_now()}

        # 只更新显式传入（非 None）的字段，实现部分更新语义
        if project_data.name is not None:
            update_dict["name"] = project_data.name

        if project_data.icon is not None:
            update_dict["icon"] = project_data.icon

        if project_data.sort_order is not None:
            update_dict["sort_order"] = project_data.sort_order

        try:
            result = await self.collection.find_one_and_update(
                {"_id": ObjectId(project_id), "user_id": user_id},
                {"$set": update_dict},
                return_document=True,
            )
        except Exception:
            return None

        if not result:
            return None

        result["id"] = str(result.pop("_id"))
        return Project(**result)

    async def delete(self, project_id: str, user_id: str) -> bool:
        """Delete a project.

        Note: This does not delete the sessions in the project, only the project itself.
        """
        # 中文：只删除项目这个"分类容器"本身，项目下的会话不会被级联删除，
        # 会话会变回"未分类"状态（具体行为取决于会话查询时如何处理 project_id 悬空引用）
        try:
            result = await self.collection.delete_one(
                {"_id": ObjectId(project_id), "user_id": user_id}
            )
            return result.deleted_count > 0
        except Exception:
            return False

    async def ensure_favorites_project(self, user_id: str) -> Project:
        """Ensure the favorites project exists for a user.

        Creates the favorites project if it doesn't exist.
        Returns the favorites project.
        """
        # Check if favorites project already exists
        existing = await self.get_by_type(user_id, "favorites")
        if existing:
            return existing

        # Create the favorites project
        # 中文：sort_order 固定为 0，确保收藏项目始终排在列表最前面
        now = utc_now()
        project_dict = {
            "name": "Favorites",
            "type": "favorites",
            "icon": "Star",
            "sort_order": 0,  # Favorites always first
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
        }

        result = await self.collection.insert_one(project_dict)
        project_dict["id"] = str(result.inserted_id)

        return Project(**project_dict)

    async def get_or_create_by_name(
        self, user_id: str, name: str, project_type: str = "channel", icon: str = "💬"
    ) -> Project:
        """Get or create a project by name for a user.

        Used by channels (e.g. Feishu) to auto-create a project for organizing conversations.
        """
        # 中文：外部渠道（如飞书机器人）接入的会话没有用户手动创建项目的过程，
        # 这里按 (user_id, name, project_type) 做"存在则复用、不存在则创建"，
        # 让同一个渠道来源的对话自动归类到同一个项目下
        project_dict = await self.collection.find_one(
            {"user_id": user_id, "name": name, "type": project_type}
        )
        if project_dict:
            project_dict["id"] = str(project_dict.pop("_id"))
            return Project(**project_dict)

        now = utc_now()
        project_dict = {
            "name": name,
            "type": project_type,
            "icon": icon,
            "sort_order": 100,
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
        }
        result = await self.collection.insert_one(project_dict)
        project_dict["id"] = str(result.inserted_id)
        return Project(**project_dict)


# Singleton instance
# 中文：模块级单例，避免每次调用 get_project_storage() 都重新创建
# ProjectStorage 实例（及其内部的懒加载数据库集合连接）
_project_storage: Optional[ProjectStorage] = None


def get_project_storage() -> ProjectStorage:
    """Get project storage singleton."""
    global _project_storage
    if _project_storage is None:
        _project_storage = ProjectStorage()
    return _project_storage

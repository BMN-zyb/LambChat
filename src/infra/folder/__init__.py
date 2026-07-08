"""Project storage module."""
# 中文说明：folder 包对外只暴露 ProjectStorage 一个类，
# 用于管理会话/对话在"项目（文件夹）"维度的组织归类（见 storage.py）

from src.infra.folder.storage import ProjectStorage

__all__ = ["ProjectStorage"]

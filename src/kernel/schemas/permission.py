"""
权限相关的 Pydantic 模型
"""
# 本模块承担两件事：
# 1. 定义向前端暴露"权限列表"接口所需的响应模型（PermissionInfo/PermissionGroup/PermissionsResponse）；
# 2. 维护权限的元数据（中文名称、描述）与分组配置（PERMISSION_METADATA/PERMISSION_GROUPS_CONFIG），
#    并通过 get_permissions_response() 把二者组装成响应对象。
# 权限的"真值来源"是 src.kernel.types.Permission 枚举，本模块只负责展示层的包装，
# 通常被角色管理/权限管理相关的 API 路由和管理后台前端使用。

from typing import TypedDict

from pydantic import BaseModel

# Permission 是系统权限的枚举真值来源，定义在 src/kernel/types.py
from src.kernel.types import Permission


# 单个权限的展示信息，用于前端渲染权限勾选项/说明文案
class PermissionInfo(BaseModel):
    """单个权限信息"""

    # 权限的原始取值，对应 Permission 枚举的 value（如 "chat:read"）
    value: str
    # 权限的中文展示名称（如"读取聊天"）
    label: str
    # 权限的详细说明文案，默认空字符串
    description: str = ""


# 一组相关权限的集合，对应权限管理页面中的一个分组（如"聊天""会话"）
class PermissionGroup(BaseModel):
    """权限分组"""

    # 分组的中文名称
    name: str
    # 该分组下包含的权限信息列表
    permissions: list[PermissionInfo]


# GET 权限列表接口的响应体，同时提供"按分组"和"扁平化全量列表"两种视图
class PermissionsResponse(BaseModel):
    """权限列表响应"""

    # 按分组组织的权限信息，供前端分组展示
    groups: list[PermissionGroup]
    # 所有权限的扁平化列表（不分组），供前端做全量查找/校验
    all_permissions: list[PermissionInfo]


# 内部使用的分组配置结构（非对外 API 模型），用于声明 PERMISSION_GROUPS_CONFIG 的每一项
class PermissionGroupConfig(TypedDict):
    """权限分组配置"""

    # 分组中文名称
    name: str
    # 该分组包含哪些权限（存的是 Permission.xxx.value 字符串，而非枚举对象）
    permissions: list[str]


# 权限元数据配置
# 结构：外层 key 为 Permission 枚举的 value（权限唯一标识字符串），
# 外层 value 是一个含 "label"（中文展示名）和 "description"（中文说明）的字典。
# 下面按业务模块用注释分段（Chat/Session/Skill/...），顺序与 Permission 枚举定义顺序保持一致，
# 主要供 get_permissions_response() 读取，为每个权限值组装出 PermissionInfo。
PERMISSION_METADATA: dict[str, dict[str, str]] = {
    # Chat
    Permission.CHAT_READ.value: {
        "label": "读取聊天",
        "description": "查看聊天消息",
    },
    Permission.CHAT_WRITE.value: {
        "label": "发送消息",
        "description": "发送聊天消息",
    },
    # Session
    Permission.SESSION_READ.value: {
        "label": "读取会话",
        "description": "查看会话列表和内容",
    },
    Permission.SESSION_WRITE.value: {
        "label": "创建/更新会话",
        "description": "创建和修改会话",
    },
    Permission.SESSION_DELETE.value: {
        "label": "删除会话",
        "description": "删除会话",
    },
    Permission.SESSION_ADMIN.value: {
        "label": "管理所有会话",
        "description": "查看和管理所有用户的会话（管理员权限）",
    },
    Permission.SESSION_SHARE.value: {
        "label": "分享会话",
        "description": "创建和管理会话分享链接",
    },
    # Skill
    Permission.SKILL_READ.value: {
        "label": "读取技能",
        "description": "查看技能列表和内容",
    },
    Permission.SKILL_WRITE.value: {
        "label": "创建/更新技能",
        "description": "创建和修改技能",
    },
    Permission.SKILL_DELETE.value: {
        "label": "删除技能",
        "description": "删除技能",
    },
    Permission.SKILL_ADMIN.value: {
        "label": "管理技能",
        "description": "管理技能的完整权限",
    },
    # User
    Permission.USER_READ.value: {
        "label": "读取用户",
        "description": "查看用户列表和信息",
    },
    Permission.USER_WRITE.value: {
        "label": "创建/更新用户",
        "description": "创建和修改用户",
    },
    Permission.USER_DELETE.value: {
        "label": "删除用户",
        "description": "删除用户",
    },
    # Role
    Permission.ROLE_MANAGE.value: {
        "label": "管理角色",
        "description": "管理角色和权限分配",
    },
    # Settings
    Permission.SETTINGS_MANAGE.value: {
        "label": "管理系统设置",
        "description": "修改系统配置",
    },
    # MCP
    Permission.MCP_READ.value: {
        "label": "读取MCP配置",
        "description": "查看MCP服务配置",
    },
    Permission.MCP_WRITE_SSE.value: {
        "label": "创建SSE类型MCP",
        "description": "创建SSE传输类型的MCP服务",
    },
    Permission.MCP_WRITE_HTTP.value: {
        "label": "创建HTTP类型MCP",
        "description": "创建HTTP/streamable_http传输类型的MCP服务",
    },
    Permission.MCP_WRITE_SANDBOX.value: {
        "label": "创建Sandbox类型MCP",
        "description": "创建Sandbox传输类型的MCP服务（在沙箱内运行）",
    },
    Permission.MCP_DELETE.value: {
        "label": "删除MCP配置",
        "description": "删除MCP服务配置",
    },
    Permission.MCP_ADMIN.value: {
        "label": "管理MCP服务",
        "description": "管理MCP服务的完整权限",
    },
    # File
    Permission.FILE_UPLOAD.value: {
        "label": "上传文件",
        "description": "上传文件和头像",
    },
    Permission.FILE_UPLOAD_IMAGE.value: {
        "label": "上传图片",
        "description": "允许上传图片文件（jpg, png, gif 等）",
    },
    Permission.FILE_UPLOAD_VIDEO.value: {
        "label": "上传视频",
        "description": "允许上传视频文件（mp4, webm 等）",
    },
    Permission.FILE_UPLOAD_AUDIO.value: {
        "label": "上传音频",
        "description": "允许上传音频文件（mp3, wav 等）",
    },
    Permission.FILE_UPLOAD_DOCUMENT.value: {
        "label": "上传文档",
        "description": "允许上传文档文件（pdf, word, excel 等）",
    },
    # Avatar
    Permission.AVATAR_UPLOAD.value: {
        "label": "上传头像",
        "description": "允许上传和删除用户头像",
    },
    # Feedback
    Permission.FEEDBACK_WRITE.value: {
        "label": "提交反馈",
        "description": "允许提交消息反馈（点赞/点踩）",
    },
    Permission.FEEDBACK_READ.value: {
        "label": "查看反馈",
        "description": "查看反馈列表和统计",
    },
    Permission.FEEDBACK_ADMIN.value: {
        "label": "管理反馈",
        "description": "删除和管理所有用户反馈",
    },
    # Agent
    Permission.AGENT_READ.value: {
        "label": "读取智能体",
        "description": "查看智能体配置和状态",
    },
    Permission.AGENT_ADMIN.value: {
        "label": "管理智能体",
        "description": "创建、修改和删除智能体配置（管理员权限）",
    },
    # Team
    Permission.TEAM_READ.value: {
        "label": "查看团队",
        "description": "查看自己的智能体团队",
    },
    Permission.TEAM_WRITE.value: {
        "label": "管理团队",
        "description": "创建和修改自己的智能体团队",
    },
    Permission.TEAM_DELETE.value: {
        "label": "删除团队",
        "description": "删除自己的智能体团队",
    },
    # Model
    Permission.MODEL_ADMIN.value: {
        "label": "管理模型",
        "description": "管理角色可用的模型分配（管理员权限）",
    },
    # Channel - Generic
    Permission.CHANNEL_READ.value: {
        "label": "查看渠道",
        "description": "查看渠道配置和连接状态",
    },
    Permission.CHANNEL_WRITE.value: {
        "label": "配置渠道",
        "description": "创建和修改渠道配置",
    },
    Permission.CHANNEL_DELETE.value: {
        "label": "删除渠道",
        "description": "删除渠道配置",
    },
    # Marketplace
    Permission.MARKETPLACE_READ.value: {
        "label": "浏览商店",
        "description": "查看和浏览技能商店",
    },
    Permission.MARKETPLACE_PUBLISH.value: {
        "label": "发布技能",
        "description": "发布和更新商店中的技能",
    },
    Permission.MARKETPLACE_ADMIN.value: {
        "label": "管理商店",
        "description": "管理技能商店（激活/停用/删除任意技能）",
    },
    # Persona Preset
    Permission.PERSONA_PRESET_READ.value: {
        "label": "浏览角色预设",
        "description": "查看角色广场和自己的角色预设",
    },
    Permission.PERSONA_PRESET_WRITE.value: {
        "label": "管理个人角色预设",
        "description": "创建、编辑、删除自己的角色预设副本",
    },
    Permission.PERSONA_PRESET_ADMIN.value: {
        "label": "管理全局角色预设",
        "description": "创建、发布、归档和删除全局角色预设",
    },
    # Scheduled Task
    Permission.SCHEDULED_TASK_READ.value: {
        "label": "读取定时任务",
        "description": "查看定时任务、运行历史和任务会话",
    },
    Permission.SCHEDULED_TASK_WRITE.value: {
        "label": "管理定时任务",
        "description": "创建、编辑、暂停、恢复和手动执行定时任务",
    },
    Permission.SCHEDULED_TASK_DELETE.value: {
        "label": "删除定时任务",
        "description": "删除自己的定时任务",
    },
    # Notification
    Permission.NOTIFICATION_MANAGE.value: {
        "label": "管理通知",
        "description": "创建、编辑、删除系统通知公告",
    },
    # Usage
    Permission.USAGE_READ.value: {
        "label": "查看用量统计",
        "description": "查看自己的模型调用和 Token 用量统计",
    },
    Permission.USAGE_ADMIN.value: {
        "label": "管理用量统计",
        "description": "查看所有用户的模型调用和 Token 用量统计",
    },
    # Environment Variables
    Permission.ENVVAR_READ.value: {
        "label": "读取环境变量",
        "description": "查看用户环境变量",
    },
    Permission.ENVVAR_WRITE.value: {
        "label": "管理环境变量",
        "description": "创建和更新用户环境变量",
    },
    Permission.ENVVAR_DELETE.value: {
        "label": "删除环境变量",
        "description": "删除用户环境变量",
    },
}

# 权限分组配置
# 结构：列表中每一项是一个 PermissionGroupConfig（{"name": 分组中文名, "permissions": [权限value,...]}）。
# 列表顺序即为前端权限管理页面中分组的展示顺序；每个分组内的权限顺序同理决定展示顺序。
# 注意：这里只登记"分组包含哪些权限"，具体的 label/description 仍从 PERMISSION_METADATA 中查表获得。
PERMISSION_GROUPS_CONFIG: list[PermissionGroupConfig] = [
    {
        "name": "聊天",
        "permissions": [
            Permission.CHAT_READ.value,
            Permission.CHAT_WRITE.value,
        ],
    },
    {
        "name": "会话",
        "permissions": [
            Permission.SESSION_READ.value,
            Permission.SESSION_WRITE.value,
            Permission.SESSION_DELETE.value,
            Permission.SESSION_ADMIN.value,
            Permission.SESSION_SHARE.value,
        ],
    },
    {
        "name": "技能",
        "permissions": [
            Permission.SKILL_READ.value,
            Permission.SKILL_WRITE.value,
            Permission.SKILL_DELETE.value,
            Permission.SKILL_ADMIN.value,
        ],
    },
    {
        "name": "用户管理",
        "permissions": [
            Permission.USER_READ.value,
            Permission.USER_WRITE.value,
            Permission.USER_DELETE.value,
        ],
    },
    {
        "name": "角色管理",
        "permissions": [
            Permission.ROLE_MANAGE.value,
        ],
    },
    {
        "name": "系统设置",
        "permissions": [
            Permission.SETTINGS_MANAGE.value,
        ],
    },
    {
        "name": "MCP服务",
        "permissions": [
            Permission.MCP_READ.value,
            Permission.MCP_WRITE_SSE.value,
            Permission.MCP_WRITE_HTTP.value,
            Permission.MCP_WRITE_SANDBOX.value,
            Permission.MCP_DELETE.value,
            Permission.MCP_ADMIN.value,
        ],
    },
    {
        "name": "文件上传",
        "permissions": [
            Permission.FILE_UPLOAD.value,
            Permission.FILE_UPLOAD_IMAGE.value,
            Permission.FILE_UPLOAD_VIDEO.value,
            Permission.FILE_UPLOAD_AUDIO.value,
            Permission.FILE_UPLOAD_DOCUMENT.value,
        ],
    },
    {
        "name": "头像",
        "permissions": [
            Permission.AVATAR_UPLOAD.value,
        ],
    },
    {
        "name": "反馈",
        "permissions": [
            Permission.FEEDBACK_WRITE.value,
            Permission.FEEDBACK_READ.value,
            Permission.FEEDBACK_ADMIN.value,
        ],
    },
    {
        "name": "智能体",
        "permissions": [
            Permission.AGENT_READ.value,
            Permission.AGENT_ADMIN.value,
        ],
    },
    {
        "name": "智能体团队",
        "permissions": [
            Permission.TEAM_READ.value,
            Permission.TEAM_WRITE.value,
            Permission.TEAM_DELETE.value,
        ],
    },
    {
        "name": "模型管理",
        "permissions": [
            Permission.MODEL_ADMIN.value,
        ],
    },
    {
        "name": "渠道管理",
        "permissions": [
            Permission.CHANNEL_READ.value,
            Permission.CHANNEL_WRITE.value,
            Permission.CHANNEL_DELETE.value,
        ],
    },
    {
        "name": "技能商店",
        "permissions": [
            Permission.MARKETPLACE_READ.value,
            Permission.MARKETPLACE_PUBLISH.value,
            Permission.MARKETPLACE_ADMIN.value,
        ],
    },
    {
        "name": "角色预设",
        "permissions": [
            Permission.PERSONA_PRESET_READ.value,
            Permission.PERSONA_PRESET_WRITE.value,
            Permission.PERSONA_PRESET_ADMIN.value,
        ],
    },
    {
        "name": "定时任务",
        "permissions": [
            Permission.SCHEDULED_TASK_READ.value,
            Permission.SCHEDULED_TASK_WRITE.value,
            Permission.SCHEDULED_TASK_DELETE.value,
        ],
    },
    {
        "name": "通知公告",
        "permissions": [
            Permission.NOTIFICATION_MANAGE.value,
        ],
    },
    {
        "name": "用量统计",
        "permissions": [
            Permission.USAGE_READ.value,
            Permission.USAGE_ADMIN.value,
        ],
    },
    {
        "name": "环境变量",
        "permissions": [
            Permission.ENVVAR_READ.value,
            Permission.ENVVAR_WRITE.value,
            Permission.ENVVAR_DELETE.value,
        ],
    },
]


def get_permissions_response() -> PermissionsResponse:
    """
    获取权限列表响应

    Returns:
        PermissionsResponse: 包含所有权限分组和权限列表
    """
    # 构建权限分组
    groups: list[PermissionGroup] = []
    # 同时维护一份不分组的扁平列表，供需要全量权限的场景使用
    all_permissions: list[PermissionInfo] = []

    # 按 PERMISSION_GROUPS_CONFIG 中登记的分组顺序逐个处理
    for group_config in PERMISSION_GROUPS_CONFIG:
        # 当前分组下的权限信息集合
        group_permissions: list[PermissionInfo] = []
        # 遍历该分组包含的每个权限 value
        for perm_value in group_config["permissions"]:
            # 从元数据表中查询该权限的中文标签/说明，查不到则用空字典兜底
            metadata = PERMISSION_METADATA.get(perm_value, {})
            # 组装单个权限的展示信息；label/description 缺失时分别回退为 value 本身/空字符串
            perm_info = PermissionInfo(
                value=perm_value,
                label=metadata.get("label", perm_value),
                description=metadata.get("description", ""),
            )
            # 同时计入当前分组和全局扁平列表
            group_permissions.append(perm_info)
            all_permissions.append(perm_info)

        # 当前分组处理完毕，包装为 PermissionGroup 加入结果列表
        groups.append(
            PermissionGroup(
                name=group_config["name"],
                permissions=group_permissions,
            )
        )

    # 汇总分组视图与扁平视图，返回最终响应
    return PermissionsResponse(
        groups=groups,
        all_permissions=all_permissions,
    )

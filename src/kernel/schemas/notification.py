"""通知系统 Schema"""

# 模块说明：定义系统公告/通知相关的数据模型。
# 通知支持多语言文本，并可配置一个可选的生效时间窗口（start_time ~ end_time），
# 只有 is_active=True 且当前时间落在该窗口内（或未设置起止时间）的通知才会展示给用户。
# 主要使用方：src/infra/notification/manager.py / storage.py（通知的增删改查与生效判断）、
# src/api/routes/notification.py（通知管理与查询接口）。
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict


# 通知类型枚举，主要影响前端展示的样式/图标。
class NotificationType(str, Enum):
    # 一般信息类通知
    INFO = "info"
    # 成功/正向类通知
    SUCCESS = "success"
    # 警告类通知
    WARNING = "warning"
    # 系统维护类通知
    MAINTENANCE = "maintenance"


# 多语言文本，用于通知标题/正文的多语言展示，五种语言均为必填。
class I18nText(BaseModel):
    """多语言文本"""

    # 英文
    en: str
    # 中文
    zh: str
    # 日文
    ja: str
    # 韩文
    ko: str
    # 俄文
    ru: str


# 创建通知的请求体。
class NotificationCreate(BaseModel):
    """创建通知"""

    # 多语言标题
    title_i18n: I18nText
    # 多语言正文内容
    content_i18n: I18nText
    # 通知类型，默认普通信息
    type: NotificationType = NotificationType.INFO
    # 生效窗口开始时间，为空表示立即生效（不限制开始时间）
    start_time: Optional[datetime] = None
    # 生效窗口结束时间，为空表示永不过期（不限制结束时间）
    end_time: Optional[datetime] = None
    # 总开关：为 False 时无论是否在生效窗口内都不会展示
    is_active: bool = True


# 更新通知的请求体，所有字段均可选（PATCH 语义）。
class NotificationUpdate(BaseModel):
    """更新通知"""

    title_i18n: Optional[I18nText] = None
    content_i18n: Optional[I18nText] = None
    type: Optional[NotificationType] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    is_active: Optional[bool] = None


# 通知的响应模型（数据库实体视图）。
class Notification(BaseModel):
    """通知响应"""

    # 通知 ID
    id: str
    title_i18n: I18nText
    content_i18n: I18nText
    type: NotificationType = NotificationType.INFO
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    is_active: bool
    # 创建时间
    created_at: datetime
    # 最近更新时间
    updated_at: datetime
    # 创建人（用户 ID）
    created_by: str

    # 允许从属性对象（如 Mongo 文档转换后的对象）直接构造本模型
    model_config = ConfigDict(from_attributes=True)


# 数据库中的通知完整字段视图；目前字段与 Notification 一致，
# 预留用于未来扩展仅内部使用、不对外暴露的字段。
class NotificationInDB(Notification):
    """数据库中的通知（完整字段）"""

    pass


# 通知列表接口的响应体。
class NotificationListResponse(BaseModel):
    """通知列表响应"""

    # 通知列表
    items: list[Notification]
    # 总条数
    total: int

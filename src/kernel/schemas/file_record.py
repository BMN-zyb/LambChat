"""File record schema for content-hash based deduplication."""

# 模块说明：定义基于"内容哈希"实现文件去重的数据模型。
# 相同内容（SHA-256 相同）的文件只会在对象存储和数据库中保存一份，
# 不同用户/多次上传同一份文件时只增加引用计数，不重复占用存储空间。
# 主要使用方：src/infra/upload/file_record.py（文件记录的增删查、引用计数增减）、
# src/api/routes/upload.py（文件上传接口）、src/infra/writer/present.py（渲染文件展示信息）。
from datetime import datetime

from pydantic import BaseModel, Field

from src.infra.utils.datetime import utc_now


class FileRecordSchema(BaseModel):
    """Represents a file record in MongoDB, keyed by content hash."""

    # 数据库主键；Mongo 中字段名为 "_id"，通过 alias 映射为 Python 侧更常用的 id
    id: str = Field(alias="_id")
    # 内容哈希（SHA-256 十六进制摘要），是去重的核心依据：内容相同则哈希相同
    hash: str  # SHA-256 hex digest
    # 对象存储中的对象键，实际文件内容按该 key 存取
    key: str  # Storage object key, e.g. "user_id/abc123hash"
    # 用户上传时的原始文件名，用于下载/展示时还原文件名
    name: str  # Original filename
    # 文件的 MIME 类型，如 "image/png"
    mime_type: str
    # 文件大小，单位字节
    size: int
    # 文件大类，用于分类限额校验等场景
    category: str  # "image", "video", "audio", "document"
    # 首次上传该内容的用户 ID；之后其他用户上传相同内容时不会更改此字段，
    # 只会增加引用计数（见 FileRecordStorage.add_references）
    uploaded_by: str  # User ID of first uploader
    # 记录创建时间，即该内容首次被上传的时间
    created_at: datetime = Field(default_factory=utc_now)

    # 允许同时使用字段名 id 或别名 _id 赋值/构造模型，便于直接从 Mongo 文档转换
    model_config = {"populate_by_name": True}

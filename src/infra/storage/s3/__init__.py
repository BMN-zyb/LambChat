"""
S3-compatible storage service

Supports multiple S3-compatible providers:
- AWS S3
- Alibaba Cloud OSS
- Tencent Cloud COS
- MinIO
- Any S3-compatible storage
- Local filesystem storage
"""

# 对象存储抽象层的包导出入口：把类型定义（types）、后端协议（base）、具体后端实现（backends）、
# 以及对外统一封装的高层服务（service）汇集到一起，方便上层代码统一从 src.infra.storage.s3 导入。
from src.infra.storage.s3.backends import (
    AliyunOssBackend,
    LocalStorageBackend,
    MinioS3Backend,
)
from src.infra.storage.s3.base import S3StorageBackend
from src.infra.storage.s3.service import (
    S3StorageService,
    close_storage,
    get_storage_service,
    init_storage,
)
from src.infra.storage.s3.types import S3Config, S3Provider, UploadResult

__all__ = [
    # Types
    "S3Config",
    "S3Provider",
    "UploadResult",
    # Base
    "S3StorageBackend",
    # Backends
    "AliyunOssBackend",
    "LocalStorageBackend",
    "MinioS3Backend",
    # Service
    "S3StorageService",
    "get_storage_service",
    "init_storage",
    "close_storage",
]

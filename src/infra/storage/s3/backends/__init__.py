"""
对象存储后端汇总模块。

统一收拢 local / minio / aliyun 三种 S3StorageBackend 实现，供上层按配置
（S3Provider）选择具体后端实例化，调用方无需关心各后端内部差异。
"""

# 本地文件系统后端：无外部依赖，常用于本地开发或没有配置对象存储时的兜底方案
from src.infra.storage.s3.backends.local import LocalStorageBackend
# 基于 minio SDK 实现的后端，兼容 AWS S3 / MinIO / 腾讯云 COS 等任意 S3 协议服务
from src.infra.storage.s3.backends.minio import MinioS3Backend

try:
    # 阿里云 OSS 后端依赖 oss2 这个可选第三方库，未安装时优雅降级，
    # 避免因为一个可选依赖缺失就导致整个存储模块无法导入
    from src.infra.storage.s3.backends.aliyun import AliyunOssBackend
except ImportError:
    AliyunOssBackend = None  # type: ignore[assignment,misc]

__all__ = [
    "AliyunOssBackend",
    "LocalStorageBackend",
    "MinioS3Backend",
]

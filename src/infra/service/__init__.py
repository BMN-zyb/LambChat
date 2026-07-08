"""
第三方服务模块
"""

from src.infra.service.base import BaseService

# 注意:下面 import 的 src.infra.service.milvus / prometheus 两个模块在仓库中并不存在
# (疑似死引用/历史遗留)。此处按要求保留原样、不做修复或删除,仅作说明。
from src.infra.service.milvus import MilvusService
from src.infra.service.prometheus import PrometheusService

__all__ = [
    "BaseService",
    "MilvusService",
    "PrometheusService",
]

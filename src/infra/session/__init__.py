"""
会话管理模块
"""

# DualEventWriter：双写器，负责把流式事件同时写入 Redis Stream（实时 SSE）与 Mongo（持久化）
# get_dual_writer：获取进程级单例双写器的工厂函数
from src.infra.session.dual_writer import DualEventWriter, get_dual_writer
# SessionManager：会话的增删改查、fork、附件等业务编排入口
from src.infra.session.manager import SessionManager
# SessionStorage：会话元数据的底层存储访问层
from src.infra.session.storage import SessionStorage

# 显式声明对外导出的公共符号，控制 from ... import * 的行为并作为包的公开 API 清单
__all__ = [
    "SessionManager",
    "SessionStorage",
    "DualEventWriter",
    "get_dual_writer",
]

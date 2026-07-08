"""Email service module."""

# 邮件基础设施包的入口：从 service 模块导出对外使用的核心符号
# EmailService：基于 Resend API 的邮件发送服务类（单例）
# get_email_service：获取单例实例的异步工厂函数
# close_email_service：进程关闭时释放 HTTP 客户端等资源
from src.infra.email.service import EmailService, close_email_service, get_email_service

# 显式声明包对外公开的 API，控制 `from ... import *` 的导出范围
__all__ = ["EmailService", "close_email_service", "get_email_service"]

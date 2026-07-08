"""
共享内核 (Shared Kernel)

零依赖的核心模块，包含：
- 异常定义
- 类型/协议定义
- Pydantic 模型
"""

# kernel 包"零依赖"在架构上的含义：kernel 位于整个分层架构的最底层，
# 不依赖 infra/agents/api 等上层模块，因此可以被项目中任何地方安全导入，
# 不会产生循环依赖问题，这是典型的"共享内核 (Shared Kernel)"分层设计模式。
from src.kernel.exceptions import (
    AgentError,
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    NotFoundError,
    ValidationError,
)
# 从 src.kernel.types 中选取部分协议（Protocol）与枚举定义重新导出：
# Permission 是权限枚举，AgentProtocol/StorageProtocol/LLMClientProtocol
# 分别是 Agent、存储、LLM 客户端的接口协议
from src.kernel.types import (
    AgentProtocol,
    LLMClientProtocol,
    Permission,
    StorageProtocol,
)

# 注意：这里只是"选择性重新导出"，并非把 exceptions.py / types.py 中的所有符号都导出。
# exceptions.py 中还定义了 StorageError/LLMError/ToolError/SkillError/SessionError/
# EmailNotVerifiedError/AccountNotActiveError；types.py 中还有 MessageType/ToolProtocol/
# Permission 相关的枚举等内容，均未在此重新导出。也就是说这里只暴露一个精简的、
# 最常用的顶层 API 表面，其余符号需要各自从 src.kernel.exceptions 或
# src.kernel.types 直接导入。
__all__ = [
    # 异常
    "AgentError",
    "AuthenticationError",
    "AuthorizationError",
    "ConfigurationError",
    "NotFoundError",
    "ValidationError",
    # 类型
    "Permission",
    "AgentProtocol",
    "StorageProtocol",
    "LLMClientProtocol",
]

"""DeepAgent middleware: retry, prompt injection, tool interception, and prompt caching."""

# 产物投递：把工具产生的文件/图片等 artifact 交付给前端
from src.infra.agent.middleware.artifact_delivery import ArtifactDeliveryMiddleware
# 代码解释器中间件工厂
from src.infra.agent.middleware.code_interpreter import create_code_interpreter_middleware
# 将消息中的图片 URL 转成 base64（部分模型只接受内联图片）
from src.infra.agent.middleware.image_url import ImageUrlToBase64Middleware
# 主 agent 上下文快照：把主 agent 上下文传递给子 agent
from src.infra.agent.middleware.main_agent_context import MainAgentContextMiddleware
# Anthropic 提示缓存：给 system/消息打 cache_control 标记以命中缓存
from src.infra.agent.middleware.prompt_caching import PromptCachingMiddleware
# 提示注入相关：环境变量、记忆索引、沙箱 MCP、分节提示
from src.infra.agent.middleware.prompt_injection import (
    EnvVarPromptMiddleware,
    MemoryIndexMiddleware,
    SandboxMCPMiddleware,
    SectionPromptMiddleware,
)
# 重试与模型 fallback：空内容重试、模型降级、重试中间件工厂
from src.infra.agent.middleware.retry import (
    EmptyContentRetryMiddleware,
    ModelFallbackMiddleware,
    _is_empty_content,
    create_retry_middleware,
)
# 子 agent 活动状态透传
from src.infra.agent.middleware.subagent_activity import SubagentActivityMiddleware
# 子 agent 结果交接（把子 agent 结果规整后交回主流程）
from src.infra.agent.middleware.subagent_result_handoff import SubagentResultHandoffMiddleware
# 工具拦截：MCP 配额、工具结果二进制处理、工具搜索
from src.infra.agent.middleware.tool_interception import (
    MCPQuotaMiddleware,
    ToolResultBinaryMiddleware,
    ToolSearchMiddleware,
)

# 对外导出的全部中间件符号
__all__ = [
    "create_retry_middleware",
    "create_code_interpreter_middleware",
    "ArtifactDeliveryMiddleware",
    "EmptyContentRetryMiddleware",
    "EnvVarPromptMiddleware",
    "ImageUrlToBase64Middleware",
    "MainAgentContextMiddleware",
    "MCPQuotaMiddleware",
    "MemoryIndexMiddleware",
    "ModelFallbackMiddleware",
    "PromptCachingMiddleware",
    "SandboxMCPMiddleware",
    "SectionPromptMiddleware",
    "SubagentActivityMiddleware",
    "SubagentResultHandoffMiddleware",
    "ToolResultBinaryMiddleware",
    "ToolSearchMiddleware",
    "_is_empty_content",
]

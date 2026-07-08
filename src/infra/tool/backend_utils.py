"""
Backend 工具函数

从 ToolRuntime 获取 Backend 的共享工具函数。
用于支持分布式环境下的安全 backend 访问。
"""

from typing import Any, Optional

from deepagents.backends.protocol import SandboxBackendProtocol

from src.infra.logging import get_logger

logger = get_logger(__name__)


# 本文件所有函数都从 LangChain ToolRuntime 的 config["configurable"] 里取值，
# 而不是依赖 ContextVar：因为 nested graph（外层 graph 调内层 deepagents graph）、
# 子 agent、以及跨 worker 执行时，ContextVar 不一定能正确传播，
# 而 runtime.config 是随调用链显式传递的，天然对分布式/嵌套场景安全。
def get_user_id_from_runtime(runtime: Any) -> Optional[str]:
    """从 ToolRuntime 获取 user_id。

    通过 runtime.config.configurable.context.user_id 获取，
    与 get_backend_from_runtime 同风格。
    """
    if runtime is not None:
        if hasattr(runtime, "config") and runtime.config:
            config = runtime.config
            if isinstance(config, dict):
                configurable = config.get("configurable", {})
                if isinstance(configurable, dict):
                    ctx = configurable.get("context")
                    # ctx 是 agent 运行时上下文对象（并非普通 dict），用 hasattr 而非 isinstance
                    # 判断，兼容不同版本 deepagents/LangGraph 里 context 的具体实现类型
                    if ctx and hasattr(ctx, "user_id"):
                        return ctx.user_id
    return None


# 依次尝试三个来源，兼容不同调用路径下 session_id 挂载的位置不一样
def get_session_id_from_runtime(runtime: Any) -> Optional[str]:
    """从 ToolRuntime 获取 session_id。

    Agent context 本身带 session_id；工具索引用它兜底，避免只依赖 ContextVar。
    """
    if runtime is not None:
        if hasattr(runtime, "config") and runtime.config:
            config = runtime.config
            if isinstance(config, dict):
                configurable = config.get("configurable", {})
                if isinstance(configurable, dict):
                    # 最直接的来源：调用方显式把 session_id 放进了 configurable
                    session_id = configurable.get("session_id")
                    if session_id:
                        return str(session_id)

                    # 其次从 presenter（负责向前端推送事件的对象）上取，
                    # 它通常持有当前会话的 session_id
                    presenter = configurable.get("presenter")
                    presenter_session_id = getattr(presenter, "session_id", None)
                    if presenter_session_id:
                        return str(presenter_session_id)

                    # 最后兜底：agent 运行时的通用 context 对象上也带 session_id
                    ctx = configurable.get("context")
                    context_session_id = getattr(ctx, "session_id", None)
                    if context_session_id:
                        return str(context_session_id)
    return None


def get_base_url_from_runtime(runtime: Any) -> str:
    """从 ToolRuntime 获取 base_url（与 get_backend_from_runtime 同风格）

    优先级：runtime.config > settings.APP_BASE_URL 环境变量
    用于确保上传/下载生成的 URL 始终带完整前缀。
    """
    # 生成的文件/上传 URL 必须带上完整域名前缀才能在前端直接访问，
    # 因此这里要拿到发起 HTTP 请求时的 base_url（反向代理场景下 host 可能不固定）
    # 1. 从 runtime.config 获取（request.base_url 传递过来的）
    if runtime is not None:
        if hasattr(runtime, "config") and runtime.config:
            config = runtime.config
            if isinstance(config, dict):
                configurable = config.get("configurable", {})
                if isinstance(configurable, dict):
                    base_url = configurable.get("base_url", "")
                    if base_url:
                        return base_url.rstrip("/")

    # 2. fallback: 从环境变量 APP_BASE_URL 获取
    from src.kernel.config import settings

    env_base_url = getattr(settings, "APP_BASE_URL", "")
    if env_base_url:
        return env_base_url.rstrip("/")

    return ""


def get_trace_id_from_runtime(runtime: Any) -> Optional[str]:
    """从 ToolRuntime 获取 trace_id。

    优先级：显式 configurable.trace_id > presenter.trace_id > context.trace_id。
    这样工具不依赖 ContextVar 跨 worker / nested graph 边界传播。
    """
    if runtime is not None:
        if hasattr(runtime, "config") and runtime.config:
            config = runtime.config
            if isinstance(config, dict):
                configurable = config.get("configurable", {})
                if isinstance(configurable, dict):
                    # 最直接的来源：调用方显式传入的 trace_id
                    trace_id = configurable.get("trace_id")
                    if trace_id:
                        return str(trace_id)

                    # 其次从事件推送器 presenter 上取（构造时通常已绑定当前 trace）
                    presenter = configurable.get("presenter")
                    presenter_trace_id = getattr(presenter, "trace_id", None)
                    if presenter_trace_id:
                        return str(presenter_trace_id)

                    # 最后从通用 context 对象上兜底获取
                    ctx = configurable.get("context")
                    context_trace_id = getattr(ctx, "trace_id", None)
                    if context_trace_id:
                        return str(context_trace_id)
    return None


# 让工具（如图片分析）能拿到当前这轮用户消息附带的附件列表，无需重新解析消息历史
def get_attachments_from_runtime(runtime: Any) -> list[dict[str, Any]] | None:
    """Return current message attachments passed through ToolRuntime config."""
    if runtime is not None:
        if hasattr(runtime, "config") and runtime.config:
            config = runtime.config
            if isinstance(config, dict):
                configurable = config.get("configurable", {})
                if isinstance(configurable, dict):
                    attachments = configurable.get("attachments")
                    if isinstance(attachments, list):
                        # 过滤并浅拷贝，防止工具函数拿到的列表被意外修改后影响到 runtime 里的原始数据
                        items = [dict(item) for item in attachments if isinstance(item, dict)]
                        return items or None
    return None


# 标记本次 reveal_file/reveal_project 调用是用户/agent 主动触发，还是
# ArtifactDeliveryMiddleware 自动补投递触发的，便于埋点区分来源
def get_delivery_source_from_runtime(runtime: Any) -> Optional[str]:
    """Return the artifact delivery source marker carried by internal tool calls."""
    if runtime is not None:
        if hasattr(runtime, "config") and runtime.config:
            config = runtime.config
            if isinstance(config, dict):
                configurable = config.get("configurable", {})
                if isinstance(configurable, dict):
                    delivery_source = configurable.get("delivery_source")
                    if delivery_source:
                        return str(delivery_source)
    return None


def get_backend_from_runtime(runtime: Any) -> Optional[SandboxBackendProtocol]:
    """从 ToolRuntime 获取 backend（分布式安全）

    Backend 通过 runtime.config["configurable"]["backend"] 传递
    注意：config 中存的是 backend_factory（函数），需要调用 factory(runtime) 获取实例
    """
    if runtime is None:
        return None

    try:
        # backend 在 config 里存的可能是「已建好的实例」，也可能是「工厂函数」
        # （沙箱场景下常见：需要在实际调用时才按 runtime 懒创建/复用沙箱连接），
        # 因此每种方式都要判断 callable 后再决定是否调用 factory(runtime)。
        # 方式1: 从 runtime.config["configurable"]["backend"] 获取（主要方式）
        if hasattr(runtime, "config") and runtime.config:
            config = runtime.config
            # 检查 configurable 字典
            if isinstance(config, dict):
                configurable = config.get("configurable", {})
                if isinstance(configurable, dict):
                    backend_or_factory = configurable.get("backend")
                    if backend_or_factory is not None:
                        # 如果是工厂函数，调用它获取 backend 实例
                        if callable(backend_or_factory):
                            logger.debug("Calling backend_factory to get backend instance")
                            return backend_or_factory(runtime)
                        else:
                            logger.debug(
                                "Got backend instance from runtime.config['configurable']['backend']"
                            )
                            return backend_or_factory
                # 也检查直接的 backend 键
                backend_or_factory = config.get("backend")
                if backend_or_factory is not None:
                    if callable(backend_or_factory):
                        logger.debug("Calling backend_factory from config['backend']")
                        return backend_or_factory(runtime)
                    else:
                        return backend_or_factory

        # 方式2: 从 runtime 的 attributes 中获取（某些 deepagents 版本/调用路径的兜底位置）
        if hasattr(runtime, "attributes"):
            backend_or_factory = runtime.attributes.get("backend")
            if backend_or_factory is not None:
                if callable(backend_or_factory):
                    logger.debug("Calling backend_factory from attributes")
                    return backend_or_factory(runtime)
                else:
                    return backend_or_factory

        # 方式3: 从 configurable 属性获取（兼容 runtime 直接暴露 configurable 而非 config 的实现）
        if hasattr(runtime, "configurable"):
            configurable = runtime.configurable
            if isinstance(configurable, dict):
                backend_or_factory = configurable.get("backend")
                if backend_or_factory is not None:
                    if callable(backend_or_factory):
                        logger.debug("Calling backend_factory from configurable")
                        return backend_or_factory(runtime)
                    else:
                        return backend_or_factory

    except Exception as e:
        # 拿不到 backend 不应该让整个工具调用崩溃，只记录警告，交给调用方按 None 处理
        logger.warning(f"Failed to get backend from runtime: {e}")

    return None

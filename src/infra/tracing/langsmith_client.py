"""LangSmith tracing client."""

import os
from contextlib import contextmanager
from typing import Any, Generator, Optional

from langsmith import Client

from src.kernel.config import settings


class LangSmithTracer:
    """
    LangSmith tracing integration.

    Environment variables:
    - LANGSMITH_API_KEY: API key for authentication
    - LANGSMITH_PROJECT: Project name for organizing traces
    - LANGSMITH_TRACING: Enable tracing (true/false)
    """

    def __init__(self) -> None:
        # 惰性初始化标志:_enabled 为 None 表示尚未初始化(首次使用时才读取配置)。
        self._enabled: Optional[bool] = None
        self._client: Optional[Client] = None

    def _ensure_initialized(self) -> None:
        """Lazily initialize the tracer on first use."""
        # 惰性初始化:已初始化(_enabled 非 None)则直接返回,保证只执行一次。
        if self._enabled is not None:
            return

        # 由环境变量 LANGSMITH_TRACING 决定是否开启追踪。
        self._enabled = os.getenv("LANGSMITH_TRACING", "false").lower() == "true"

        # settings.LANGSMITH_API_KEY 已在 initialize_settings 时从数据库加载
        # 仅在启用且已配置 API key 时才创建客户端;否则保持 client 为 None(相当于关闭)。
        if self._enabled and settings.LANGSMITH_API_KEY:
            self._client = Client(
                api_key=settings.LANGSMITH_API_KEY,
                api_url=os.getenv("LANGSMITH_API_URL", "https://api.smith.langchain.com"),
            )

    @property
    def enabled(self) -> bool:
        """Check if tracing is enabled."""
        # 访问前先确保已初始化;未启用时返回 False。
        self._ensure_initialized()
        return self._enabled or False

    @property
    def client(self) -> Optional[Client]:
        """Get the LangSmith client."""
        # 返回底层 LangSmith 客户端(未启用/未配置时为 None)。
        self._ensure_initialized()
        return self._client

    @contextmanager
    def trace_run(self, name: str, run_type: str = "chain") -> Generator[Optional[Any], None, None]:
        """Context manager for tracing a run."""
        # 追踪一次 run 的上下文管理器;未启用或无客户端时降级为「空操作」(yield None 直接返回)。
        if not self.enabled or not self.client:
            yield None
            return

        try:
            # Create run with required inputs parameter
            # create_run 要求必须传 inputs,这里以空 dict 占位。
            self.client.create_run(
                name=name,
                run_type=run_type,  # type: ignore[arg-type]
                inputs={},
                project_name=os.getenv("LANGSMITH_PROJECT", "lamb-agent"),
            )
            yield None
            # Note: For proper tracing, use @traceable decorator instead
        except Exception:
            raise

    def get_trace_url(self, run_id: str) -> Optional[str]:
        """Get URL to view trace in LangSmith."""
        # 拼接 LangSmith 上查看该 run 的页面 URL;未启用或缺 run_id 则返回 None。
        if not self.enabled or not run_id:
            return None

        project = os.getenv("LANGSMITH_PROJECT", "default")
        return f"https://smith.langchain.com/o/default/projects/p/{project}/r/{run_id}"

    def flush(self) -> None:
        """Flush pending traces."""
        # 刷写缓冲中的追踪数据(如进程退出前调用),无客户端则不操作。
        if self.client:
            self.client.flush()


# Global tracer instance (lazy initialization)
# 全局单例 tracer:导入即创建对象,但真正的配置读取/客户端构建推迟到首次使用。
tracer = LangSmithTracer()

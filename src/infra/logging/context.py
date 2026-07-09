"""
Trace Context - 分布式追踪上下文

使用 contextvars 存储追踪信息，支持跨异步调用传递。
"""

# ---------------------------------------------------------------------------
# 模块说明：分布式追踪 / 请求上下文（基于 contextvars）
#
# 本模块是「trace 信息自动注入日志」链路的源头，与 filter.py 配套使用：
#   本模块负责「存」——把 trace_id/span_id 等写入 contextvars；
#   filter.py 负责「取」——在每条日志产生时读出并写进 LogRecord。
#
# 为什么用 contextvars 而不是全局变量或手动传参：
#   contextvars 的值绑定到「当前执行上下文」，会随 await / asyncio.Task 自动
#   传播，且并发的不同请求各自隔离、互不串味。因此无需在函数间层层透传
#   trace_id，任意深处的代码都能通过 TraceContext.get() 取到当前请求的追踪信息。
#
# 本模块维护两组相互独立的 contextvars：
#   1) 追踪组：request_id / trace_id / span_id / parent_span_id —— 面向可观测性；
#   2) 请求上下文组：session_id / run_id / user_id 等 —— 面向业务（供工具读取）。
# 两组解耦、互不影响，各自拥有 set / get / clear 方法。
# ---------------------------------------------------------------------------

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


# 追踪信息值对象：TraceContext.get() 的返回类型，聚合一次快照的追踪字段，
# 并提供 is_set()/format() 便于日志渲染
@dataclass
class TraceInfo:
    """追踪信息数据类"""

    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None

    def is_set(self) -> bool:
        """检查是否设置了追踪信息"""
        return self.request_id is not None or self.trace_id is not None

    def format(self) -> str:
        """格式化为日志字符串"""
        if not self.is_set():
            return "-"
        parts = []
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        if self.trace_id:
            parts.append(f"trace_id={self.trace_id}")
        if self.span_id:
            parts.append(f"span_id={self.span_id}")
        return " ".join(parts)


# 请求上下文值对象：TraceContext.get_request_context() 的返回类型，
# 承载业务维度（会话/运行/用户）标识
@dataclass
class RequestContext:
    """请求上下文数据类"""

    request_id: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    user_id: Optional[str] = None
    trace_id: Optional[str] = None


# 追踪上下文管理器：以「类属性 = ContextVar」的形式集中管理全部上下文变量，
# 所有方法均为 classmethod（无需实例化，直接以 TraceContext.xxx 调用）
class TraceContext:
    """
    追踪上下文管理器

    使用 contextvars 存储追踪信息，支持跨异步调用传递。

    Usage:
        # 设置追踪上下文
        TraceContext.set(trace_id="abc123", span_id="def456")

        # 获取追踪信息
        info = TraceContext.get()

        # 清除追踪上下文
        TraceContext.clear()
    """

    # 追踪三元组(trace/span/parent_span)+ request_id,均用 contextvars 存储。
    # contextvars 的关键特性:值绑定到当前执行上下文,能自动随 await/Task 传播,
    # 且并发的不同请求各自隔离,因此无需手动层层传参即可在日志中带上追踪信息。
    _request_id: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
    _trace_id: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)
    _span_id: ContextVar[Optional[str]] = ContextVar("span_id", default=None)
    _parent_span_id: ContextVar[Optional[str]] = ContextVar("parent_span_id", default=None)

    # 请求上下文 - 用于工具等需要访问 session_id/run_id 的场景
    # 这是与上面「追踪」相互独立的第二组 contextvars:承载业务维度的会话/运行/用户标识,
    # 供 Agent 执行期间的工具读取(与日志追踪解耦)。
    _session_id: ContextVar[Optional[str]] = ContextVar("session_id", default=None)
    _run_id: ContextVar[Optional[str]] = ContextVar("run_id", default=None)
    _user_id: ContextVar[Optional[str]] = ContextVar("user_id", default=None)
    _request_context_request_id: ContextVar[Optional[str]] = ContextVar(
        "request_context_request_id", default=None
    )
    _request_trace_id: ContextVar[Optional[str]] = ContextVar("request_trace_id", default=None)

    @classmethod
    def set(
        cls,
        trace_id: str,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        """
        设置追踪上下文

        Args:
            trace_id: 追踪 ID（跨请求唯一）
            span_id: 当前跨度 ID
            parent_span_id: 父跨度 ID（用于嵌套调用）
            request_id: 请求 ID（单次 HTTP 请求唯一）
        """
        cls._request_id.set(request_id)
        cls._trace_id.set(trace_id)
        cls._span_id.set(span_id)
        cls._parent_span_id.set(parent_span_id)

    @classmethod
    def get(cls) -> TraceInfo:
        """
        获取当前追踪信息

        Returns:
            TraceInfo 包含 trace_id, span_id, parent_span_id
        """
        return TraceInfo(
            request_id=cls._request_id.get(),
            trace_id=cls._trace_id.get(),
            span_id=cls._span_id.get(),
            parent_span_id=cls._parent_span_id.get(),
        )

    @classmethod
    def clear(cls) -> None:
        """清除追踪上下文"""
        cls._request_id.set(None)
        cls._trace_id.set(None)
        cls._span_id.set(None)
        cls._parent_span_id.set(None)

    @classmethod
    def new_span(cls, span_id: str) -> str:
        """
        创建新的子跨度

        保存当前 span_id 为 parent_span_id，设置新的 span_id。

        Args:
            span_id: 新的跨度 ID

        Returns:
            之前的 span_id（可作为新的 parent_span_id）
        """
        old_span = cls._span_id.get()
        cls._parent_span_id.set(old_span)
        cls._span_id.set(span_id)
        return old_span or ""

    # ==================== 请求上下文方法 ====================

    @classmethod
    def set_request_context(
        cls,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        user_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        """
        设置请求上下文

        用于在 Agent 执行期间传递 session_id、run_id 等信息给工具。

        Args:
            session_id: 会话 ID
            run_id: 运行 ID
            user_id: 用户 ID
            request_id: 请求 ID
            trace_id: 追踪 ID
        """
        if request_id is not None:
            cls._request_context_request_id.set(request_id)
        if session_id is not None:
            cls._session_id.set(session_id)
        if run_id is not None:
            cls._run_id.set(run_id)
        if user_id is not None:
            cls._user_id.set(user_id)
        if trace_id is not None:
            cls._request_trace_id.set(trace_id)

    @classmethod
    def get_request_context(cls) -> RequestContext:
        """
        获取当前请求上下文

        Returns:
            RequestContext 包含 session_id, run_id, user_id
        """
        return RequestContext(
            request_id=cls._request_context_request_id.get(),
            session_id=cls._session_id.get(),
            run_id=cls._run_id.get(),
            user_id=cls._user_id.get(),
            trace_id=cls._request_trace_id.get(),
        )

    @classmethod
    def clear_request_context(cls) -> None:
        """清除请求上下文"""
        cls._request_context_request_id.set(None)
        cls._session_id.set(None)
        cls._run_id.set(None)
        cls._user_id.set(None)
        cls._request_trace_id.set(None)

    @classmethod
    def get_session_id(cls) -> Optional[str]:
        """获取当前 session_id"""
        return cls._session_id.get()

    @classmethod
    def get_run_id(cls) -> Optional[str]:
        """获取当前 run_id"""
        return cls._run_id.get()

    @classmethod
    def get_user_id(cls) -> Optional[str]:
        """获取当前 user_id"""
        return cls._user_id.get()

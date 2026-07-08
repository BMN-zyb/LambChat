"""Debug logger for LangChain astream_events.

Dumps every raw event from ``astream_events(version="v2")`` to a JSONL file
so developers can inspect the full event stream without a debugger.

Enable via::

    DEBUG_STREAM_EVENTS=true  python -m src.main

Logs are written to ``logs/stream_events_YYYYMMDD_HHMMSS.jsonl``.
Each record includes a ``_context`` object with the LambChat trace/run/session
identity when the event is processed through ``AgentEventProcessor``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from src.infra.async_utils import run_blocking_io

# 模块级单例：_ENABLED 缓存开关状态，_LOG_FILE 缓存打开的日志文件句柄
_ENABLED: bool | None = None
_LOG_FILE: Any = None  # TextIO | None


def _is_enabled() -> bool:
    # 首次调用时解析开关并缓存，避免每个事件都重复读配置
    global _ENABLED
    if _ENABLED is None:
        try:
            # 优先从项目配置读取 DEBUG_STREAM_EVENTS
            from src.kernel.config import settings

            _ENABLED = bool(settings.DEBUG_STREAM_EVENTS)
        except Exception:
            # 配置不可用时回退到环境变量
            _ENABLED = os.getenv("DEBUG_STREAM_EVENTS", "false").lower() in (
                "true",
                "1",
                "yes",
            )
    return _ENABLED


def _get_log_file() -> Any:
    # 惰性创建日志文件：首个事件到来时才在 logs/ 下按时间戳新建 JSONL 文件
    global _LOG_FILE
    if _LOG_FILE is None:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        path = log_dir / f"stream_events_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        _LOG_FILE = open(path, "a", encoding="utf-8")
    return _LOG_FILE


def shutdown() -> None:
    """Close the log file handle. Call during application shutdown."""
    # 应用关闭时刷新并关闭日志句柄，异常一律吞掉（调试功能不应影响退出）
    global _LOG_FILE
    if _LOG_FILE is not None:
        try:
            _LOG_FILE.flush()
            _LOG_FILE.close()
        except Exception:
            pass
        _LOG_FILE = None


# 清洗对象时的防爆保护上限：递归深度、字符串长度、列表/字典元素数
_SANITIZE_MAX_DEPTH = 10
_SANITIZE_MAX_STRING_CHARS = 2000
_SANITIZE_MAX_LIST_ITEMS = 100
_SANITIZE_MAX_DICT_ITEMS = 100


def _sanitize(obj: Any, _depth: int = 0) -> Any:
    """Make *obj* JSON-serialisable (Pydantic models, AIMessage, etc.)."""
    # 超过最大递归深度直接截断，防止循环引用/超深结构导致栈溢出
    if _depth > _SANITIZE_MAX_DEPTH:
        return "<truncated>"
    # 基础标量类型直接透传
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        # 超长字符串截断并标注原始长度，避免日志爆炸
        if len(obj) > _SANITIZE_MAX_STRING_CHARS:
            return (
                obj[:_SANITIZE_MAX_STRING_CHARS].rstrip() + f"\n[truncated from {len(obj)} chars]"
            )
        return obj
    if isinstance(obj, dict):
        # 字典逐键递归清洗，键数超限时记录被截断的键数
        sanitized: dict[Any, Any] = {}
        for index, (key, value) in enumerate(obj.items()):
            if index >= _SANITIZE_MAX_DICT_ITEMS:
                sanitized["_truncated_keys"] = len(obj) - _SANITIZE_MAX_DICT_ITEMS
                break
            sanitized[key] = _sanitize(value, _depth + 1)
        return sanitized
    if isinstance(obj, (list, tuple)):
        # 列表/元组逐项递归，超出上限的元素以计数占位
        sanitized_items = [_sanitize(v, _depth + 1) for v in obj[:_SANITIZE_MAX_LIST_ITEMS]]
        omitted = len(obj) - _SANITIZE_MAX_LIST_ITEMS
        if omitted > 0:
            sanitized_items.append({"_truncated_items": omitted})
        return sanitized_items
    # Pydantic BaseModel → dict then recurse
    # Pydantic 模型先转 dict 再递归清洗
    if hasattr(obj, "model_dump"):
        return _sanitize(obj.model_dump(), _depth + 1)
    # Avoid infinite recursion on Mock / unittest objects
    # Mock 对象访问任意属性都会返回新 Mock，会无限递归，故直接输出类型名
    type_name = type(obj).__name__
    if "Mock" in type_name:
        return f"<{type_name}>"
    # 普通对象退化为其 __dict__ 递归清洗，失败则退化为 str
    if hasattr(obj, "__dict__"):
        try:
            return _sanitize(vars(obj), _depth + 1)
        except Exception:
            return str(obj)
    return str(obj)


async def debug_log_event(event: Any, context: Mapping[str, Any] | None = None) -> None:
    """Dump the complete raw *event* to the debug JSONL log.

    Every field of the LangChain stream event is preserved so nothing is
    lost.  Non-serialisable objects (Pydantic models, ``AIMessage``, etc.)
    are converted via ``model_dump()`` / ``vars()`` fallback.

    File writes are offloaded to a thread to avoid blocking the event loop.
    Failures are silently swallowed so that debug logging can never crash
    production agent execution.
    """
    # 未开启调试直接返回，零开销
    if not _is_enabled():
        return

    try:
        # 文件写入是阻塞 IO，放到线程池执行并设 1s 超时，避免拖慢事件循环
        if context:
            await run_blocking_io(_write_event_sync, event, dict(context), timeout=1.0)
        else:
            await run_blocking_io(_write_event_sync, event, timeout=1.0)
    except Exception:
        # Debug logging is non-critical — must never kill the agent stream.
        # 调试日志属非关键路径，任何异常都必须吞掉，绝不影响 agent 流
        pass


def _write_event_sync(event: Any, context: Mapping[str, Any] | None = None) -> None:
    """Sanitize, serialize, and write a debug event synchronously."""
    # 每条记录带毫秒级时间戳前缀，便于对齐事件时序
    record: dict[str, Any] = {
        "_ts": time.strftime("%H:%M:%S.") + f"{time.time() % 1:.3f}"[2:],
    }
    # 附带 LambChat 的 trace/run/session 上下文（若有）
    if context:
        record["_context"] = _sanitize(dict(context))
    # 合并清洗后的事件字段，序列化为一行 JSON（JSONL 格式）
    record.update(_sanitize(event))
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    log_file = _get_log_file()
    _write_line(log_file, line)


def _write_line(log_file: Any, line: str) -> None:
    """Synchronous file write."""
    try:
        # 写入后立即 flush，保证崩溃时也能看到最新事件
        log_file.write(line)
        log_file.flush()
    except (ValueError, OSError):
        pass  # File closed or invalid — silently skip

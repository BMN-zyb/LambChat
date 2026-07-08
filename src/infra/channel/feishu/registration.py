"""Feishu one-click app registration sessions.

The lark-oapi ``register_app`` helper is synchronous and blocks while the user
scans and approves a QR code. This module wraps it in a short-lived background
thread so the API can expose a pollable registration session.
"""

from __future__ import annotations

import inspect
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import redis

from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)

# 进行中会话的存活时长；已完成（成功/失败/过期/取消）会话保留较短时间供前端拉取结果。
_SESSION_TTL_SECONDS = 15 * 60
_COMPLETED_SESSION_TTL_SECONDS = 2 * 60
# 跨实例共享会话快照的 Redis 键前缀；分布式取消的轮询间隔。
_SHARED_KEY_PREFIX = "feishu:registration:"
_CANCEL_POLL_SECONDS = 1.0
# 进程内会话表及其锁（本进程发起的注册会话缓存在这里）。
_sessions_lock = threading.Lock()
_sessions: dict[str, "FeishuRegistrationSession"] = {}


@dataclass
class FeishuRegistrationSession:
    # 一次"扫码注册飞书应用"会话的完整状态。
    # cancel_event 是跨线程取消信号；app_id/app_secret 在注册成功后填充。
    id: str
    status: str = "pending"
    qr_url: str | None = None
    expire_in: int | None = None
    app_id: str | None = None
    app_secret: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        # 转为对外响应字典；默认不含 app_secret，仅在明确要求时才带上。
        payload = {
            "session_id": self.id,
            "status": self.status,
            "qr_url": self.qr_url,
            "expire_in": self.expire_in,
            "app_id": self.app_id,
            "error": self.error,
        }
        if include_secret:
            payload["app_secret"] = self.app_secret
        return payload

    def to_snapshot(self) -> dict[str, Any]:
        # 生成用于写入 Redis 的完整快照（含 secret 与时间戳），供跨实例共享。
        payload = self.to_dict(include_secret=True)
        payload["created_at"] = self.created_at
        payload["updated_at"] = self.updated_at
        return payload

    # 刷新更新时间戳。
    def touch(self) -> None:
        self.updated_at = time.time()


class _SharedRegistrationStore:
    # 跨实例共享的注册会话存储（同步 Redis 客户端）：
    # 注册请求可能落在任一实例，通过 Redis 快照让其它实例也能查询/取消同一会话。
    def __init__(self) -> None:
        self._client = redis.Redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
            retry_on_timeout=True,
            password=settings.REDIS_PASSWORD or None,
        )

    def _key(self, session_id: str) -> str:
        # 拼接会话在 Redis 中的键。
        return f"{_SHARED_KEY_PREFIX}{session_id}"

    def save(self, session: FeishuRegistrationSession) -> None:
        # 保存会话快照，并按"是否已完成"选择不同 TTL。
        ttl = (
            _COMPLETED_SESSION_TTL_SECONDS
            if session.status in {"success", "error", "expired", "cancelled"}
            else _SESSION_TTL_SECONDS
        )
        self._client.set(
            self._key(session.id),
            json.dumps(session.to_snapshot()),
            ex=ttl,
        )

    def get(self, session_id: str) -> dict[str, Any] | None:
        # 读取会话快照。本存储要求同步客户端；若误用异步客户端会返回 awaitable，
        # 此处显式检测并拒绝，避免静默出错。
        raw = self._client.get(self._key(session_id))
        if inspect.isawaitable(raw):
            logger.warning(
                "[Feishu] async Redis client is not supported by shared registration store"
            )
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[Feishu] invalid registration snapshot in Redis: %s", session_id)
            return None

    def mark_cancelled(self, session_id: str) -> bool:
        # 将共享快照标记为已取消（供发起会话的其它实例感知并停止注册）。
        snapshot = self.get(session_id)
        if snapshot is None:
            return False
        snapshot["status"] = "cancelled"
        snapshot["updated_at"] = time.time()
        self._client.set(
            self._key(session_id),
            json.dumps(snapshot),
            ex=_COMPLETED_SESSION_TTL_SECONDS,
        )
        return True

    def close(self) -> None:
        # 关闭底层 Redis 连接。
        self._client.close()


# 用 lru_cache 把共享存储做成惰性单例（首次调用才建连接）。
@lru_cache
def _get_shared_store() -> _SharedRegistrationStore:
    return _SharedRegistrationStore()


def close_registration_sessions() -> None:
    """Cancel local registration sessions and release the cached Redis store."""
    # 取消本进程所有未完成会话，并释放缓存的共享 Redis 存储（进程停机清理）。
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.cancel_event.set()
        if session.status not in {"success", "error", "expired", "cancelled"}:
            session.status = "cancelled"
            session.touch()

    # 未曾创建过共享存储则无需清理。
    if _get_shared_store.cache_info().currsize == 0:
        return

    try:
        _get_shared_store().close()
    except Exception as e:
        logger.debug("[Feishu] failed to close registration shared store: %s", e)
    finally:
        _get_shared_store.cache_clear()


def _save_shared_session(session: FeishuRegistrationSession) -> None:
    # 写共享快照的容错封装：失败仅记 debug，不影响本地会话推进。
    try:
        _get_shared_store().save(session)
    except Exception as e:
        logger.debug("[Feishu] failed to save registration snapshot: %s", e)


def _session_from_snapshot(snapshot: dict[str, Any]) -> FeishuRegistrationSession:
    # 从共享快照重建一个本地会话对象（用于其它实例查询时构造返回值）。
    return FeishuRegistrationSession(
        id=str(snapshot.get("session_id") or snapshot.get("id") or ""),
        status=str(snapshot.get("status") or "pending"),
        qr_url=snapshot.get("qr_url"),
        expire_in=snapshot.get("expire_in"),
        app_id=snapshot.get("app_id"),
        app_secret=snapshot.get("app_secret"),
        error=snapshot.get("error"),
        created_at=float(snapshot.get("created_at") or time.time()),
        updated_at=float(snapshot.get("updated_at") or time.time()),
        cancel_event=threading.Event(),
    )


def _cleanup_sessions() -> None:
    # 清理进程内过期会话：超过存活期，或已完成且超过完成保留期的都移除。
    now = time.time()
    with _sessions_lock:
        expired = [
            sid
            for sid, session in _sessions.items()
            if now - session.created_at > _SESSION_TTL_SECONDS
            or session.status in {"success", "error", "expired", "cancelled"}
            and now - session.updated_at > _COMPLETED_SESSION_TTL_SECONDS
        ]
        for sid in expired:
            _sessions.pop(sid, None)


def _watch_distributed_cancel(session: FeishuRegistrationSession) -> None:
    """Mirror cross-instance cancel requests into this process' cancel_event."""
    # 分布式取消监视线程：注册在实例 A 上跑，但取消请求可能打到实例 B。
    # 此循环轮询共享快照，一旦发现被别处标记为 cancelled，就置本地 cancel_event，
    # 从而让阻塞中的 register_app 提前退出。
    while not session.cancel_event.wait(_CANCEL_POLL_SECONDS):
        if session.status in {"success", "error", "expired", "cancelled"}:
            return
        try:
            snapshot = _get_shared_store().get(session.id)
        except Exception as e:
            logger.debug("[Feishu] failed to poll registration cancel state: %s", e)
            continue
        if snapshot and snapshot.get("status") == "cancelled":
            session.cancel_event.set()
            session.status = "cancelled"
            session.touch()
            return


def start_registration(source: str = "lambchat") -> FeishuRegistrationSession:
    """Start a Feishu registration session in a background thread."""
    # 先清理过期会话，再创建新会话并登记（本地 + 共享快照）。
    _cleanup_sessions()
    session = FeishuRegistrationSession(id=uuid.uuid4().hex)
    with _sessions_lock:
        _sessions[session.id] = session
    _save_shared_session(session)

    def _run() -> None:
        # 后台线程体：lark.register_app 是同步阻塞调用（等待用户扫码授权），
        # 因此必须放到独立线程里跑，通过回调把二维码/状态回写到会话。
        try:
            import lark_oapi as lark

            def _on_qr(info: dict[str, Any]) -> None:
                # 收到二维码：记录 URL/过期时间并标记为 qr_ready。
                session.qr_url = info.get("url")
                session.expire_in = info.get("expire_in")
                session.status = "qr_ready"
                session.touch()
                _save_shared_session(session)

            def _on_status(info: dict[str, Any]) -> None:
                # 状态变更回调：忽略中间的 polling，其余状态同步到会话。
                status = info.get("status")
                if status and status != "polling":
                    session.status = str(status)
                    session.touch()
                    _save_shared_session(session)

            # 阻塞直到用户完成授权或取消/超时；cancel_event 用于中途打断。
            result = lark.register_app(
                on_qr_code=_on_qr,
                on_status_change=_on_status,
                source=source,
                cancel_event=session.cancel_event,
            )
            # 兼容不同返回字段命名，取出应用凭据。
            session.app_id = result.get("client_id") or result.get("app_id")
            session.app_secret = result.get("client_secret") or result.get("app_secret")
            if session.app_id and session.app_secret:
                session.status = "success"
            else:
                session.status = "error"
                session.error = "Feishu registration result did not include app credentials"
        except Exception as e:
            # 依据异常/取消信号区分终态：主动取消、二维码过期、或其它错误。
            if session.cancel_event.is_set():
                session.status = "cancelled"
            elif "Expired" in e.__class__.__name__:
                session.status = "expired"
                session.error = "QR code expired"
            else:
                session.status = "error"
                session.error = str(e)
            logger.warning("[Feishu] one-click registration failed: %s", e)
        finally:
            session.touch()
            _save_shared_session(session)

    # 先启动分布式取消监视线程，再启动实际注册线程；两者都是守护线程。
    watcher = threading.Thread(
        target=_watch_distributed_cancel,
        args=(session,),
        daemon=True,
        name=f"feishu-register-cancel-{session.id[:8]}",
    )
    watcher.start()
    thread = threading.Thread(target=_run, daemon=True, name=f"feishu-register-{session.id[:8]}")
    thread.start()
    return session


def get_registration(session_id: str) -> FeishuRegistrationSession | None:
    # 查询会话：优先本地，本地没有再回退到共享快照（跨实例查询）。
    _cleanup_sessions()
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session:
        return session

    try:
        snapshot = _get_shared_store().get(session_id)
    except Exception as e:
        logger.debug("[Feishu] failed to read registration snapshot: %s", e)
        return None
    if not snapshot:
        return None
    return _session_from_snapshot(snapshot)


def cancel_registration(session_id: str) -> bool:
    # 取消会话：本地有则直接置取消信号与状态；否则通过共享快照标记，
    # 让持有该会话的实例经由监视线程感知并停止。
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session:
        session.cancel_event.set()
        session.status = "cancelled"
        session.touch()
        _save_shared_session(session)
        return True
    try:
        return _get_shared_store().mark_cancelled(session_id)
    except Exception as e:
        logger.debug("[Feishu] failed to cancel registration snapshot: %s", e)
        return False

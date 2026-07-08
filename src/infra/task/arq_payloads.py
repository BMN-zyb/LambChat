"""arq 任务载荷（payload）的 Redis 持久化存储。

当 TASK_BACKEND=arq 时，任务不在提交进程内直接跑，而是通过 arq 分发到独立的
worker 进程执行。函数引用无法跨进程序列化，因此提交端先把「重建任务所需的可
序列化上下文」（session_id、agent_id、message、各类开关等）以 JSON 存到 Redis，
再向 arq 队列 enqueue 一个只带 run_id 的轻量 job；worker 收到 job 后凭 run_id
回读这份 payload 来还原并执行任务。这是 arq 分发相较本地分发的关键差异。
"""

from __future__ import annotations

import json
from typing import Any

from src.infra.async_utils import run_blocking_io
from src.infra.storage.redis import get_redis_client

# payload 默认存活 24 小时：留足 worker 消费和失败重试的时间窗口。
DEFAULT_TASK_ARQ_PAYLOAD_TTL_SECONDS = 60 * 60 * 24
# Redis key 前缀，完整 key 为 TASK_ARQ_PAYLOAD_PREFIX + run_id。
TASK_ARQ_PAYLOAD_PREFIX = "task:arq:payload:"
# 单个 payload 的体积上限（2MB），超限直接拒绝，避免超大上下文压垮 Redis。
TASK_ARQ_PAYLOAD_MAX_BYTES = 2 * 1024 * 1024


class TaskArqPayloadStore:
    """Persist serializable task context for arq workers."""

    # redis 可注入（便于测试），ttl_seconds 缺省用模块默认值。
    def __init__(self, redis: Any | None = None, ttl_seconds: int | None = None) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds or DEFAULT_TASK_ARQ_PAYLOAD_TTL_SECONDS

    # 惰性获取 Redis 客户端：首次访问才真正建立连接。
    @property
    def redis(self) -> Any:
        if self._redis is None:
            self._redis = get_redis_client()
        return self._redis

    # 保存任务上下文：JSON 序列化放到线程池执行（避免阻塞事件循环），做体积
    # 校验后带 TTL 写入 Redis。ensure_ascii=False 以保留中文等原文减小体积。
    async def save(self, run_id: str, payload: dict[str, Any]) -> None:
        encoded = await run_blocking_io(json.dumps, payload, ensure_ascii=False)
        size = len(encoded.encode("utf-8"))
        if size > TASK_ARQ_PAYLOAD_MAX_BYTES:
            raise ValueError(
                f"task payload too large: {size} bytes (max {TASK_ARQ_PAYLOAD_MAX_BYTES})"
            )
        await self.redis.set(
            self._key(run_id),
            encoded,
            ex=self._ttl_seconds,
        )

    # 读回任务上下文：不存在返回 None；兼容 bytes/str 两种返回类型后反序列化。
    async def load(self, run_id: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self._key(run_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return await run_blocking_io(json.loads, raw)

    # 删除任务上下文：任务成功/被取消/无法恢复时清理，返回是否确实删除了 key。
    async def delete(self, run_id: str) -> bool:
        return bool(await self.redis.delete(self._key(run_id)))

    # 由 run_id 拼出完整 Redis key。
    @staticmethod
    def _key(run_id: str) -> str:
        return f"{TASK_ARQ_PAYLOAD_PREFIX}{run_id}"

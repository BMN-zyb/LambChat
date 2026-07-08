"""Process memory monitoring and diagnostics."""

from __future__ import annotations

import asyncio
import gc
import os
import tracemalloc
from collections import Counter, deque
from datetime import datetime
from typing import Any

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings

try:
    import psutil
except ImportError:  # pragma: no cover - dependency is expected to be installed
    psutil = None

logger = get_logger(__name__)

# 单次"重诊断"采样（tracemalloc 快照/对比、gc 对象遍历等）允许的最长阻塞时间；
# 超时后不让告警流程失败，而是用占位快照代替，见 _build_timeout_diagnostics_snapshot
_BLOCKING_SAMPLE_TIMEOUT_SECONDS = 10.0


# 把 tracemalloc 的 Frame 格式化为 "文件路径:行号" 形式的位置标识，用于展示 Top 内存分配点
def _format_trace_location(trace: tracemalloc.Frame) -> str:
    return f"{trace.filename}:{trace.lineno}"


def _format_bytes_as_mb(value: int) -> str:
    return f"{round(value / 1024 / 1024, 2)}MB"


class MemoryMonitor:
    """Background process memory sampler with on-demand diagnostics."""

    # 分为两档诊断能力：
    # 1) 轻量采样（始终开启，只要 psutil 可用）：定期记录 RSS/VMS/线程数/文件描述符数，
    #    开销很小，用于判断是否存在"持续增长"的疑似内存泄漏；
    # 2) 重诊断（由 heavy_diagnostics_enabled 控制，默认仅 DEBUG 或显式开启）：
    #    用 tracemalloc 做增长对比/Top 分配点采样，用 gc.get_objects() 做对象类型计数，
    #    这些操作本身有明显的 CPU/内存开销，因此只在真正怀疑泄漏时才触发，而不是每次采样都跑。
    def __init__(
        self,
        *,
        interval_seconds: float | None = None,
        history_limit: int | None = None,
        leak_threshold_bytes: int | None = None,
        min_samples_for_alert: int | None = None,
        alert_cooldown_seconds: float | None = None,
        traceback_limit: int | None = None,
        top_stats_limit: int | None = None,
        gc_object_limit: int | None = None,
        heavy_diagnostics_enabled: bool | None = None,
    ) -> None:
        # 以下每个参数都是"显式传入优先，否则读全局配置"，方便测试/特定场景覆盖默认值
        self.interval_seconds = (
            interval_seconds
            if interval_seconds is not None
            else settings.MEMORY_MONITOR_INTERVAL_SECONDS
        )
        self.history_limit = (
            history_limit if history_limit is not None else settings.MEMORY_MONITOR_HISTORY_LIMIT
        )
        self.leak_threshold_bytes = (
            leak_threshold_bytes
            if leak_threshold_bytes is not None
            else settings.MEMORY_MONITOR_LEAK_THRESHOLD_MB * 1024 * 1024
        )
        self.min_samples_for_alert = (
            min_samples_for_alert
            if min_samples_for_alert is not None
            else settings.MEMORY_MONITOR_MIN_SAMPLES
        )
        self.alert_cooldown_seconds = (
            alert_cooldown_seconds
            if alert_cooldown_seconds is not None
            else settings.MEMORY_MONITOR_ALERT_COOLDOWN_SECONDS
        )
        self.traceback_limit = (
            traceback_limit
            if traceback_limit is not None
            else settings.MEMORY_MONITOR_TRACEBACK_LIMIT
        )
        self.top_stats_limit = (
            top_stats_limit
            if top_stats_limit is not None
            else settings.MEMORY_MONITOR_TOP_STATS_LIMIT
        )
        self.gc_object_limit = (
            gc_object_limit
            if gc_object_limit is not None
            else settings.MEMORY_MONITOR_GC_OBJECT_LIMIT
        )
        self.heavy_diagnostics_enabled = (
            heavy_diagnostics_enabled
            if heavy_diagnostics_enabled is not None
            else settings.MEMORY_MONITOR_HEAVY_DIAGNOSTICS or settings.DEBUG
        )

        # _history: 最近若干次轻量采样组成的滑动窗口，用于判断增长趋势（超出 history_limit 自动丢弃最旧的）
        self._history: deque[dict[str, Any]] = deque(maxlen=max(1, self.history_limit))
        self._running = False
        self._task: asyncio.Task[None] | None = None
        # 保护 _history/_baseline_snapshot/_last_alert 等可变状态，避免采样任务与外部只读查询并发冲突
        self._state_lock = asyncio.Lock()
        self._process = psutil.Process(os.getpid()) if psutil is not None else None
        # _baseline_snapshot: tracemalloc 的"基线"快照，后续增长对比都是与它做 diff
        self._baseline_snapshot: tracemalloc.Snapshot | None = None
        self._baseline_reset_at: datetime | None = None
        # 标记 tracemalloc 是否是本实例负责启动的，stop() 时只关闭自己启动的，避免误关其他地方开启的 tracing
        self._started_tracemalloc = False
        self._last_alert: dict[str, Any] | None = None
        self._last_alert_at: datetime | None = None
        self._last_error: str | None = None

    async def _run_monitor_blocking(
        self, func, *, timeout: float = _BLOCKING_SAMPLE_TIMEOUT_SECONDS
    ):
        # 把可能阻塞的同步采集函数（psutil 调用、tracemalloc 快照/对比、gc.get_objects 遍历等）
        # 丢到线程池执行，并设置超时兜底，防止其中任意一步耗时过长时拖住事件循环或整个监控任务
        return await run_blocking_io(func, timeout=timeout)

    async def start(self) -> None:
        """Start the background monitor if enabled."""
        # 已在运行，或功能被全局配置关闭时，直接跳过
        if self._running or not settings.MEMORY_MONITOR_ENABLED:
            return
        self._running = True

        if psutil is None:
            self._last_error = "psutil is not installed"
            logger.warning("[MemoryMonitor] psutil is unavailable; monitoring disabled")
            return

        # 仅在需要重诊断且当前未开启 tracemalloc 时才启动它，并记录"是本实例启动的"，
        # 避免与其他已经在跑的 tracemalloc 使用者互相干扰
        if self.heavy_diagnostics_enabled and not tracemalloc.is_tracing():
            tracemalloc.start(self.traceback_limit)
            self._started_tracemalloc = True

        self._task = asyncio.create_task(self._run_loop())
        # 主动"消费"一次任务异常，避免后台任务出错但无人 await 时触发
        # asyncio 的 "Task exception was never retrieved" 警告；已取消的任务不能调用 exception()，需跳过
        self._task.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        logger.info(
            "[MemoryMonitor] started interval=%ss threshold=%sMB",
            self.interval_seconds,
            round(self.leak_threshold_bytes / 1024 / 1024, 2),
        )

    async def stop(self) -> None:
        """Stop the background monitor."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # 只关闭本实例自己启动的 tracemalloc，避免影响其他代码路径可能依赖的 tracing 状态
        if self._started_tracemalloc and tracemalloc.is_tracing():
            tracemalloc.stop()
            self._started_tracemalloc = False

    async def _run_loop(self) -> None:
        # 首次运行（历史为空）时先建立一次基线，把当前内存状态当作后续增长判断的起点
        if not self._history:
            try:
                await self.reset_baseline()
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning(
                    "[MemoryMonitor] initial baseline capture failed: %s",
                    exc,
                    exc_info=True,
                )

        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self._sample_once()
            except asyncio.CancelledError:
                # 任务被取消是正常的停止路径，直接跳出循环结束任务
                break
            except Exception as exc:  # pragma: no cover - defensive logging path
                # 单次采样失败不应终止整个后台循环，记录错误后继续下一轮
                self._last_error = str(exc)
                logger.warning("[MemoryMonitor] sampling failed: %s", exc, exc_info=True)

    async def _sample_once(self) -> None:
        distributed_snapshot: dict[str, Any] | None = None
        # 用 _state_lock 保护"采样 -> 追加历史 -> 判断疑似增长 -> 可能采集重诊断"这一整段过程，
        # 避免与 get_summary/get_diagnostics 等只读查询并发时读到不一致的中间状态
        async with self._state_lock:
            sample = await self._run_monitor_blocking(self._collect_process_sample)
            sample.setdefault("timestamp", utc_now())
            self._history.append(sample)

            if self._is_suspicious_growth(sample["timestamp"]):
                now = sample["timestamp"]
                # 告警冷却检查通过才真正采集重诊断/发日志，避免持续增长时反复触发重诊断
                if self._should_emit_alert(now):
                    self._last_alert = None
                    if self.heavy_diagnostics_enabled:
                        try:
                            self._last_alert = await self._run_monitor_blocking(
                                self._capture_diagnostics_snapshot
                            )
                        except asyncio.TimeoutError:
                            # 重诊断耗时超过 _BLOCKING_SAMPLE_TIMEOUT_SECONDS：
                            # 不让整条告警流程失败，改用一个标注"超时"的占位快照
                            self._last_error = "heavy diagnostics timed out"
                            self._last_alert = self._build_timeout_diagnostics_snapshot(
                                captured_at=now
                            )
                            logger.warning(
                                "[MemoryMonitor] heavy diagnostics timed out after %ss",
                                _BLOCKING_SAMPLE_TIMEOUT_SECONDS,
                            )
                    self._last_alert_at = now
                    # 拼接告警日志：基础的 rss/growth 信息始终有，三类明细摘要按是否采集到而追加
                    alert_parts = [
                        f"rss={_format_bytes_as_mb(sample['rss_bytes'])}",
                        f"growth={_format_bytes_as_mb(self._growth_bytes())}",
                    ]
                    growth_summary = self._format_growth_summary(self._last_alert)
                    if growth_summary is not None:
                        alert_parts.append(f"top_growth={growth_summary}")
                    allocation_summary = self._format_allocation_summary(self._last_alert)
                    if allocation_summary is not None:
                        alert_parts.append(f"top_allocations={allocation_summary}")
                    object_summary = self._format_object_summary(self._last_alert)
                    if object_summary is not None:
                        alert_parts.append(f"top_objects={object_summary}")
                    logger.warning(
                        "[MemoryMonitor] suspicious memory growth detected %s",
                        " ".join(alert_parts),
                    )
            distributed_snapshot = await self._build_distributed_snapshot_locked()

        # 发布到 Redis 的网络 IO 放在锁外执行，避免把网络等待时间也算进锁的持有时长
        await self._publish_distributed_snapshot(distributed_snapshot)

    # 采集一次轻量指标：常驻内存(RSS)、虚拟内存(VMS)、线程数、已打开的文件描述符数。
    # num_fds() 只在类 Unix 系统上存在；不支持时退化为统计 open_files() 列表长度（较慢但更通用）。
    def _collect_process_sample(self) -> dict[str, Any]:
        if self._process is None:
            raise RuntimeError("psutil process is unavailable")

        memory = self._process.memory_info()
        try:
            open_file_count = self._process.num_fds()  # type: ignore[attr-defined]
        except (AttributeError, NotImplementedError):
            open_file_count = len(self._process.open_files())

        return {
            "timestamp": utc_now(),
            "rss_bytes": int(memory.rss),
            "vms_bytes": int(memory.vms),
            "thread_count": int(self._process.num_threads()),
            "open_file_count": int(open_file_count),
        }

    def _capture_diagnostics_snapshot(self) -> dict[str, Any]:
        # 一次性采集三类"重"诊断数据：tracemalloc 增长对比、当前分配 Top N、gc 对象类型计数。
        # 本方法预期通过 _run_monitor_blocking 丢到线程池执行，因为三者都可能耗时较长。
        return {
            "captured_at": utc_now().isoformat(),
            "heavy_diagnostics_enabled": self.heavy_diagnostics_enabled,
            "top_growth": self._build_growth_stats(),
            "top_allocations": self._build_allocation_stats(),
            "top_object_types": self._build_object_type_stats(),
        }

    async def reset_baseline(self) -> None:
        """Re-anchor growth tracking to the current process state."""
        # 用途：把当前进程状态重新设为"基线"——清空历史（只保留这一条最新样本）、
        # 重置 tracemalloc 基线快照、清空上一次告警状态。
        # 典型场景：启动阶段一次性的内存上涨不应被后续持续判定为"疑似泄漏"，
        # 调用者可以在确认稳定后手动/首次重置基线。
        distributed_snapshot: dict[str, Any] | None = None
        async with self._state_lock:
            sample = await self._run_monitor_blocking(self._collect_process_sample)
            sample.setdefault("timestamp", utc_now())

            if tracemalloc.is_tracing():
                try:
                    self._baseline_snapshot = await self._run_monitor_blocking(
                        tracemalloc.take_snapshot
                    )
                except RuntimeError:
                    self._baseline_snapshot = None
            else:
                self._baseline_snapshot = None

            self._baseline_reset_at = sample["timestamp"]
            self._history = deque([sample], maxlen=max(1, self.history_limit))
            self._last_alert = None
            self._last_alert_at = None
            distributed_snapshot = await self._build_distributed_snapshot_locked()

        # 同样把网络发布放到锁外，减少持锁时间
        await self._publish_distributed_snapshot(distributed_snapshot)

    # 以下三个 _format_*_summary 方法把诊断快照里的三类明细各取前 3 条，
    # 格式化为写入告警日志的一行摘要文本（不返回结构化数据，仅用于日志展示）
    def _format_growth_summary(self, diagnostics: dict[str, Any] | None) -> str | None:
        if not diagnostics:
            return None
        rows = diagnostics.get("top_growth") or []
        if not rows:
            return None
        return ", ".join(
            f"{row['location']} (+{_format_bytes_as_mb(int(row['size_diff_bytes']))})"
            for row in rows[:3]
        )

    def _format_allocation_summary(self, diagnostics: dict[str, Any] | None) -> str | None:
        if not diagnostics:
            return None
        rows = diagnostics.get("top_allocations") or []
        if not rows:
            return None
        return ", ".join(
            f"{row['location']} ({_format_bytes_as_mb(int(row['size_bytes']))})" for row in rows[:3]
        )

    def _format_object_summary(self, diagnostics: dict[str, Any] | None) -> str | None:
        if not diagnostics:
            return None
        rows = diagnostics.get("top_object_types") or []
        if not rows:
            return None
        return ", ".join(f"{row['type']}={int(row['count'])}" for row in rows[:3])

    # 用当前 tracemalloc 快照与最初记录的 baseline 快照做 compare_to("lineno")，
    # 得到"按代码行"聚合的内存增长/减少对比；只保留 size_diff > 0（真正在增长的位置），
    # 并按 top_stats_limit 截断，避免返回结果集过大
    def _build_growth_stats(self) -> list[dict[str, Any]]:
        if self._baseline_snapshot is None or not tracemalloc.is_tracing():
            return []

        snapshot = tracemalloc.take_snapshot()
        growth_stats = snapshot.compare_to(self._baseline_snapshot, "lineno")
        rows: list[dict[str, Any]] = []

        for stat in growth_stats:
            if stat.size_diff <= 0:
                continue
            rows.append(
                {
                    "location": _format_trace_location(stat.traceback[0]),
                    "size_diff_bytes": int(stat.size_diff),
                    "count_diff": int(stat.count_diff),
                }
            )
            if len(rows) >= self.top_stats_limit:
                break

        return rows

    # 不与基线比较，只看当前时刻各代码行的绝对内存占用 Top N，
    # 用于定位"现在占用最多的是谁"而不仅是"谁在持续增长"
    def _build_allocation_stats(self) -> list[dict[str, Any]]:
        if not tracemalloc.is_tracing():
            return []

        snapshot = tracemalloc.take_snapshot()
        rows: list[dict[str, Any]] = []

        for stat in snapshot.statistics("lineno")[: self.top_stats_limit]:
            rows.append(
                {
                    "location": _format_trace_location(stat.traceback[0]),
                    "size_bytes": int(stat.size),
                    "count": int(stat.count),
                }
            )

        return rows

    # 用 gc.get_objects() 遍历当前所有被 GC 追踪的对象并按类型名计数，取数量最多的 Top N 类型。
    # 这能帮助判断是否有某类对象（自定义类实例、dict、list 等）在不断堆积；
    # 遍历全部对象本身开销不小，这也是它被归入"重诊断"、需要显式开启的原因之一。
    def _build_object_type_stats(self) -> list[dict[str, Any]]:
        counts = Counter(type(obj).__name__ for obj in gc.get_objects())
        return [
            {"type": object_type, "count": count}
            for object_type, count in counts.most_common(self.gc_object_limit)
        ]

    # 用历史队列里最新一条与最早一条的 RSS 差值，近似"这段观测窗口内的内存增长量"
    def _growth_bytes(self) -> int:
        if len(self._history) < 2:
            return 0
        return int(self._history[-1]["rss_bytes"] - self._history[0]["rss_bytes"])

    # 判定是否"疑似内存泄漏"，需要同时满足三个条件：
    #   1) 样本数达到 min_samples_for_alert（数据量太少容易误判）
    #   2) 累计增长量（growth_bytes）达到 leak_threshold_bytes 阈值
    #   3) RSS 在整个历史窗口内单调不降（每一个样本都 >= 前一个样本）
    # 第 3 点是为了区分"持续稳定增长的疑似泄漏"与"短暂尖峰后又回落的正常波动"，
    # 避免因为一次性的临时高峰而误报。
    def _is_suspicious_growth(self, sampled_at: datetime) -> bool:
        del sampled_at
        if len(self._history) < self.min_samples_for_alert:
            return False

        growth_bytes = self._growth_bytes()
        if growth_bytes < self.leak_threshold_bytes:
            return False

        rss_series = [sample["rss_bytes"] for sample in self._history]
        return all(current >= previous for previous, current in zip(rss_series, rss_series[1:]))

    # 告警冷却：距离上一次告警不足 alert_cooldown_seconds 则不重复告警，避免日志刷屏
    def _should_emit_alert(self, now: datetime) -> bool:
        if self._last_alert_at is None:
            return True
        elapsed = (now - self._last_alert_at).total_seconds()
        return elapsed >= self.alert_cooldown_seconds

    # 方法名后缀 _locked 表示：调用前必须已经持有 self._state_lock。
    # 依次检查若干"不可用"前提（功能关闭/psutil 缺失/尚无样本），命中任一条就提前返回精简状态；
    # 否则汇总最新一次采样并计算增长量/是否疑似泄漏，顺带查询 checkpointer 的运行诊断
    # （checkpointer 诊断失败不应影响内存监控本身，因此单独用 try/except 兜底）
    def _get_summary_locked(self) -> dict[str, Any]:
        if not settings.MEMORY_MONITOR_ENABLED:
            return {"available": False, "reason": "disabled"}

        if psutil is None:
            return {
                "available": False,
                "reason": "psutil_unavailable",
                "last_error": self._last_error,
            }

        if not self._history:
            return {"available": False, "reason": "no_samples", "last_error": self._last_error}

        latest = self._history[-1]
        try:
            from src.infra.storage.checkpoint import get_checkpointer_diagnostics

            checkpointer = get_checkpointer_diagnostics()
        except Exception as exc:
            checkpointer = {"available": False, "last_error": str(exc)}
        return {
            "available": True,
            "rss_bytes": latest["rss_bytes"],
            "vms_bytes": latest["vms_bytes"],
            "thread_count": latest["thread_count"],
            "open_file_count": latest["open_file_count"],
            "history_size": len(self._history),
            "growth_bytes": self._growth_bytes(),
            "suspected_leak": self._is_suspicious_growth(latest["timestamp"]),
            "heavy_diagnostics_enabled": self.heavy_diagnostics_enabled,
            "tracemalloc_tracing": tracemalloc.is_tracing(),
            "sample_interval_seconds": self.interval_seconds,
            "baseline_reset_at": self._baseline_reset_at,
            "last_sample_at": latest["timestamp"],
            "last_error": self._last_error,
            "checkpointer": checkpointer,
        }

    # 以下两个方法分别用于"未开启重诊断"和"重诊断超时"两种场景下的占位快照，
    # 结构与真实诊断快照保持一致（字段齐全但明细列表为空），
    # 这样下游代码无需为这两种特殊情况额外写判空分支
    def _build_disabled_current_snapshot_locked(
        self,
        *,
        captured_at: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "captured_at": (captured_at or utc_now()).isoformat(),
            "heavy_diagnostics_enabled": False,
            "reason": "heavy_diagnostics_disabled",
            "top_growth": [],
            "top_allocations": [],
            "top_object_types": [],
        }

    def _build_timeout_diagnostics_snapshot(
        self,
        *,
        captured_at: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "captured_at": (captured_at or utc_now()).isoformat(),
            "heavy_diagnostics_enabled": True,
            "reason": "heavy_diagnostics_timeout",
            "top_growth": [],
            "top_allocations": [],
            "top_object_types": [],
        }

    # 决定要发布到分布式快照里的"明细"数据：
    # 优先使用刚采集到的 _last_alert（说明本次触发过重诊断）；
    # 否则退化为一个不含明细的占位快照，保证分布式快照结构始终完整
    async def _resolve_snapshot_details_locked(self) -> dict[str, Any] | None:
        if not self._history:
            return None
        if self._last_alert is not None:
            return self._last_alert
        latest = self._history[-1]
        return self._build_disabled_current_snapshot_locked(
            captured_at=latest.get("timestamp") if isinstance(latest, dict) else None
        )

    # 同样要求已持有 _state_lock。组装好 summary + details 后交给
    # distributed_memory_health 模块构建统一格式的实例快照，供跨实例汇总展示使用
    async def _build_distributed_snapshot_locked(self) -> dict[str, Any] | None:
        if not self._history:
            return None

        from src.infra.monitoring.distributed_memory_health import build_instance_snapshot

        summary = self._get_summary_locked()
        details = await self._resolve_snapshot_details_locked()
        captured_at = details.get("captured_at") if isinstance(details, dict) else None
        return build_instance_snapshot(
            captured_at=captured_at,
            summary=summary,
            details=details,
        )

    async def _publish_distributed_snapshot(
        self,
        snapshot: dict[str, Any] | None,
    ) -> None:
        if snapshot is None:
            return

        try:
            from src.infra.monitoring.distributed_memory_health import publish_instance_snapshot

            await publish_instance_snapshot(snapshot, interval_seconds=self.interval_seconds)
        except Exception as exc:
            # 发布失败（例如 Redis 不可用）只记录告警，不能让分布式快照发布的问题
            # 影响本进程自身的内存监控主流程
            logger.warning(
                "[MemoryMonitor] failed to publish distributed snapshot: %s",
                exc,
                exc_info=True,
            )

    # 对外暴露的只读接口：加锁读取一份汇总信息快照
    async def get_summary(self) -> dict[str, Any]:
        async with self._state_lock:
            return self._get_summary_locked()

    # refresh=True 时：若已开启重诊断则立即重新采一次明细；否则返回一个占位快照。
    # refresh=False（默认）时：直接返回上一次告警时采集到的明细（可能为 None，表示从未触发过告警）
    async def get_diagnostics(self, refresh: bool = False) -> dict[str, Any]:
        async with self._state_lock:
            summary = self._get_summary_locked()
            current_snapshot = self._last_alert
            if refresh and self._history and self.heavy_diagnostics_enabled:
                current_snapshot = await self._run_monitor_blocking(
                    self._capture_diagnostics_snapshot
                )
            elif refresh and self._history and not self.heavy_diagnostics_enabled:
                current_snapshot = self._build_disabled_current_snapshot_locked()

        diagnostics = {
            "summary": summary,
            "last_alert": self._last_alert,
            "last_error": self._last_error,
            "current_snapshot": current_snapshot,
        }

        return diagnostics


# 进程级单例：与事件循环/分布式健康检查等模块保持一致的懒创建单例模式
_memory_monitor: MemoryMonitor | None = None


def get_memory_monitor() -> MemoryMonitor:
    global _memory_monitor
    if _memory_monitor is None:
        _memory_monitor = MemoryMonitor()
    return _memory_monitor


async def close_memory_monitor() -> None:
    """Stop and release the singleton memory monitor without creating it."""
    global _memory_monitor
    monitor = _memory_monitor
    # 先取出并清空单例引用，再停止，避免停止过程中其他协程仍拿到即将失效的实例；
    # 若单例从未被创建过（monitor is None）则什么都不做，不会意外触发创建
    _memory_monitor = None
    if monitor is not None:
        await monitor.stop()

"""Unified process-local scheduler built on APScheduler."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.infra.logging import get_logger
from src.infra.utils.datetime import utc_now

logger = get_logger(__name__)

# 以下类型别名统一支持"固定值"或"每次调度时重新求值的可调用对象"两种写法：
# interval/enabled/trigger 都可以是一个固定值，也可以是一个函数，
# 从而在不重启整个调度器的前提下响应配置变化（例如管理员把执行间隔从 60 秒改成 30 秒）。
IntervalValue = int | Callable[[], int]
EnabledValue = bool | Callable[[], bool]
JobHandler = Callable[[], Awaitable[Any]]
TriggerValue: TypeAlias = BaseTrigger | Callable[[], BaseTrigger]


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """A managed scheduled task."""

    # 任务唯一标识，同时作为 APScheduler 内部的 job id
    id: str
    # 触发器：可以是固定的 BaseTrigger 实例，也可以是每次调度时重新计算触发器的函数
    trigger: TriggerValue
    # 实际执行逻辑的异步回调，无参数；返回值除 run_job_now 主动调用外不会被使用
    handler: JobHandler
    # 是否启用：支持固定布尔值，或"每次触发时重新判断"的函数，配合运行时配置开关使用
    enabled: EnabledValue = True
    name: str | None = None
    # APScheduler 允许同一任务同时存在的最大并发实例数，默认 1 表示不允许重叠执行
    max_instances: int = 1
    # 多次错过的触发合并为一次执行（例如进程短暂卡顿后恢复），避免"补跑"导致任务堆积
    coalesce: bool = True
    # 是否在调度器启动时立即执行一次，而不是等到第一个自然触发时间点
    run_on_start: bool = False

    # ── Factory helpers ────────────────────────────

    @classmethod
    def from_interval(
        cls,
        id: str,
        interval_seconds: IntervalValue,
        handler: JobHandler,
        **kwargs: Any,
    ) -> "ScheduledJob":
        """Create a ScheduledJob with an IntervalTrigger (backward compatible)."""
        # 兼容旧版"只支持固定间隔秒数"的调用方式，内部转换为 IntervalTrigger；
        # 若 interval_seconds 是可调用对象，则每次任务跑完后都会重新求值生成新的触发器
        # （具体在 RuntimeScheduler._refresh_trigger_if_needed 中触发）
        if callable(interval_seconds):

            def _make_trigger() -> BaseTrigger:
                return IntervalTrigger(seconds=max(1, int(interval_seconds())))

            return cls(id=id, trigger=_make_trigger, handler=handler, **kwargs)
        return cls(
            id=id,
            trigger=IntervalTrigger(seconds=max(1, int(interval_seconds))),
            handler=handler,
            **kwargs,
        )


class RuntimeScheduler:
    """Small APScheduler facade for LambChat runtime services."""

    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler | None = None
        # _jobs：已注册的任务定义登记表，无论 APScheduler 是否已启动都会保留，
        # 方便 start() 时统一把所有任务加进真正的调度器
        self._jobs: dict[str, ScheduledJob] = {}
        # _scheduled_intervals：记录每个 callable-interval 任务"上一次实际生效的间隔秒数"，
        # 用于判断某次任务跑完后间隔是否发生了变化，从而决定是否需要 reschedule
        self._scheduled_intervals: dict[str, int] = {}

    # ── Public API ─────────────────────────────────

    def register_interval_job(self, job: ScheduledJob) -> None:
        """Register or replace an interval job (backward compatible)."""
        self.register_job(job)

    def register_job(self, job: ScheduledJob) -> None:
        """Register or replace a scheduled job (supports interval and cron)."""
        if not job.id:
            raise ValueError("scheduled job id is required")
        self._jobs[job.id] = job
        logger.info(
            "[Scheduler] registered job %s trigger=%s run_on_start=%s",
            job.id,
            type(self._resolve_trigger(job)).__name__,
            job.run_on_start,
        )
        # 调度器已经在运行时，注册/替换任务要立即同步到 APScheduler；
        # 否则只更新登记表，等 start() 时统一批量添加
        if self._scheduler is not None:
            self._add_or_replace_job(job)

    def unregister_job(self, job_id: str) -> None:
        """Remove a job from the scheduler."""
        self._jobs.pop(job_id, None)
        self._scheduled_intervals.pop(job_id, None)
        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(job_id)
            except JobLookupError:
                # 任务可能从未被真正加入 APScheduler（比如调度器还没 start），找不到就忽略
                pass
        logger.info("[Scheduler] unregistered job %s", job_id)

    def has_job(self, job_id: str) -> bool:
        """Check whether a job is registered."""
        return job_id in self._jobs

    def start(self) -> None:
        """Start APScheduler and add all registered jobs."""
        # 已经在运行则跳过，start() 可以安全地被重复调用
        if self._scheduler is not None and getattr(self._scheduler, "running", False):
            return
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        # 重新构建间隔记录表，因为下面会对每个已注册任务重新调用 _add_or_replace_job
        self._scheduled_intervals.clear()
        for job in self._jobs.values():
            self._add_or_replace_job(job)
        self._scheduler.start()
        logger.info("[Scheduler] started with %d jobs", len(self._jobs))

    async def stop(self) -> None:
        """Stop APScheduler without waiting for long-running jobs."""
        if self._scheduler is None:
            return
        scheduler = self._scheduler
        self._scheduler = None
        self._scheduled_intervals.clear()
        # wait=False：不等待当前正在执行的任务跑完，尽快关闭
        # （应用退出场景不应被一个耗时较长的任务卡住）
        shutdown_result = scheduler.shutdown(wait=False)
        # AsyncIOScheduler.shutdown 在某些版本下可能返回协程，需要按需 await
        if inspect.isawaitable(shutdown_result):
            await shutdown_result
        logger.info("[Scheduler] stopped")

    def clear(self) -> None:
        """Release registered job handlers and interval bookkeeping."""
        # 仅清空本地登记表，不会触碰已经在跑的 APScheduler 实例；调用前应确保已先 stop()
        self._jobs.clear()
        self._scheduled_intervals.clear()

    async def run_job_now(self, job_id: str) -> Any:
        """Run a registered job immediately; mainly useful for tests and admin hooks."""
        # 绕开触发器直接执行一次任务体，常用于测试或管理后台的"立即执行"按钮
        job = self._jobs[job_id]
        return await self._run_job(job)

    # ── Internal ───────────────────────────────────

    # 把一个 ScheduledJob 真正注册到 APScheduler：解析出实际触发器、记录当前间隔
    # （供后续动态刷新时对比），并用 replace_existing=True 保证重复注册同一 id 时是覆盖而非报错。
    def _add_or_replace_job(self, job: ScheduledJob) -> None:
        if self._scheduler is None:
            return
        trigger = self._resolve_trigger(job)
        # Track interval for legacy dynamic-refresh support
        # 只有间隔类触发器才需要记录当前间隔秒数，用于后续判断"下次触发时间隔是否变化了"
        if isinstance(trigger, IntervalTrigger):
            self._scheduled_intervals[job.id] = trigger.interval_length
        self._scheduler.add_job(
            self._make_job_runner(job.id),
            trigger=trigger,
            id=job.id,
            name=job.name or job.id,
            replace_existing=True,
            coalesce=job.coalesce,
            max_instances=job.max_instances,
            # run_on_start 时把 next_run_time 设为当前时间，让 APScheduler 立即调度一次，
            # 而不是等到第一个自然触发时间点
            **({"next_run_time": utc_now()} if job.run_on_start else {}),
        )
        logger.info(
            "[Scheduler] scheduled job %s with trigger=%s%s",
            job.id,
            type(trigger).__name__,
            " starting now" if job.run_on_start else "",
        )

    # 生成一个绑定了 job_id 的零参数异步闭包交给 APScheduler 调用；
    # 运行时才通过 job_id 去 self._jobs 里查最新定义，这样即使任务在注册后被 register_job 替换过，
    # 实际触发时也总是执行最新版本，而不是闭包创建时刻的旧版本
    def _make_job_runner(self, job_id: str) -> Callable[[], Awaitable[Any]]:
        async def _runner() -> Any:
            job = self._jobs[job_id]
            return await self._run_job(job)

        return _runner

    # 实际执行入口：先检查 enabled（可能是动态函数）决定是否跳过；
    # 执行失败时记录日志并重新抛出（APScheduler 会据此把本次运行记为失败）；
    # finally 中始终尝试刷新一次触发器，以支持 callable trigger 的"动态间隔"场景。
    async def _run_job(self, job: ScheduledJob) -> Any:
        try:
            if not self._resolve_enabled(job):
                return {"skipped": True, "reason": "disabled"}
            result = await job.handler()
            return result
        except Exception as exc:
            logger.warning("[Scheduler] job %s failed: %s", job.id, exc)
            raise
        finally:
            self._refresh_trigger_if_needed(job)

    def _refresh_trigger_if_needed(self, job: ScheduledJob) -> None:
        """Reschedule if the trigger is a callable that may have changed."""
        if self._scheduler is None:
            return
        if not callable(job.trigger):
            return
        # Only refresh interval-style callable triggers (legacy pattern)
        # trigger 是可调用对象时才需要每次跑完检查间隔是否变了（历史遗留的动态间隔支持）；
        # 静态 Trigger（cron/date 等）注册后不会自动变化，不需要在这里处理
        new_trigger = self._resolve_trigger(job)
        if isinstance(new_trigger, IntervalTrigger):
            current = self._scheduled_intervals.get(job.id)
            if current != new_trigger.interval_length:
                self._scheduler.reschedule_job(job.id, trigger=new_trigger)
                self._scheduled_intervals[job.id] = new_trigger.interval_length

    @staticmethod
    def _resolve_trigger(job: ScheduledJob) -> BaseTrigger:
        """Resolve the trigger, evaluating callables."""
        trigger = job.trigger
        if callable(trigger):
            return trigger()
        return trigger

    @staticmethod
    def _resolve_interval_seconds(job: ScheduledJob) -> int:
        """Legacy helper: resolve interval from an IntervalTrigger-based job."""
        from apscheduler.triggers.interval import IntervalTrigger

        trigger = RuntimeScheduler._resolve_trigger(job)
        if isinstance(trigger, IntervalTrigger):
            return max(1, int(trigger.interval_length))
        return 0

    @staticmethod
    def _resolve_enabled(job: ScheduledJob) -> bool:
        # 与 trigger 相同的"固定值或可调用求值"模式，支持在不重新注册任务的情况下动态开关
        value = job.enabled() if callable(job.enabled) else job.enabled
        return bool(value)


# 进程级单例：一个进程内只需要一个 RuntimeScheduler 实例统一管理所有后台定时任务
_runtime_scheduler: RuntimeScheduler | None = None


def get_runtime_scheduler() -> RuntimeScheduler:
    global _runtime_scheduler
    if _runtime_scheduler is None:
        _runtime_scheduler = RuntimeScheduler()
    return _runtime_scheduler


async def close_runtime_scheduler() -> None:
    """Stop and release the process-local scheduler without creating it."""
    global _runtime_scheduler
    scheduler = _runtime_scheduler
    # 先取出并清空单例引用，再停止，避免停止过程中其他协程仍拿到即将失效的实例；
    # 若单例从未被创建过则直接返回，不会意外触发创建
    _runtime_scheduler = None
    if scheduler is None:
        return
    await scheduler.stop()
    scheduler.clear()

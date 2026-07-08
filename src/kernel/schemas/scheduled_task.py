"""Scheduled task schemas."""
# 本模块定义"定时任务"（Scheduled Task）功能相关的全部 Pydantic 模型，大致分为四组：
# 1. 枚举：触发方式（TriggerType）、任务状态（ScheduledTaskStatus）、单次运行状态（RunStatus）；
# 2. 触发器配置：IntervalTriggerConfig/CronTriggerConfig/DateTriggerConfig，
#    分别对应"固定间隔""cron 表达式""一次性指定时间"三种调度方式，
#    以及可选的 ChannelDeliveryConfig（把任务结果投递到外部聊天渠道，如飞书）；
# 3. 任务本体：ScheduledTaskCreate/Update 为 API 请求体，ScheduledTask 为持久化到 MongoDB 的文档模型；
# 4. 运行记录与响应：TaskRunRecord（持久化的单次执行记录）及各类分页/API 响应模型。
# 该模块被定时任务的调度引擎（如基于 APScheduler）、任务管理 API 路由、以及任务执行结果的
# 会话/渠道投递逻辑所共同使用。

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

# ChannelType：外部聊天渠道类型枚举（如飞书），用于任务结果投递目标
from src.kernel.schemas.channel import ChannelType

# ── Enums ──────────────────────────────────────────


# 定时任务的触发方式
class TriggerType(str, Enum):
    # 固定间隔重复执行（每隔 N 秒）
    INTERVAL = "interval"
    # 按 cron 表达式定义的调度规则重复执行
    CRON = "cron"
    # 在指定的某一个具体时间点执行一次
    DATE = "date"


# 定时任务本身的生命周期状态（区别于"某次运行"的状态）
class ScheduledTaskStatus(str, Enum):
    # 活跃：调度器会按配置正常触发该任务
    ACTIVE = "active"
    # 已暂停：任务仍存在，但调度器不会触发执行，直到恢复为 ACTIVE
    PAUSED = "paused"
    # 已删除：软删除标记，任务不再触发也不在正常列表中展示
    DELETED = "deleted"


# 单次任务执行（一次 run）的状态
class RunStatus(str, Enum):
    # 等待执行（已创建运行记录，尚未真正开始）
    PENDING = "pending"
    # 正在执行中
    RUNNING = "running"
    # 执行成功
    SUCCESS = "success"
    # 执行失败
    FAILED = "failed"
    # 本次调度被跳过（例如上一次运行尚未结束，或任务已暂停/禁用）
    SKIPPED = "skipped"
    # 执行超时（超过 timeout_seconds 仍未完成而被强制终止）
    TIMEOUT = "timeout"


# ── Trigger configs ────────────────────────────────
# 以下三个模型分别对应 TriggerType 的三种取值，描述"什么时候触发"的具体参数。
# 实际持久化/传输时 trigger_config 字段是弱类型的 dict（见下文 ScheduledTaskCreate），
# 按 trigger_type 的取值在业务逻辑层解析并校验为对应的这三种模型之一。


# 固定间隔触发器：每隔固定秒数重复执行一次
class IntervalTriggerConfig(BaseModel):
    """Fixed-interval trigger."""

    # 触发间隔，单位秒，至少为 1
    seconds: int = Field(..., ge=1, description="Interval in seconds")


# Cron 表达式触发器：字段命名与常见调度库（如 APScheduler）的 cron 触发器参数保持一致，
# 便于直接映射为底层调度器的构造参数；每个字段都是可选的字符串模式，留空表示不限制该维度
class CronTriggerConfig(BaseModel):
    """Cron-expression trigger. All fields accept standard cron syntax."""

    # 年份模式
    year: Optional[str] = Field(None, description="Year pattern")
    # 月份模式（1-12）
    month: Optional[str] = Field(None, description="Month pattern (1-12)")
    # 日期模式（1-31）
    day: Optional[str] = Field(None, description="Day of month pattern (1-31)")
    # ISO 周数模式（1-53）
    week: Optional[str] = Field(None, description="ISO week pattern (1-53)")
    # 星期模式（如 mon,tue,...）
    day_of_week: Optional[str] = Field(None, description="Day of week pattern (mon,tue,...)")
    # 小时模式（0-23），默认 "0"
    hour: Optional[str] = Field("0", description="Hour pattern (0-23)")
    # 分钟模式（0-59），默认 "0"
    minute: Optional[str] = Field("0", description="Minute pattern (0-59)")
    # 秒模式（0-59），默认 "0"
    second: Optional[str] = Field("0", description="Second pattern (0-59)")


# 一次性触发器：在指定的某个 UTC 时间点执行一次后不再重复
class DateTriggerConfig(BaseModel):
    """One-time trigger at a specific UTC timestamp."""

    # 计划执行的具体时间
    run_date: datetime = Field(..., description="One-time execution datetime")


# 定时任务结果的渠道投递目标配置（可选功能）：任务执行完成后，除了写入运行记录，
# 还可以把结果推送到某个外部聊天渠道（如飞书群/会话）
class ChannelDeliveryConfig(BaseModel):
    """Optional channel delivery target for scheduled-task results."""

    # 目标渠道类型（如飞书）
    channel_type: ChannelType = Field(..., description="Channel type to deliver task results to")
    # 目标渠道内的会话/聊天 ID，不能为空
    chat_id: str = Field(..., min_length=1, description="Target channel chat/conversation ID")
    # 产生该投递目标时所对应的渠道实例 ID（同一渠道类型可能存在多个实例，如多个飞书应用）
    channel_instance_id: Optional[str] = Field(
        None,
        description="Channel instance ID that originated this delivery target",
    )
    # 是否启用渠道结果投递功能；关闭后任务仍正常运行，只是不再推送结果
    enabled: bool = Field(True, description="Whether channel result delivery is enabled")
    # 是否仅在任务执行成功时才推送结果到渠道
    send_on_success: bool = Field(True, description="Send successful agent results to channel")
    # 推送到渠道的文本内容最大字符数（超出会被截断），范围 [1, 20000]，默认 4000
    max_content_chars: int = Field(
        4000,
        ge=1,
        le=20000,
        description="Maximum text characters sent to the channel",
    )


# ── Task models ────────────────────────────────────


# 创建定时任务的请求体（对应任务管理 API 的 POST 接口）
class ScheduledTaskCreate(BaseModel):
    """Request body for creating a scheduled task."""

    # 任务名称，长度 1-200
    name: str = Field(..., min_length=1, max_length=200)
    # 该任务触发时要运行的智能体 ID
    agent_id: str = Field(..., min_length=1)
    # 触发方式：interval/cron/date
    trigger_type: TriggerType
    # 触发器的具体参数；结构随 trigger_type 不同而不同，
    # 对应 IntervalTriggerConfig | CronTriggerConfig | DateTriggerConfig 三者之一
    trigger_config: dict = Field(
        ...,
        description="Trigger config (IntervalTriggerConfig | CronTriggerConfig | DateTriggerConfig)",
    )
    # 解释 cron/date 调度语义时使用的 IANA 时区，默认 UTC
    timezone: str = Field("UTC", description="IANA timezone used for cron/date schedule semantics")
    # 触发时传给智能体的输入参数
    input_payload: dict = Field(default_factory=dict, description="Agent input parameters")
    # 任务描述，最长 2000 字符
    description: Optional[str] = Field(None, max_length=2000)
    # 创建后是否立即处于启用状态（可正常被调度触发）
    enabled: bool = Field(True)
    # 创建成功后是否立刻执行一次（不等待下一次调度时机）
    run_on_start: bool = Field(False)
    # 单次运行失败后的最大重试次数，范围 [0, 10]
    max_retries: int = Field(0, ge=0, le=10)
    # 单次运行的超时时间（秒），范围 [10, 7200]（即最长 2 小时）
    timeout_seconds: int = Field(3600, ge=10, le=7200)
    # 若该任务是由某次对话中创建的（如智能体自行安排定时任务），记录来源会话 ID
    source_session_id: Optional[str] = Field(
        None, description="Conversation session where the task was created"
    )
    # 若该任务是由某次智能体运行中创建的，记录来源 run ID
    source_run_id: Optional[str] = Field(None, description="Agent run where the task was created")
    # 创建来源：user（用户手动创建）/ agent（智能体自主创建）/ api（外部 API 创建）
    created_by: str = Field("user", description="Creator source: user / agent / api")
    # 可选的结果投递渠道目标（任务成功执行后把结果推送到该渠道）
    delivery: Optional[ChannelDeliveryConfig] = Field(
        None,
        description="Optional channel target for delivering successful task results",
    )


# 更新定时任务的请求体；所有字段均可选，未提供的字段保持原值不变（PATCH 语义）。
# 相比 ScheduledTaskCreate 缺少 source_session_id/source_run_id/created_by，
# 这三个是创建时固定下来的"来源溯源"信息，语义上创建后不允许再修改
class ScheduledTaskUpdate(BaseModel):
    """Request body for updating a scheduled task."""

    # 新的任务名称
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    # 新绑定的智能体 ID
    agent_id: Optional[str] = Field(None, min_length=1)
    # 新的触发方式
    trigger_type: Optional[TriggerType] = None
    # 新的触发器参数
    trigger_config: Optional[dict] = None
    # 新的时区设置
    timezone: Optional[str] = None
    # 新的智能体输入参数
    input_payload: Optional[dict] = None
    # 新的任务描述
    description: Optional[str] = Field(None, max_length=2000)
    # 新的启用状态
    enabled: Optional[bool] = None
    # 新的"创建后立即执行一次"设置（一般更新场景意义不大，但保留字段以支持覆盖）
    run_on_start: Optional[bool] = None
    # 新的最大重试次数
    max_retries: Optional[int] = Field(None, ge=0, le=10)
    # 新的超时时间（秒）
    timeout_seconds: Optional[int] = Field(None, ge=10, le=7200)
    # 新的渠道投递配置
    delivery: Optional[ChannelDeliveryConfig] = None


# 定时任务的完整持久化文档模型（对应 MongoDB 中的一条任务记录），
# 在 ScheduledTaskCreate 的基础上补充了运行时/生命周期相关的字段
class ScheduledTask(BaseModel):
    """Full task document persisted in MongoDB."""

    # 允许既用字段名 id，也用别名 _id 来构造该模型，便于直接从 Mongo 文档转换
    model_config = ConfigDict(populate_by_name=True)

    # 任务唯一 ID，对应 Mongo 文档的 _id
    id: str = Field(..., alias="_id")
    # 任务名称
    name: str
    # 任务描述
    description: Optional[str] = None
    # 绑定的智能体 ID
    agent_id: str
    # 触发方式
    trigger_type: TriggerType
    # 触发器参数
    trigger_config: dict
    # 调度时区
    timezone: str = "UTC"
    # 智能体输入参数
    input_payload: dict
    # 任务当前生命周期状态（活跃/暂停/已删除）
    status: ScheduledTaskStatus = ScheduledTaskStatus.ACTIVE
    # 是否启用
    enabled: bool = True
    # 创建后是否立即执行过一次
    run_on_start: bool = False
    # 最大重试次数
    max_retries: int = 0
    # 单次运行超时时间（秒）
    timeout_seconds: int = 3600
    # 任务归属/创建者的用户 ID（用于权限校验与"我的任务"列表过滤）
    owner_id: str = Field(..., description="Creator user_id")
    # 来源会话 ID
    source_session_id: Optional[str] = None
    # 来源运行 ID
    source_run_id: Optional[str] = None
    # 创建来源：user/agent/api
    created_by: str = "user"
    # 渠道投递配置
    delivery: Optional[ChannelDeliveryConfig] = None
    # 最近一次运行的开始时间（缓存字段，避免每次都查运行记录表）
    last_run_at: Optional[datetime] = None
    # 最近一次运行的状态
    last_run_status: Optional[RunStatus] = None
    # 最近一次运行对应的 run_id
    last_run_id: Optional[str] = None
    # 累计运行次数
    total_runs: int = 0
    # 任务创建时间
    created_at: Optional[datetime] = None
    # 任务最后更新时间
    updated_at: Optional[datetime] = None


# ── Run record models ──────────────────────────────


# 定时任务"某一次具体执行"的持久化记录（对应 MongoDB 中的一条运行记录），
# 与 ScheduledTask（任务定义本身）是一对多关系：一个任务可以有多条运行记录
class TaskRunRecord(BaseModel):
    """Single execution record persisted in MongoDB."""

    # 允许既用字段名 id，也用别名 _id 来构造该模型
    model_config = ConfigDict(populate_by_name=True)

    # 本次运行的唯一标识（UUID），对应 Mongo 文档的 _id
    id: str = Field(..., alias="_id", description="run_id (UUID)")
    # 所属的定时任务 ID
    task_id: str
    # 本次运行使用的智能体 ID
    agent_id: str
    # 本次运行的触发方式：cron/interval/date 之外还可能是 "manual"（用户手动点击立即执行），
    # 因此这里用普通字符串而非直接复用 TriggerType 枚举
    trigger_type: str = Field("cron", description="Trigger mode: cron / interval / date / manual")
    # 本次运行的当前状态
    status: RunStatus = RunStatus.PENDING
    # 本次运行对应创建的对话会话 ID（便于用户在会话列表中查看这次运行的完整交互过程）
    session_id: Optional[str] = None
    # 本次运行的可观测性追踪 ID
    trace_id: Optional[str] = None
    # 触发时刻 input_payload 的快照，与任务当前配置解耦（后续修改任务配置不影响历史运行记录）
    input_snapshot: dict = Field(default_factory=dict)
    # 智能体的最终输出结果，结构不固定
    output_result: Any = None
    # 执行失败时的错误信息
    error_message: Optional[str] = None
    # 已重试次数
    retry_count: int = 0
    # 实际开始执行的时间
    started_at: Optional[datetime] = None
    # 执行结束的时间
    finished_at: Optional[datetime] = None
    # 执行耗时，单位毫秒
    duration_ms: Optional[int] = None
    # 该运行记录的创建时间
    created_at: Optional[datetime] = None


# ── API responses ──────────────────────────────────


# 定时任务对外 API 响应体：字段含义与 ScheduledTask 基本一一对应（此处不再逐条复述），
# 区别在于这是面向前端的普通字段结构（无 _id 别名），并新增 unread_count 展示字段
class ScheduledTaskResponse(BaseModel):
    """API response for a scheduled task."""

    # 任务 ID
    id: str
    # 任务名称
    name: str
    # 任务描述
    description: Optional[str] = None
    # 绑定的智能体 ID
    agent_id: str
    # 触发方式
    trigger_type: TriggerType
    # 触发器参数
    trigger_config: dict
    # 调度时区
    timezone: str = "UTC"
    # 智能体输入参数
    input_payload: dict
    # 任务生命周期状态
    status: ScheduledTaskStatus
    # 是否启用
    enabled: bool
    # 是否创建后立即执行过一次
    run_on_start: bool
    # 最大重试次数
    max_retries: int
    # 单次运行超时时间（秒）
    timeout_seconds: int
    # 任务归属用户 ID
    owner_id: str
    # 来源会话 ID
    source_session_id: Optional[str] = None
    # 来源运行 ID
    source_run_id: Optional[str] = None
    # 创建来源
    created_by: str = "user"
    # 渠道投递配置
    delivery: Optional[ChannelDeliveryConfig] = None
    # 最近一次运行开始时间
    last_run_at: Optional[datetime] = None
    # 最近一次运行状态
    last_run_status: Optional[RunStatus] = None
    # 最近一次运行的 run_id
    last_run_id: Optional[str] = None
    # 累计运行次数
    total_runs: int = 0
    # 该任务下用户尚未查看的运行结果/通知数量（用于任务列表页的未读徽标）
    unread_count: int = 0
    # 创建时间
    created_at: Optional[datetime] = None
    # 最后更新时间
    updated_at: Optional[datetime] = None


# 单次任务运行记录的对外 API 响应体，字段含义与 TaskRunRecord 一一对应
class TaskRunResponse(BaseModel):
    """API response for a single task run."""

    # 运行记录 ID（run_id）
    id: str
    # 所属任务 ID
    task_id: str
    # 使用的智能体 ID
    agent_id: str
    # 触发方式（含 manual）
    trigger_type: str
    # 运行状态
    status: RunStatus
    # 关联的对话会话 ID
    session_id: Optional[str] = None
    # 追踪 ID
    trace_id: Optional[str] = None
    # 触发时刻的输入快照
    input_snapshot: dict
    # 最终输出结果
    output_result: Any = None
    # 错误信息
    error_message: Optional[str] = None
    # 已重试次数
    retry_count: int = 0
    # 开始时间
    started_at: Optional[datetime] = None
    # 结束时间
    finished_at: Optional[datetime] = None
    # 耗时（毫秒）
    duration_ms: Optional[int] = None
    # 创建时间
    created_at: Optional[datetime] = None


# 任务运行记录分页列表的响应体
class TaskRunListResponse(BaseModel):
    """API response for paginated task run list."""

    # 当前页的运行记录列表
    items: list[TaskRunResponse]
    # 满足条件的记录总数
    total: int


# 定时任务分页列表的响应体
class ScheduledTaskListResponse(BaseModel):
    """API response for paginated scheduled task list."""

    # 当前页的任务列表
    items: list[ScheduledTaskResponse]
    # 满足条件的任务总数
    total: int


# ── Task session responses ──────────────────────────


# 精简版的会话信息，用于"定时任务详情页下钻查看关联会话列表"场景，
# 相比完整的会话模型只保留了列表展示所需的最小字段集
class TaskSessionResponse(BaseModel):
    """Lightweight session response for the scheduled-task drill-down."""

    # 会话 ID
    id: str
    # 会话名称/标题
    name: Optional[str] = None
    # 该会话使用的智能体 ID，默认 "default"
    agent_id: str = "default"
    # 会话创建时间
    created_at: Optional[datetime] = None
    # 会话最后更新时间
    updated_at: Optional[datetime] = None
    # 会话是否仍处于活跃状态
    is_active: bool = True
    # 会话的附加元数据
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 该会话下未读消息/结果数量
    unread_count: int = 0


# 任务关联会话分页列表的响应体
class TaskSessionListResponse(BaseModel):
    """API response for paginated task session list."""

    # 当前页的会话列表
    items: list[TaskSessionResponse]
    # 满足条件的会话总数
    total: int

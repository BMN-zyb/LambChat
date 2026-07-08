"""
Usage log schemas for token consumption tracking.

定义使用日志的数据模型，基于 traces 集合中的 token:usage 事件。
"""

# 模块说明：本文件定义"用量统计"相关的数据模型，数据来源于 traces 集合里
# 每次 Agent 调用产生的 token:usage 事件，涵盖单条日志、聚合统计、
# 仪表盘汇总、按天趋势点、各维度排行榜等结构。
# 主要使用方：src/infra/usage/storage.py（用量数据的聚合查询）、
# src/api/routes/usage.py（用量日志列表 / 仪表盘 HTTP 接口）。
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class UsageLog(BaseModel):
    """单条使用日志（一次 trace 的 token 消耗）"""

    # 关联的 trace ID（一次 Agent 调用链的唯一标识）
    trace_id: str
    # 所属会话 ID
    session_id: str
    # 发起用户 ID
    user_id: str
    # 用户名（冗余存储，避免列表展示时再查用户表）
    username: str = ""
    # 使用的 Agent 名称（冗余展示字段）
    agent_name: str = ""
    # 所属团队 ID（团队协作场景下触发）
    team_id: str = ""
    # 所属团队名称（冗余展示字段）
    team_name: str = ""
    # 使用的人设预设 ID
    persona_preset_id: str = ""
    # 使用的人设预设名称（冗余展示字段）
    persona_preset_name: str = ""
    # 触发来源，如 "chat"（普通对话）、"scheduled"（定时任务）等
    source: str = "chat"
    # 若由定时任务触发，对应的定时任务 ID
    scheduled_task_id: str = ""
    # 若由定时任务触发，对应的本次运行（run）ID
    scheduled_task_run_id: str = ""
    # 若由定时任务触发，对应的触发方式（如 cron / interval / date）
    scheduled_task_trigger_type: str = ""
    # 实际调用的模型标识
    model: str = ""
    # 输入 token 数
    input_tokens: int = 0
    # 输出 token 数
    output_tokens: int = 0
    # 合计 token 数（一般为输入+输出）
    total_tokens: int = 0
    # 提示缓存写入的 token 数
    cache_creation_tokens: int = 0
    # 提示缓存命中（读取）的 token 数
    cache_read_tokens: int = 0
    # 本次调用耗时，单位秒
    duration: float = 0.0
    # 开始时间
    started_at: Optional[datetime] = None
    # 结束时间
    completed_at: Optional[datetime] = None
    # 本次调用状态，如 success / failed / unknown
    status: str = "unknown"

    # 允许从属性对象（如 ORM 实例、Mongo 文档转换出的对象）直接构造本模型
    model_config = ConfigDict(from_attributes=True)


class UsageStats(BaseModel):
    """聚合使用统计"""

    # 总请求（trace）数
    total_requests: int = 0
    # 输入 token 总数
    total_input_tokens: int = 0
    # 输出 token 总数
    total_output_tokens: int = 0
    # token 总数（输入+输出）
    total_tokens: int = 0
    # 缓存写入 token 总数
    total_cache_creation_tokens: int = 0
    # 缓存命中 token 总数
    total_cache_read_tokens: int = 0
    # 累计耗时，单位秒
    total_duration: float = 0.0


class UsageLogListResponse(BaseModel):
    """分页使用日志列表响应"""

    # 当前页的日志列表
    items: list[UsageLog]
    # 满足筛选条件的总条数（不受分页限制）
    total: int
    # 当前筛选条件下的聚合统计
    stats: UsageStats


# 用量仪表盘的汇总指标（对应查询时间范围内的整体统计，如最近 7/30 天）。
class UsageDashboardSummary(BaseModel):
    # 总请求（trace）数
    total_requests: int = 0
    # token 总数（输入+输出）
    total_tokens: int = 0
    # 输入 token 总数
    total_input_tokens: int = 0
    # 输出 token 总数
    total_output_tokens: int = 0
    # 缓存命中 token 总数
    total_cache_read_tokens: int = 0
    # 累计耗时，单位秒
    total_duration: float = 0.0
    # 期间内工具调用总次数
    total_tool_calls: int = 0
    # 由定时任务触发的请求数
    scheduled_runs: int = 0
    # 失败请求数
    failed_requests: int = 0
    # 成功率 = 成功请求数 / 总请求数
    success_rate: float = 0.0
    # 平均每请求 token 数 = total_tokens / total_requests
    avg_tokens_per_request: float = 0.0
    # 平均每请求耗时 = total_duration / total_requests
    avg_duration_per_request: float = 0.0
    # 定时任务请求占比 = scheduled_runs / total_requests
    scheduled_share: float = 0.0
    # 缓存命中占比 = total_cache_read_tokens / total_input_tokens
    cache_read_share: float = 0.0
    # 平均每请求工具调用次数 = total_tool_calls / total_requests
    tool_calls_per_request: float = 0.0
    # 期间内单次请求的最长耗时
    max_duration: float = 0.0
    # 期间内最"忙"的一天（按 tokens/请求数/耗时排序取最大值），无数据时为空；
    # 使用字符串前向引用是因为 UsageDailyPoint 定义在本类之后
    peak_day: Optional["UsageDailyPoint"] = None


# 仪表盘按天统计的一个数据点，用于绘制趋势图。
class UsageDailyPoint(BaseModel):
    # 日期字符串，如 "2026-07-01"
    date: str
    # 当日请求数
    requests: int = 0
    # 当日 token 总数
    tokens: int = 0
    # 当日累计耗时，单位秒
    duration: float = 0.0
    # 当日由定时任务触发的请求数
    scheduled_runs: int = 0
    # 当日失败请求数
    failed_requests: int = 0
    # 当日工具调用次数
    tool_calls: int = 0


# 用量排行榜的通用条目结构，可复用于 Agent / 团队 / 人设 / 模型 / 用户 /
# 来源 / 触发方式等多种排行榜场景。
class UsageRankingItem(BaseModel):
    # 排行对象的 ID，具体含义随场景变化（如 agent_id、team_id、模型标识等）
    id: str
    # 排行对象的展示名称
    name: str
    # 该对象在统计期间内的请求数（排行依据之一）
    requests: int = 0
    # 该对象在统计期间内的 token 总数
    tokens: int = 0
    # 该对象在统计期间内的累计耗时
    duration: float = 0.0


# 用量仪表盘接口的整体响应：概览指标 + 每日趋势 + 各维度排行榜。
class UsageDashboardResponse(BaseModel):
    # 汇总指标
    summary: UsageDashboardSummary
    # 按天的趋势数据点列表
    daily: list[UsageDailyPoint]
    # Agent 用量排行
    top_agents: list[UsageRankingItem]
    # 团队用量排行
    top_teams: list[UsageRankingItem]
    # 人设预设用量排行
    top_personas: list[UsageRankingItem]
    # 模型用量排行
    top_models: list[UsageRankingItem]
    # 用户用量排行（默认空列表，通常仅管理员视角需要）
    top_users: list[UsageRankingItem] = []
    # 按触发来源（chat/scheduled 等）统计的排行
    sources: list[UsageRankingItem] = []
    # 按定时任务触发方式（cron/interval/date 等）统计的排行
    triggers: list[UsageRankingItem] = []

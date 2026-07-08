"""任务运行标识（run_id）生成工具。

run_id 是后台任务引擎实现「多轮对话隔离」的核心：每次提交任务都会生成一个
唯一 run_id，session 内多轮请求、任务恢复产生的新一轮都以 run_id 区分，
心跳 / 中断 / 并发槽位 / trace 等都按 run_id 隔离，互不干扰。
"""

from __future__ import annotations

import uuid

from src.infra.utils.datetime import utc_now


# 生成格式为 run_<UTC时间戳>_<8位随机十六进制> 的唯一 run_id。
# 时间戳前缀便于按时间排序 / 排查；随机后缀保证并发提交也不会碰撞。
def generate_run_id() -> str:
    """Generate a unique task run identifier."""
    return f"run_{utc_now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"

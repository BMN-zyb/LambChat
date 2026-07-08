# src/infra/task/constants.py
"""
Background Task Manager - Constants
"""

# 本模块集中定义 task 引擎在 Redis 上使用的 key 前缀、pub/sub 频道名，以及
# 心跳相关的时间参数。并发限流 / 心跳 / 取消 / 恢复 / pubsub 等子模块都复用
# 这里的常量，集中定义可避免各处硬编码字符串产生不一致。

# Redis keys and channels
# 分布式取消信号频道：某实例调用取消时向此频道 publish，其余实例订阅后就地
# 取消本地运行的 asyncio 任务，实现跨进程取消。
CANCEL_CHANNEL = "task:cancel"
# 任务心跳 key 前缀（完整 key 为 HEARTBEAT_PREFIX + run_id）：worker 定期写入
# 并带 TTL，用于判定任务是否仍存活；进程崩溃后 key 过期即代表任务已死。
HEARTBEAT_PREFIX = "task:heartbeat:"
# 中断信号 key 前缀（完整 key 为 INTERRUPT_PREFIX + run_id）：取消任务时写入，
# 供 agent 在执行检查点主动检测并抛出 TaskInterruptedError 实现优雅中断。
INTERRUPT_PREFIX = "task:interrupt:"  # 中断信号前缀
HEARTBEAT_INTERVAL = 10  # 心跳间隔（秒）
# 心跳超时阈值：超过该秒数无心跳即视为任务已死（僵尸任务）。并发限流的
# Sorted Set 据此清理过期条目，启动恢复也据此判断是否需要接管该任务。
HEARTBEAT_TIMEOUT = 60  # 心跳超时阈值（秒）

# Settings sync channel (distributed instances)
# 全局设置变更广播频道：多实例部署时，一处修改设置后广播，其余实例刷新缓存。
SETTINGS_CHANNEL = "settings:changed"

# Model config sync channel (distributed instances)
# 模型配置变更广播频道：作用同上，用于跨实例同步模型相关配置。
MODEL_CONFIG_CHANNEL = "model_config:changed"

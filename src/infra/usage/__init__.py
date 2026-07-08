"""Usage tracking infrastructure."""

# 用量追踪子包:核心是独立的 usage_logs 集合(见 storage.py),在 trace 完成时写入扁平化的
# token 消耗记录,供列表查询与运营仪表盘聚合使用。

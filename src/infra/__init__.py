"""
基础设施层 (Infrastructure Layer)

提供底层服务实现，依赖 kernel 层。

包含：
- auth: 认证授权
- user: 用户管理
- role: 角色管理
- llm: LLM 客户端
- storage: 存储服务
- backend: 后端服务
- session: 会话管理
- skill: 技能管理
- tool: 工具管理
- service: 第三方服务
"""

# 各模块通过子包导入
# 本包不做「聚合再导出」：__all__ 保持为空，调用方按需从各子包直接导入，
# 以避免在导入 src.infra 时触发大量子模块的连锁加载与潜在循环依赖。
__all__ = []

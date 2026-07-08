"""Setting metadata definitions - single source of truth.

This module assembles SETTING_DEFINITIONS from domain-grouped sub-modules:
  - _definitions_core: Frontend, Application, LLM, Session, Event Merger
  - _definitions_sandbox: Sandbox platform, Skills, Code Interpreter
  - _definitions_tools: MCP, Audio, Image Analysis, Image Generation, Scheduled Task
  - _definitions_infra: MongoDB, Redis, Task Backend, LangSmith Tracing
  - _definitions_extra: Security, Storage, User, Memory (already existed)
"""
# SETTING_DEFINITIONS 是整个配置系统的"单一事实来源"（single source of truth）：
# - service.py 的 SettingsService.init_from_env() 用它把 .env 中的值首次导入数据库；
# - constants.py 用它筛选出 is_sensitive=True 的项，生成 SENSITIVE_SETTINGS；
# - 管理后台 API（src/infra/settings）用它渲染设置页面（分类、类型、默认值、
#   是否敏感、是否需要重启、JSON Schema 等元数据均来自这里）；
# - base.py 中 Settings 类的字段默认值理论上也应与这里的 "default" 保持一致
#   （Settings 类字段是 pydantic 的静态声明，这里的 dict 是运行时可查询的元数据）。
# 之所以把定义拆到 5 个 _definitions_*.py 私有子模块（文件名前缀下划线表示
# "内部实现，不建议被其他模块直接 import"），只是为了避免单个文件过大难以维护，
# 本文件负责把它们合并成一份完整的 SETTING_DEFINITIONS 供外部统一使用。

from __future__ import annotations

# 5 个领域子模块一一对应上面文档字符串里列出的分组；每个子模块导出一个独立的
# XXX_SETTING_DEFINITIONS 字典，互不重名（依赖各子模块自行保证 key 不冲突）
from src.kernel.config._definitions_core import CORE_SETTING_DEFINITIONS
from src.kernel.config._definitions_extra import EXTRA_SETTING_DEFINITIONS
from src.kernel.config._definitions_infra import INFRA_SETTING_DEFINITIONS
from src.kernel.config._definitions_sandbox import SANDBOX_SETTING_DEFINITIONS
from src.kernel.config._definitions_tools import TOOLS_SETTING_DEFINITIONS
# SettingCategory/SettingType 是配置项元数据里"分类"和"值类型"字段用的枚举，
# 定义在 schemas 层；这里顺带 re-export，方便调用方只需 import 本模块即可拿到
from src.kernel.schemas.setting import SettingCategory, SettingType

# Re-export for convenience
__all__ = ["SETTING_DEFINITIONS", "SettingCategory", "SettingType"]

# Assemble all definitions
# 用字典展开（**）依次合并 5 个领域字典；如果不同领域之间出现了相同的 key，
# 后展开的会静默覆盖先展开的（Python dict literal 的通用行为），
# 因此各 _definitions_*.py 之间必须保证配置项 key 互不重复，顺序本身不代表优先级设计
SETTING_DEFINITIONS: dict[str, dict] = {
    **CORE_SETTING_DEFINITIONS,
    **SANDBOX_SETTING_DEFINITIONS,
    **TOOLS_SETTING_DEFINITIONS,
    **INFRA_SETTING_DEFINITIONS,
    **EXTRA_SETTING_DEFINITIONS,
}

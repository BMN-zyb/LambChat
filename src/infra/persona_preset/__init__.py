"""Persona preset domain."""

# 对外统一导出人设预设的业务门面 Manager 及其单例获取/释放函数，
# 上层代码只需从 src.infra.persona_preset 导入即可，无需关心内部模块划分。
from src.infra.persona_preset.manager import (
    PersonaPresetManager,
    close_persona_preset_manager,
    get_persona_preset_manager,
)

__all__ = [
    "PersonaPresetManager",
    "get_persona_preset_manager",
    "close_persona_preset_manager",
]

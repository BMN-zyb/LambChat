"""
LLM 客户端模块
"""

# 注意导入顺序：必须先应用第三方库补丁，再导入依赖这些库的 client/models_service。
# reasoning_patch 修正推理（thinking）相关行为，deepagents_patch 修补 deepagents 库。
from src.infra.llm.deepagents_patch import apply_deepagents_patches
from src.infra.llm.reasoning_patch import apply_reasoning_patches

# 在模块被 import 时立即打补丁（副作用），保证后续所有调用都基于打过补丁的库。
apply_reasoning_patches()
apply_deepagents_patches()

# 补丁生效后再导入对外 API（E402：这些 import 故意放在补丁调用之后）。
from src.infra.llm.client import LLMClient, get_llm_client  # noqa: E402
from src.infra.llm.models_service import (  # noqa: E402
    get_available_models,
    invalidate_cache,
    refresh_models,
)
from src.infra.llm.pubsub import (  # noqa: E402
    get_model_config_pubsub,
    publish_model_config_changed,
)

__all__ = [
    "LLMClient",
    "get_llm_client",
    "get_available_models",
    "invalidate_cache",
    "refresh_models",
    "get_model_config_pubsub",
    "publish_model_config_changed",
]

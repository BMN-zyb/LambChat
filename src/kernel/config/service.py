"""Settings service integration."""
# 本模块把"静态"的 Settings 对象（base.py，只在进程启动时从 env/.env 读取一次）
# 和"动态"的数据库配置（src.infra.settings.service.SettingsService）连接起来，
# 实现"修改配置后不需要重启进程就能生效"的能力，主要提供两个入口：
#   - initialize_settings()：进程启动时调用一次，把数据库中的配置灌入全局 settings；
#   - refresh_settings()：某个（或全部）配置项在数据库中被修改后调用，实时更新
#     内存中的 settings，并触发相应的缓存清理/后端重置副作用。
# 模块内还维护了几个模块级可变状态（_settings_service、_settings_cache）和
# 几个"哪些配置需要特殊处理"的集合常量。

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from src.infra.logging import get_logger

# 拿到的是 base.py 里的全局单例对象；本模块后面通过 setattr(settings, ...) 直接
# 修改它的属性，因为其它模块 import 的是同一个对象引用，改了立刻全局生效
from .base import settings

# SettingsService 属于 infra（基础设施）层，而本文件属于 kernel（内核）层。
# infra.settings.storage 在其模块顶层又反过来 import 了 src.kernel.config
# （需要 SETTING_DEFINITIONS 等），如果这里在模块顶层直接 import SettingsService，
# 会在包（src/kernel/config）尚未初始化完成时形成循环导入并报错。
# 因此只在 TYPE_CHECKING 下引入类型用于标注，真正的运行时导入延迟到
# initialize_settings() 函数体内部（见下文）。
if TYPE_CHECKING:
    from src.infra.settings.service import SettingsService

logger = get_logger(__name__)

# SettingsService integration
# 全局单例引用，由 initialize_settings() 首次赋值；用字符串形式的类型注解
# "SettingsService" 是因为真正的类只在 TYPE_CHECKING 分支被 import，运行时这里
# 只是一个普通的 Optional 引用
_settings_service: Optional["SettingsService"] = None

# Cache for all settings from database
# 注意：当前项目里只有本文件会写入这个缓存（initialize_settings/refresh_settings），
# 没有其它地方读取它——配置的实际读取路径始终是直接访问 settings.XXX 属性。
# 这里更像是"最近一次从数据库加载的原始值"旁路记录，为调试或后续扩展预留。
_settings_cache: dict[str, Any] = {}

# 大多数配置项里，数据库存的空字符串 "" 被当作"未真正设置，应继续使用 env/默认值"的
# 占位含义；但下面这两个 key 把 "" 当作有意义的合法取值（"自动选择/不特别指定"），
# 因此在加载/刷新时需要放行，而不能被当成"跳过不处理"
_ALLOW_EMPTY_STRING_SETTINGS = {
    "DEFAULT_MODEL_ID",
    "NATIVE_MEMORY_COMPACTION_MODEL_ID",
}

# 这组 CHECKPOINT_* 配置同时也出现在 constants.py 的 RESTART_REQUIRED_SETTINGS 中：
# 那边负责在管理后台提示"这项修改建议重启服务"，这里则是尽力而为地自动补救——
# 一旦检测到其中任意一项被修改，就调用 _reset_checkpoint_runtime_state()
# 主动重置 checkpointer 的连接池，让新连接尽快用上新参数，减少必须重启才能生效的窗口
_CHECKPOINT_AFFECTED_SETTINGS = {
    "CHECKPOINT_BACKEND",
    "CHECKPOINT_PG_HOST",
    "CHECKPOINT_PG_PORT",
    "CHECKPOINT_PG_USER",
    "CHECKPOINT_PG_PASSWORD",
    "CHECKPOINT_PG_DB",
    "CHECKPOINT_PG_POOL_MIN_SIZE",
    "CHECKPOINT_PG_POOL_MAX_SIZE",
}


def _mark_runtime_secret_as_explicit(key: str) -> None:
    """标记某个"可能被自动生成的密钥"已经有了显式（来自数据库）的值。

    base.py 的 Settings.__init__ 在 JWT_SECRET_KEY / MCP_ENCRYPTION_SALT /
    VAPID 密钥缺失时会随机生成一份，并把对应的 _*_generated 标志置为 True。
    这个标志之后会被 src.infra.distributed_validation 在分布式部署下检查：
    如果是"随机生成"的，每个副本进程生成的值都不同，会导致副本之间无法互相验证
    JWT/解密数据，因此会在启动时报错拦截。
    一旦这个 key 的值是从数据库加载来的（意味着所有副本都会读到同一份持久化的值），
    就不再是"本进程随机生成"，需要把标志翻回 False，避免被误判为不安全配置。
    """
    if key == "JWT_SECRET_KEY":
        settings._jwt_secret_key_generated = False
    elif key == "MCP_ENCRYPTION_SALT":
        settings._mcp_encryption_salt_generated = False
    elif key == "VAPID_PUBLIC_KEY":
        settings._vapid_keys_generated = False


async def _reset_checkpoint_runtime_state(reason: str) -> None:
    """在 checkpoint 相关配置变化后，重置 checkpointer 的运行时连接状态。

    Args:
        reason: 触发重置的原因描述，仅用于日志，方便排查是哪次设置变更引起的。
    """
    try:
        # 延迟导入：避免 kernel.config 在模块加载阶段就直接依赖 infra.storage.checkpoint
        from src.infra.storage.checkpoint import reset_checkpointer_runtime_state

        await reset_checkpointer_runtime_state()
        logger.info("[Settings] Checkpointer runtime state reset after %s", reason)
    except Exception as exc:
        # 重置失败通常发生在设置已经保存成功之后的收尾阶段，不应该让这一步的异常
        # 影响主流程，因此只记录警告，不向上抛出
        logger.warning(
            "[Settings] Failed to reset checkpointer runtime state after %s: %s",
            reason,
            exc,
        )


async def initialize_settings() -> None:
    """Initialize settings from database, importing from .env if needed.

    After calling this function, the global `settings` object will have its
    attributes overridden by values from the database (database > env > default).
    """
    # 声明修改的是模块级变量，而不是新建同名局部变量
    global _settings_service, _settings_cache

    # 延迟导入 infra 层的 SettingsService，原因见文件顶部的说明（避免循环导入）
    from src.infra.settings.service import SettingsService

    # 获取（或首次创建）SettingsService 单例
    _settings_service = SettingsService.get_instance()
    # 首次调用会顺带把 .env 中尚未写入数据库的配置项导入数据库
    # （具体逻辑见 SettingsService.init_from_env）
    await _settings_service.initialize()
    logger.info("[Settings] SettingsService initialized")

    # Load all settings from database and update the global settings object
    # admin_mode=True：取全部配置项，不局限于前端可见的；
    # mask_sensitive=False：要真实值而不是掩码后的 "****"，因为接下来要把它们
    # 真正写入内存中的 settings 对象，不能是占位符
    all_settings = await _settings_service.get_all(admin_mode=True, mask_sensitive=False)
    logger.info(f"[Settings] Loaded {len(all_settings)} categories from database")

    # Flatten the settings dict and cache them
    # 数据库返回的结构是按分类（category）分组的 {category: [SettingItem, ...]}，
    # 这里展开成扁平的逐项处理
    loaded_count = 0
    for category, items in all_settings.items():
        logger.debug(f"[Settings] Category {category}: {len(items)} items")
        for item in items:
            # Empty strings usually mean "keep env fallback", but selected model
            # settings use "" as an intentional "automatic/default" value.
            if (
                item
                and item.value is not None
                and (item.value != "" or item.key in _ALLOW_EMPTY_STRING_SETTINGS)
            ):
                _settings_cache[item.key] = item.value
                # Only update if the field exists in Settings class
                # 数据库里可能残留历史/已废弃的 key，Settings 类没有对应字段就直接忽略，
                # 不当作错误处理
                if hasattr(settings, item.key):
                    # setattr 直接修改的是全局单例对象，其它模块拿到的引用会立刻看到新值
                    setattr(settings, item.key, item.value)
                    # 这个值是从数据库加载来的显式配置，不再是本进程随机生成的密钥
                    _mark_runtime_secret_as_explicit(item.key)
                    loaded_count += 1

    logger.info(f"[Settings] Loaded {loaded_count} settings into cache")
    logger.info(f"[Settings] REDIS_URL = {settings.REDIS_URL}")

    # Persist auto-generated VAPID keys to database so they survive restarts
    # 如果上面从数据库加载完之后，settings._vapid_keys_generated 仍然是 True，
    # 说明数据库和环境变量里都没有配置 VAPID 密钥，base.py 在这次启动时临时生成了
    # 一对新的。必须马上存回数据库，否则下次重启又会生成不同的一对，导致所有已经
    # 订阅了 Web Push 的浏览器（订阅信息绑定了旧公钥）全部失效
    if settings._vapid_keys_generated and _settings_service is not None:
        try:
            from datetime import datetime, timezone

            # 直接操作底层 MongoDB collection（而不是走 SettingsService.set()），
            # 因为这里要一次性写入两个关联的 key，且这个变更本来就源自本进程，
            # 不需要再触发一遍"设置变更"的刷新/跨实例广播流程
            collection = _settings_service._storage._get_collection()
            now = datetime.now(timezone.utc).isoformat()
            # 公钥和私钥必须成对写入，否则数据库里只保存了一半，
            # 下次启动读到不匹配的另一半会导致 Web Push 无法正常工作
            for key in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"):
                value = getattr(settings, key, "")
                if value:
                    # upsert=True：不存在就插入，存在就覆盖；
                    # 字段结构模拟常规配置项记录的格式，保证后续可以被
                    # SettingsService/管理后台按普通配置项正常读取和展示
                    await collection.update_one(
                        {"_id": key},
                        {
                            "$set": {
                                "value": value,
                                "type": "string",
                                "category": "push",
                                "description": f"Auto-generated VAPID {key} for Web Push",
                                "default_value": "",
                                "updated_at": now,
                                "updated_by": "system",
                            }
                        },
                        upsert=True,
                    )
                    logger.info(f"[Settings] Persisted auto-generated {key} to database")
            # 同步更新内存缓存，与刚写入数据库的值保持一致
            _settings_cache["VAPID_PUBLIC_KEY"] = settings.VAPID_PUBLIC_KEY
            _settings_cache["VAPID_PRIVATE_KEY"] = settings.VAPID_PRIVATE_KEY
            # 已经持久化成功，之后视为"显式配置"而非"本进程随机生成"，
            # 避免被 distributed_validation 误判为多副本下不安全的配置
            settings._vapid_keys_generated = False
            logger.info("[Settings] VAPID keys persisted to database successfully")
        except Exception as exc:
            # 持久化失败不应阻塞启动流程；代价是密钥仍标记为"本次生成"，
            # 单机部署下次重启会再生成一套新的，多副本部署会被 distributed_validation 拦截
            logger.warning("[Settings] Failed to persist auto-generated VAPID keys: %s", exc)


async def refresh_settings(key: Optional[str] = None) -> None:
    """Refresh settings from database.

    Args:
        key: Specific key to refresh, or None for all settings.

    This should be called after database settings are updated.
    """
    global _settings_cache

    # 如果从未调用过 initialize_settings()（比如某些独立脚本/测试场景），
    # 说明没有数据库连接可用，直接跳过，不做任何事也不报错
    if _settings_service is None:
        return

    # Settings that affect LLM model cache (used for title generation etc.)
    # 这些配置一变，之前缓存的 LLM 客户端实例可能用的是旧模型/旧密钥，
    # 需要清空缓存，下一次请求会用新配置重新创建客户端
    llm_affected_settings = {
        "DEFAULT_MODEL_ID",
        "SESSION_TITLE_MODEL",
        "SESSION_TITLE_API_BASE",
        "SESSION_TITLE_API_KEY",
        "LLM_MAX_RETRIES",
    }

    # Settings that require memory backend reinitialization
    # 记忆功能的开关或 embedding 服务地址/密钥变了，正在运行的记忆后端实例
    # 需要重新构建才能用上新配置
    memory_affected_settings = {
        "ENABLE_MEMORY",
        "NATIVE_MEMORY_EMBEDDING_API_BASE",
        "NATIVE_MEMORY_EMBEDDING_API_KEY",
    }

    if key:
        # Refresh single setting
        # 用 get_raw 而不是带掩码的 get：这里要拿真实值写入内存中的 settings 对象，
        # 不能是被脱敏成 "****" 的占位符
        setting = await _settings_service._storage.get_raw(key)
        if (
            setting
            and setting.value is not None
            and (setting.value != "" or key in _ALLOW_EMPTY_STRING_SETTINGS)
        ):
            _settings_cache[key] = setting.value
            setattr(settings, key, setting.value)
            _mark_runtime_secret_as_explicit(key)
            # Clear LLM model cache if this setting affects it
            if key in llm_affected_settings:
                from src.infra.llm.client import LLMClient

                cleared = LLMClient.clear_cache_by_model()
                logger.info(
                    f"[Settings] Cleared {cleared} LLM model cache entries after setting '{key}' changed"
                )
            # Reset memory backend if this setting affects it
            if key in memory_affected_settings:
                from src.infra.memory.tools import schedule_backend_reset

                # 只是登记一个非阻塞的后台重置任务（fire-and-forget），
                # 不在这里同步等待重置完成
                schedule_backend_reset()
                logger.info(f"[Settings] Memory backend reset after setting '{key}' changed")
            if key in _CHECKPOINT_AFFECTED_SETTINGS:
                await _reset_checkpoint_runtime_state(f"setting '{key}' changed")
    else:
        # Refresh all settings
        all_settings = await _settings_service.get_all(admin_mode=True, mask_sensitive=False)
        # 全量刷新时，用三个布尔标记记录"这一批里有没有任意一项命中对应的受影响集合"，
        # 而不是在循环内部逐项立即触发副作用——这样即使这批里有多个 key 命中同一类
        # 副作用（比如同时改了两个 LLM 相关配置），也只会清一次缓存/重置一次，
        # 避免不必要的重复开销
        any_llm_setting_changed = False
        any_memory_setting_changed = False
        any_checkpoint_setting_changed = False
        for items in all_settings.values():
            for item in items:
                if (
                    item
                    and item.value is not None
                    and (item.value != "" or item.key in _ALLOW_EMPTY_STRING_SETTINGS)
                ):
                    _settings_cache[item.key] = item.value
                    setattr(settings, item.key, item.value)
                    _mark_runtime_secret_as_explicit(item.key)
                    if item.key in llm_affected_settings:
                        any_llm_setting_changed = True
                    if item.key in memory_affected_settings:
                        any_memory_setting_changed = True
                    if item.key in _CHECKPOINT_AFFECTED_SETTINGS:
                        any_checkpoint_setting_changed = True

        # Clear LLM model cache if any affected setting changed
        # 循环结束后再统一处理副作用，逻辑与单 key 分支一致，只是触发条件是"批量里
        # 是否命中过"而不是"这一个 key 是否命中"
        if any_llm_setting_changed:
            from src.infra.llm.client import LLMClient

            cleared = LLMClient.clear_cache_by_model()
            logger.info(
                f"[Settings] Cleared {cleared} LLM model cache entries after settings refresh"
            )

        # Reset memory backend if any affected setting changed
        if any_memory_setting_changed:
            from src.infra.memory.tools import schedule_backend_reset

            schedule_backend_reset()
            logger.info("[Settings] Memory backend reset after settings refresh")

        if any_checkpoint_setting_changed:
            await _reset_checkpoint_runtime_state("settings refresh")

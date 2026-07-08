"""
Feishu 消息处理器模块

处理飞书消息的 Agent 执行和响应。
发送一条卡片消息，支持 markdown 渲染。
"""

import asyncio
import sys
import types
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Optional

from src.infra.channel.feishu import approval as _approval_mod
from src.infra.channel.feishu import collector as _collector_mod
from src.infra.channel.feishu import events as _events_mod
from src.infra.channel.feishu.collector import (
    FeishuResponseCollector,
)
from src.infra.channel.feishu.events import _process_events
from src.infra.channel.feishu.handler_helpers import (
    _create_new_feishu_session,
    _get_feishu_session_id,
)
from src.infra.channel.feishu.manager import FeishuChannelManager
from src.infra.logging import get_logger
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings  # noqa: F401 - compatibility for handler tests/patching

logger = get_logger(__name__)

# Re-export the historical handler module surface while the implementation lives
# in focused modules. Tests and downstream monkeypatches still target this module.
# 兼容层：真正实现已拆分到 approval/collector/events 等子模块，但历史上的测试与外部
# monkeypatch 仍指向本模块，因此这里把各符号重新导出，保持对外接口不变。
EVENT_APPROVAL_REQUIRED = _approval_mod.EVENT_APPROVAL_REQUIRED
FEISHU_APPROVAL_ACTION = _approval_mod.FEISHU_APPROVAL_ACTION
build_feishu_approval_processing_card_data = (
    _approval_mod.build_feishu_approval_processing_card_data
)
handle_feishu_approval_action = _approval_mod.handle_feishu_approval_action
_claim_feishu_approval_action = _approval_mod._claim_feishu_approval_action
_get_existing_approval_status = _approval_mod._get_existing_approval_status
_respond_to_human_approval = _approval_mod._respond_to_human_approval

FEISHU_REVEAL_DOWNLOAD_CHUNK_SIZE = _collector_mod.FEISHU_REVEAL_DOWNLOAD_CHUNK_SIZE
FEISHU_REVEAL_DOWNLOAD_MAX_BYTES = _collector_mod.FEISHU_REVEAL_DOWNLOAD_MAX_BYTES
FEISHU_REVEAL_LEGACY_DOWNLOAD_MAX_BYTES = _collector_mod.FEISHU_REVEAL_LEGACY_DOWNLOAD_MAX_BYTES
FEISHU_STREAM_FIRST_PAINT_CHARS = _collector_mod.FEISHU_STREAM_FIRST_PAINT_CHARS
FEISHU_STREAM_UPDATE_DEBOUNCE_SECONDS = _collector_mod.FEISHU_STREAM_UPDATE_DEBOUNCE_SECONDS
_download_storage_object_to_file = _collector_mod._download_storage_object_to_file
run_blocking_io = _collector_mod.run_blocking_io

EVENT_MESSAGE_CHUNK = _events_mod.EVENT_MESSAGE_CHUNK
EVENT_TOOL_RESULT = _events_mod.EVENT_TOOL_RESULT
EVENT_TOOL_START = _events_mod.EVENT_TOOL_START

# 记录"哪些符号被 patch 时需要同步到哪些子模块"。因为子模块在导入时已经把这些名字
# 绑定为各自的局部引用，若只改本模块不会影响它们；这里的映射用于把改动传播过去。
_PATCH_TARGETS = {
    "run_blocking_io": (_collector_mod, _events_mod),
    "FEISHU_STREAM_UPDATE_DEBOUNCE_SECONDS": (_collector_mod,),
    "FEISHU_STREAM_FIRST_PAINT_CHARS": (_collector_mod,),
    "FEISHU_REVEAL_LEGACY_DOWNLOAD_MAX_BYTES": (_collector_mod,),
    "FEISHU_REVEAL_DOWNLOAD_MAX_BYTES": (_collector_mod,),
    "FEISHU_REVEAL_DOWNLOAD_CHUNK_SIZE": (_collector_mod,),
    "_respond_to_human_approval": (_approval_mod,),
    "_claim_feishu_approval_action": (_approval_mod,),
    "_get_existing_approval_status": (_approval_mod, _events_mod),
}


class _FeishuHandlerCompatModule(types.ModuleType):
    # 自定义模块类型：拦截对本模块属性的赋值，一并同步到 _PATCH_TARGETS 中登记的子模块，
    # 使测试里 `monkeypatch.setattr(handler, name, ...)` 能真正影响到实际执行处。
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for module in _PATCH_TARGETS.get(name, ()):
            setattr(module, name, value)


# 把本模块对象的类替换为上面的兼容类型，从而启用属性同步逻辑。
sys.modules[__name__].__class__ = _FeishuHandlerCompatModule


async def execute_feishu_agent(
    session_id: str,
    agent_id: str,
    message: str,
    user_id: str,
    presenter: Optional[Any] = None,
    disabled_tools: list[str] | None = None,
    agent_options: dict | None = None,
    attachments: list[dict] | None = None,
    disabled_skills: list[str] | None = None,
    enabled_skills: list[str] | None = None,
    persona_system_prompt: str | None = None,
    disabled_mcp_tools: list[str] | None = None,
    recommendation_input: str | None = None,
    team_id: str | None = None,
    active_goal: dict | None = None,
    auto_mode: bool = False,
) -> AsyncGenerator[dict[str, Any], None]:
    """执行 Agent 并生成事件流"""
    # 延迟导入以打破潜在的循环依赖，并只在真正执行时才加载重型依赖。
    from src.agents.core.base import AgentFactory
    from src.infra.task.exceptions import TaskInterruptedError

    agent = await AgentFactory.get(agent_id)
    run_id = presenter.run_id if presenter else None

    # 若本次运行带"目标(goal)"，先发一个 goal:start 事件并记录起始时间。
    started_at: str | None = None
    if active_goal is not None:
        started_at = datetime.now(timezone.utc).isoformat()
        yield {"event": "goal:start", "data": {"goal": active_goal, "started_at": started_at}}

    try:
        # 透传所有参数给 agent.stream，并把产出的事件逐个 yield 出去。
        async for event in agent.stream(
            message,
            session_id,
            user_id=user_id,
            presenter=presenter,
            disabled_tools=disabled_tools,
            agent_options=agent_options,
            attachments=attachments,
            disabled_skills=disabled_skills,
            enabled_skills=enabled_skills,
            persona_system_prompt=persona_system_prompt,
            disabled_mcp_tools=disabled_mcp_tools,
            recommendation_input=recommendation_input,
            team_id=team_id,
            active_goal=active_goal,
            auto_mode=auto_mode,
            goal_started_at=started_at,
        ):
            yield event
    except (asyncio.CancelledError, TaskInterruptedError):
        # 被取消/中断时：关闭本次运行；若有 goal，补发 goal:end 事件后再抛出。
        if run_id:
            await agent.close(run_id)
        if active_goal is not None:
            ended_at = datetime.now(timezone.utc).isoformat()
            yield {
                "event": "goal:end",
                "data": {"goal": active_goal, "started_at": started_at, "ended_at": ended_at},
            }
        raise


def create_feishu_message_handler(
    manager: "FeishuChannelManager",
    default_agent: str,
    show_tools: bool = True,
) -> Callable:
    """
    创建飞书消息处理器

    Args:
        manager: 飞书渠道管理器
        default_agent: 默认 Agent ID
        show_tools: 是否显示工具调用
    """
    from src.infra.task.manager import get_task_manager

    async def feishu_message_handler(
        user_id: str,
        sender_id: str,
        chat_id: str,
        content: str,
        metadata: dict,
    ) -> None:
        """处理飞书消息"""
        print(
            f"[DEBUG] feishu_message_handler: {content[:50]}",
            file=sys.stderr,
            flush=True,
        )

        # 从元数据取出原始消息 ID、已加的"收到"表情 ID、渠道实例 ID 等，用于回复与收尾。
        original_message_id = metadata.get("message_id")
        received_reaction_id = metadata.get("reaction_id")
        instance_id = metadata.get("instance_id")
        delivery_chat_id = chat_id

        try:
            logger.info(
                f"[Feishu] Processing message from {sender_id} for user {user_id}: {content[:50]}..."
            )

            sender_id_from_msg = metadata.get("sender_id")
            chat_type_from_msg = metadata.get("chat_type")
            reply_to_message_id = original_message_id
            # 单聊(p2p)用 reply_chat_id 作为实际投递目标；群聊沿用传入 chat_id。
            if chat_type_from_msg == "p2p":
                delivery_chat_id = metadata.get("reply_chat_id") or chat_id
            attachments = metadata.get("attachments")

            # 处理 /new 命令 - 严格匹配
            # 严格匹配 /new：新建会话并回执，随后直接返回（不进入 agent 执行）。
            if content.strip() == "/new":
                new_session_id = await _create_new_feishu_session(chat_id)
                await manager.send_message(
                    user_id,
                    delivery_chat_id,
                    "✅ 已创建新对话，请发送消息开始",
                    instance_id,
                )
                logger.info(f"[Feishu] New session created for chat {chat_id}: {new_session_id}")
                return

            # 获取当前 session ID
            # 按 chat_id 解析出（或复用）当前会话，实现"同一对话延续上下文"。
            session_id = await _get_feishu_session_id(chat_id)
            task_manager = get_task_manager()

            # Resolve agent, model & project: use per-channel config if available
            # 解析本次要用的 agent/模型/项目/人设等：优先采用"该渠道实例"上的绑定配置。
            agent_to_use = default_agent
            model_id: str | None = None
            project_id: str | None = None
            team_id: str | None = None
            persona_preset_id: str | None = None
            enabled_skills: list[str] | None = None
            persona_system_prompt: str | None = None
            persona_metadata: dict[str, Any] | None = None
            channel_name: str | None = None
            stream_reply = True
            ch_storage = None
            if instance_id:
                from src.infra.channel.channel_storage import ChannelStorage
                from src.kernel.schemas.channel import ChannelType

                ch_storage = ChannelStorage()
                ch_config = await ch_storage.get_config(user_id, ChannelType.FEISHU, instance_id)
                if ch_config:
                    if ch_config.get("agent_id"):
                        agent_to_use = ch_config["agent_id"]
                        logger.info(
                            f"[Feishu] Using channel agent: {agent_to_use} for instance {instance_id}"
                        )
                    model_id = ch_config.get("model_id")
                    project_id = ch_config.get("project_id")
                    team_id = ch_config.get("team_id")
                    persona_preset_id = (
                        None if agent_to_use == "team" else ch_config.get("persona_preset_id")
                    )
                    channel_name = ch_config.get("name")
                    stream_reply = bool(ch_config.get("stream_reply", True))

            if persona_preset_id:
                try:
                    # 加载人设预设快照：得到系统提示词与启用技能，并组装人设元数据。
                    from src.infra.persona_preset.manager import PersonaPresetManager

                    snapshot = await PersonaPresetManager().use_preset(
                        persona_preset_id,
                        user_id=user_id,
                        is_admin=False,
                    )
                    persona_system_prompt = snapshot.system_prompt
                    enabled_skills = snapshot.skill_names or None
                    persona_metadata = {
                        "persona_preset_id": snapshot.preset_id,
                        "persona_preset_name": snapshot.name,
                        "persona_snapshot": snapshot.model_dump(),
                        "enabled_skills": enabled_skills,
                    }
                    if snapshot.avatar:
                        persona_metadata["persona_avatar"] = snapshot.avatar
                    logger.info(
                        f"[Feishu] Using channel persona: {snapshot.name} "
                        f"({persona_preset_id}) for instance {instance_id}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[Feishu] Ignoring unavailable channel persona {persona_preset_id}: {e}"
                    )

            if project_id:
                try:
                    # 校验绑定的项目是否仍存在；已删除则清掉配置里的 project_id 并置空。
                    from src.infra.folder.storage import get_project_storage

                    proj_storage = get_project_storage()
                    project = await proj_storage.get_by_id(project_id, user_id)
                    if not project:
                        logger.warning(
                            f"[Feishu] Ignoring missing channel project_id {project_id} "
                            f"for user {user_id}"
                        )
                        if ch_storage and instance_id:
                            await ch_storage.clear_config_project_id(
                                user_id, ChannelType.FEISHU, instance_id
                            )
                        project_id = None
                except Exception as e:
                    logger.warning(f"[Feishu] Failed to validate channel project_id: {e}")
                    project_id = None

            # Auto-create project by channel name if not manually configured
            if not project_id and channel_name:
                try:
                    from src.infra.folder.storage import get_project_storage

                    proj_storage = get_project_storage()
                    project = await proj_storage.get_or_create_by_name(user_id, channel_name)
                    project_id = project.id
                except Exception as e:
                    logger.warning(f"[Feishu] Failed to auto-create project: {e}")

            # Build agent_options with model_id if configured
            feishu_agent_options: dict | None = None
            if model_id:
                feishu_agent_options = {"model_id": model_id}

            collector = FeishuResponseCollector(
                manager=manager,
                user_id=user_id,
                chat_id=delivery_chat_id,
                reply_to_message_id=reply_to_message_id,
                sender_id=sender_id_from_msg,
                chat_type=chat_type_from_msg,
                stream_reply=stream_reply,
                instance_id=instance_id,
            )

            async def executor(
                session_id: str,
                agent_id: str,
                message: str,
                user_id: str,
                presenter=None,
                disabled_tools=None,
                agent_options=None,
                attachments=None,
                disabled_skills=None,
                enabled_skills=None,
                persona_system_prompt=None,
                disabled_mcp_tools=None,
                recommendation_input=None,
                team_id=None,
                active_goal=None,
                auto_mode=False,
            ):
                # executor 适配器：把任务管理器调用桥接到 execute_feishu_agent 事件流，
                # 供 task_manager.submit 在后台运行 agent。
                async for event in execute_feishu_agent(
                    session_id=session_id,
                    agent_id=agent_id,
                    message=message,
                    user_id=user_id,
                    presenter=presenter,
                    disabled_tools=disabled_tools,
                    agent_options=agent_options,
                    attachments=attachments,
                    disabled_skills=disabled_skills,
                    enabled_skills=enabled_skills,
                    persona_system_prompt=persona_system_prompt,
                    disabled_mcp_tools=disabled_mcp_tools,
                    recommendation_input=recommendation_input,
                    team_id=team_id,
                    active_goal=active_goal,
                    auto_mode=auto_mode,
                ):
                    yield event

            # Use time-based session title for Feishu
            session_title = utc_now().strftime("%Y-%m-%d %H:%M")

            # 提交任务给任务管理器在后台执行；返回 run_id 用于追踪本次运行。
            run_id, _ = await task_manager.submit(
                session_id=session_id,
                agent_id=agent_to_use,
                message=content,
                user_id=user_id,
                executor=executor,
                attachments=attachments,
                project_id=project_id,
                agent_options=feishu_agent_options,
                session_name=session_title,
                enabled_skills=enabled_skills,
                persona_system_prompt=persona_system_prompt,
                team_id=team_id if agent_to_use == "team" else None,
                auto_mode=True,
            )
            # 记录会话/运行链接，便于卡片末尾附"查看会话"深链。
            collector.set_session_link(session_id, run_id)
            try:
                # 持久化"渠道投递"元数据：使会话完成时能把结果回投到该飞书会话。
                from src.infra.session.manager import SessionManager
                from src.kernel.schemas.channel import ChannelType

                await SessionManager().update_session_metadata(
                    session_id,
                    {
                        "channel_delivery": {
                            "channel_type": ChannelType.FEISHU.value,
                            "chat_id": delivery_chat_id,
                            "channel_instance_id": instance_id,
                            "enabled": True,
                            "send_on_success": True,
                        }
                    },
                )
            except Exception as e:
                logger.warning(f"[Feishu] Failed to persist channel delivery metadata: {e}")
            if persona_metadata:
                try:
                    from src.infra.session.manager import SessionManager
                    from src.kernel.schemas.session import SessionUpdate

                    await SessionManager().update_session(
                        session_id,
                        SessionUpdate(metadata=persona_metadata),
                    )
                except Exception as e:
                    logger.warning(f"[Feishu] Failed to persist persona metadata: {e}")

            logger.info(f"[Feishu] Task submitted: session={session_id}, run_id={run_id}")

            # 消费事件流：把 agent 产出实时驱动到收集器（流式卡片/工具/审批/文件）。
            await _process_events(
                collector=collector,
                session_id=session_id,
                run_id=run_id,
                show_tools=show_tools,
            )

            # 收尾：优先定稿流式卡片；若本次未走流式，则整卡发送一次；最后补发文件。
            streamed = await collector.finalize_stream_message()
            if not streamed:
                await collector.send_card_message()
            await collector.upload_and_send_files()

            logger.info(f"[Feishu] Message processing completed for {chat_id}")

        except Exception as e:
            # 出错时尽量给用户一条错误回执（失败也不再抛出）。
            logger.error(f"[Feishu] Error handling message: {e}", exc_info=True)
            try:
                await manager.send_message(
                    user_id,
                    delivery_chat_id,
                    f"❌ 处理消息时发生错误: {str(e)[:200]}",
                    instance_id,
                )
            except Exception:
                pass
        finally:
            # 无论成败，都移除最初那枚"已收到"表情，表示处理已结束。
            if original_message_id and received_reaction_id:
                try:
                    await manager.delete_reaction(
                        user_id,
                        original_message_id,
                        received_reaction_id,
                        instance_id,
                    )
                except Exception as e:
                    logger.debug(f"[Feishu] Failed to remove received reaction: {e}")

    return feishu_message_handler


async def setup_feishu_handler(
    default_agent: str,
    show_tools: bool = True,
) -> None:
    """
    设置飞书消息处理器

    Args:
        default_agent: 默认 Agent ID
        show_tools: 是否显示工具调用
    """
    from src.infra.channel.feishu import get_feishu_channel_manager, start_feishu_channels

    manager = get_feishu_channel_manager()
    handler = create_feishu_message_handler(
        manager=manager,
        default_agent=default_agent,
        show_tools=show_tools,
    )

    await start_feishu_channels(handler)
    logger.info("Feishu channels started")

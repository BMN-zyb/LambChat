"""
Feishu 消息处理器模块

处理飞书消息的 Agent 执行和响应。
发送一条卡片消息，支持 markdown 渲染。
"""

import asyncio
import json
import sys
import time
from typing import Any, AsyncGenerator, Callable, Optional, cast

from src.infra.channel.feishu.channel import FeishuChannel
from src.infra.channel.feishu.manager import FeishuChannelManager
from src.infra.channel.feishu.markdown import FeishuMarkdownAdapter
from src.infra.logging import get_logger
from src.infra.utils.datetime import utc_now

logger = get_logger(__name__)

# Redis key prefix for Feishu chat session mapping
FEISHU_SESSION_KEY_PREFIX = "feishu:session:"

# 事件类型定义
EVENT_MESSAGE_CHUNK = "message:chunk"
EVENT_THINKING = "thinking"
EVENT_TOOL_START = "tool:start"
EVENT_TOOL_RESULT = "tool:result"
EVENT_DONE = "done"
FEISHU_STREAM_UPDATE_DEBOUNCE_SECONDS = 0.12
FEISHU_STREAM_FIRST_PAINT_CHARS = 12


async def _get_feishu_session_id(chat_id: str) -> str:
    """获取飞书聊天对应的当前 session ID，如果不存在则创建默认的"""
    from src.infra.storage.redis import RedisStorage

    storage = RedisStorage()
    key = f"{FEISHU_SESSION_KEY_PREFIX}{chat_id}"
    session_id = await storage.get(key)

    if session_id is None:
        # 默认使用 chat_id 作为 session ID（兼容旧数据）
        session_id = f"feishu_{chat_id}"
        await storage.set(key, session_id)

    return session_id


async def _create_new_feishu_session(chat_id: str) -> str:
    """为飞书聊天创建新的 session ID"""
    from src.infra.storage.redis import RedisStorage

    storage = RedisStorage()
    key = f"{FEISHU_SESSION_KEY_PREFIX}{chat_id}"

    # 使用时间戳生成唯一的 session ID
    timestamp = int(time.time())
    session_id = f"feishu_{chat_id}_{timestamp}"

    # 存储到 Redis，不设置过期时间
    await storage.set(key, session_id)

    logger.info(f"[Feishu] Created new session for chat {chat_id}: {session_id}")
    return session_id


class FeishuResponseCollector:
    """
    飞书响应收集器

    收集 Agent 响应内容，发送一条美观的 markdown 卡片消息。
    """

    def __init__(
        self,
        manager: "FeishuChannelManager",
        user_id: str,
        chat_id: str,
        reply_to_message_id: str | None = None,
        sender_id: str | None = None,
        chat_type: str | None = None,
        stream_reply: bool = True,
        instance_id: str | None = None,
    ):
        self.manager = manager
        self.user_id = user_id
        self.chat_id = chat_id
        self.reply_to_message_id = reply_to_message_id
        self.sender_id = sender_id
        self.chat_type = chat_type
        self.stream_reply = stream_reply
        self.instance_id = instance_id

        # 内容收集
        self.text_parts: list[str] = []
        self.tools_used: list[str] = []
        self.files_to_reveal: list[dict] = []
        self._sent_file_keys: set[str] = set()

        # 处理中 emoji 控制
        self._processing_message_id: str | None = None
        self._processing_reaction_id: str | None = None
        self._stream_card_id: str | None = None
        self._stream_message_id: str | None = None
        self._stream_sequence = 0
        self._stream_failed = False
        self._stream_finalized = False
        self._stream_lock = asyncio.Lock()
        self._stream_update_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._stream_update_task: asyncio.Task | None = None
        self._stream_last_pushed_content = ""

    def append_text(self, chunk: str) -> None:
        """追加文本内容"""
        self.text_parts.append(chunk)

    async def append_stream_chunk(self, chunk: str) -> None:
        """Append one response chunk and push it to a Feishu streaming card when enabled."""
        self.append_text(chunk)
        if not self.stream_reply or self._stream_failed or self._stream_finalized:
            return

        if self._stream_card_id:
            self._ensure_stream_update_worker()
            self._stream_update_queue.put_nowait("".join(self.text_parts))
            return

        initialized = False
        initial_content = self._first_paint_content("".join(self.text_parts))
        async with self._stream_lock:
            if self._stream_failed or self._stream_finalized:
                return
            client = self._get_client()
            if not client:
                self._stream_failed = True
                return

            if not self._stream_card_id:
                card_id = await client.create_stream_card(initial_content)
                if not card_id:
                    self._stream_failed = True
                    return
                sent, message_id = await client.send_card_by_id(
                    self.chat_id,
                    card_id,
                    reply_to_id=self.reply_to_message_id,
                )
                if not sent:
                    self._stream_failed = True
                    return
                self._stream_card_id = card_id
                self._stream_message_id = message_id
                self._stream_last_pushed_content = initial_content
                initialized = True

        self._ensure_stream_update_worker()
        content = "".join(self.text_parts)
        if initialized:
            if initial_content != content:
                self._stream_update_queue.put_nowait(content)
        else:
            self._stream_update_queue.put_nowait(content)

    def _first_paint_content(self, content: str) -> str:
        """Return a tiny first update so Feishu starts typewriter rendering quickly."""
        stripped = content.strip()
        if not stripped:
            return content
        if len(stripped) <= FEISHU_STREAM_FIRST_PAINT_CHARS:
            return content
        return stripped[:FEISHU_STREAM_FIRST_PAINT_CHARS]

    def _ensure_stream_update_worker(self) -> None:
        if self._stream_update_task and not self._stream_update_task.done():
            return
        self._stream_update_task = asyncio.create_task(self._stream_update_worker())
        self._stream_update_task.add_done_callback(self._on_stream_update_task_done)

    def _on_stream_update_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as e:
            self._stream_failed = True
            logger.warning("[Feishu] Stream update worker failed: %s", e, exc_info=True)

    async def _stream_update_worker(self) -> None:
        first_update = True
        while True:
            content = await self._stream_update_queue.get()
            if content is None:
                return

            if not first_update:
                await asyncio.sleep(FEISHU_STREAM_UPDATE_DEBOUNCE_SECONDS)
                while True:
                    try:
                        next_content = self._stream_update_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if next_content is None:
                        return
                    content = next_content
            first_update = False

            if content == self._stream_last_pushed_content:
                continue

            async with self._stream_lock:
                if self._stream_failed or self._stream_finalized or not self._stream_card_id:
                    return
                client = self._get_client()
                if not client:
                    self._stream_failed = True
                    return
                self._stream_sequence += 1
                success = await client.update_stream_card(
                    self._stream_card_id,
                    content,
                    self._stream_sequence,
                )
                if not success:
                    self._stream_failed = True
                    return
                self._stream_last_pushed_content = content

    async def _cancel_stream_update_worker(self) -> None:
        task = self._stream_update_task
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def add_tool(self, tool_name: str) -> None:
        """添加使用的工具"""
        if tool_name:
            self.tools_used.append(tool_name)

    def add_file_to_reveal(self, file_info: dict) -> None:
        """添加待展示的文件"""
        self.files_to_reveal.append(file_info)

    async def start_processing_indicator(self, message_id: str) -> None:
        """发送一次处理中 emoji 指示器。"""
        if self._processing_reaction_id:
            return
        reaction_id = await self.manager.add_reaction(
            self.user_id,
            message_id,
            FeishuChannel.PROCESSING_EMOJI,
            self.instance_id,
        )
        if reaction_id:
            self._processing_message_id = message_id
            self._processing_reaction_id = reaction_id

    async def stop_processing_indicator(self) -> None:
        """移除处理中 emoji 指示器。"""
        if not self._processing_message_id or not self._processing_reaction_id:
            return
        message_id = self._processing_message_id
        reaction_id = self._processing_reaction_id
        self._processing_message_id = None
        self._processing_reaction_id = None
        try:
            await self.manager.delete_reaction(
                self.user_id,
                message_id,
                reaction_id,
                self.instance_id,
            )
        except Exception as e:
            logger.debug(f"[Feishu] Processing emoji error: {e}")

    async def _upload_image_from_uri(self, uri: str) -> str | None:
        """从 send:// URI 读取图片并上传到飞书，返回 image_key。"""
        from src.infra.storage.s3.service import get_or_init_storage

        base_client = self.manager._find_channel(self.user_id, self.instance_id)
        if not base_client:
            return None
        client = cast(FeishuChannel, base_client)

        try:
            # send:// URI maps to S3 key path
            s3_key = uri.replace("send://", "")
            storage = await get_or_init_storage()
            backend = storage._get_backend()
            image_bytes = await backend.download(s3_key)
            if not image_bytes:
                return None
            return await client.upload_image(image_bytes)
        except Exception as e:
            logger.debug(f"[Feishu] Failed to upload image from URI {uri}: {e}")
            return None

    def _get_client(self) -> "FeishuChannel | None":
        base_client = self.manager._find_channel(self.user_id, self.instance_id)
        if not base_client:
            logger.warning(f"[Feishu] No client for user {self.user_id}")
            return None
        return cast(FeishuChannel, base_client)

    async def finalize_stream_message(self) -> bool:
        """Close the streaming card. Returns True when the reply was streamed."""
        if not self._stream_card_id or self._stream_failed or self._stream_finalized:
            return False

        await self._cancel_stream_update_worker()
        async with self._stream_lock:
            if not self._stream_card_id or self._stream_failed or self._stream_finalized:
                return False
            client = self._get_client()
            if not client:
                return False
            final_text = "".join(self.text_parts).strip() or " "
            self._stream_sequence += 1
            success = await client.finalize_stream_card(
                self._stream_card_id,
                final_text,
                self._stream_sequence,
            )
            self._stream_finalized = success
            return success

    async def send_card_message(self) -> bool:
        """发送卡片消息（支持回复引用、图片嵌入）"""
        if self._stream_finalized:
            return True

        client = self._get_client()
        if not client:
            return False

        content = await self._build_card_content_async(client)
        success = await client.send_card_message(
            self.chat_id, content, reply_to_id=self.reply_to_message_id
        )
        if success:
            reply_info = (
                f" (reply to {self.reply_to_message_id})" if self.reply_to_message_id else ""
            )
            logger.info(f"[Feishu] Card message sent to {self.chat_id}{reply_info}")
        else:
            logger.warning("[Feishu] Failed to send card message")
        return success

    async def _build_card_content_async(self, client: "FeishuChannel") -> str:
        """构建飞书卡片消息内容（异步，支持图片上传嵌入）"""
        elements: list[dict[str, Any]] = []

        # ===== @mention（群聊回复时 @原发送者）=====
        if self.chat_type == "group" and self.sender_id:
            elements.append(
                {
                    "tag": "markdown",
                    "content": f'<at user_id="{self.sender_id}"></at>',
                }
            )

        # ===== 主要内容区域 =====
        if self.text_parts:
            raw_content = "".join(self.text_parts)
            # 使用带图片上传的适配器构建 elements
            elements.extend(
                await FeishuMarkdownAdapter.build_elements_with_images(
                    raw_content, self._upload_image_from_uri
                )
            )

        # ===== 元数据区域（工具 + 文件）=====
        metadata_parts = []

        if self.tools_used:
            unique_tools = list(dict.fromkeys(self.tools_used))
            tool_badges = " ".join(f"`{t}`" for t in unique_tools)
            metadata_parts.append(f"🔧 {tool_badges}")

        if self.files_to_reveal:
            file_names = [f.get("name", "未知文件") for f in self.files_to_reveal]
            metadata_parts.append(f"📎 {', '.join(file_names)}")

        if metadata_parts:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": " · ".join(metadata_parts)})

        if not elements:
            elements.append({"tag": "div", "text": {"tag": "plain_text", "content": "(无内容)"}})

        card = {"config": {"wide_screen_mode": True}, "elements": elements}
        return json.dumps(card, ensure_ascii=False)

    async def upload_and_send_files(self) -> None:
        """上传文件并发送文件卡片

        直接从 S3 storage 读取文件内容，然后上传到飞书。
        """
        from src.infra.storage.s3.service import get_or_init_storage

        if not self.files_to_reveal:
            return

        base_client = self.manager._find_channel(self.user_id, self.instance_id)
        if not base_client:
            logger.warning(f"[Feishu] No client for user {self.user_id}")
            return

        client = cast(FeishuChannel, base_client)

        try:
            storage = await get_or_init_storage()
        except Exception as e:
            logger.error(f"[Feishu] Failed to init storage: {e}")
            return

        for file_info in self.files_to_reveal:
            try:
                file_name = file_info.get("name", "unknown")
                file_key = file_info.get("key", "")

                if not file_key:
                    logger.warning(f"[Feishu] No key for file {file_name}")
                    continue
                if file_key in self._sent_file_keys:
                    continue

                logger.info(f"[Feishu] Reading file {file_name} from storage, key={file_key}")

                backend = storage._get_backend()
                file_data = await backend.download(file_key)
                if not file_data:
                    logger.warning(f"[Feishu] File not found or empty: {file_key}")
                    continue

                logger.info(f"[Feishu] Read file {file_name}, size: {len(file_data)} bytes")

                feishu_file_key = await client.upload_bytes(
                    file_data=file_data,
                    file_name=file_name,
                )
                if feishu_file_key:
                    sent = await client.send_file_by_key(
                        chat_id=self.chat_id,
                        file_key=feishu_file_key,
                        file_name=file_name,
                        reply_to_id=self.reply_to_message_id,
                    )
                    if sent:
                        self._sent_file_keys.add(file_key)
                        logger.info(f"[Feishu] Sent file: {file_name}")
                    else:
                        logger.warning(f"[Feishu] Failed to send file {file_name} to Feishu")
                else:
                    logger.warning(f"[Feishu] Failed to upload file {file_name} to Feishu")
            except Exception as e:
                logger.error(f"[Feishu] Failed to upload file {file_info.get('name')}: {e}")


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
) -> AsyncGenerator[dict[str, Any], None]:
    """执行 Agent 并生成事件流"""
    from src.agents.core.base import AgentFactory
    from src.infra.task.exceptions import TaskInterruptedError

    agent = await AgentFactory.get(agent_id)
    run_id = presenter.run_id if presenter else None

    try:
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
        ):
            yield event
    except (asyncio.CancelledError, TaskInterruptedError):
        if run_id:
            await agent.close(run_id)
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

        try:
            logger.info(
                f"[Feishu] Processing message from {sender_id} for user {user_id}: {content[:50]}..."
            )

            original_message_id = metadata.get("message_id")
            sender_id_from_msg = metadata.get("sender_id")
            chat_type_from_msg = metadata.get("chat_type")
            instance_id = metadata.get("instance_id")
            delivery_chat_id = chat_id
            reply_to_message_id = original_message_id
            if chat_type_from_msg == "p2p":
                delivery_chat_id = metadata.get("reply_chat_id") or chat_id
            attachments = metadata.get("attachments")

            # 处理 /new 命令 - 严格匹配
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
            session_id = await _get_feishu_session_id(chat_id)
            task_manager = get_task_manager()

            # Resolve agent, model & project: use per-channel config if available
            agent_to_use = default_agent
            model_id: str | None = None
            project_id: str | None = None
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
                    persona_preset_id = ch_config.get("persona_preset_id")
                    channel_name = ch_config.get("name")
                    stream_reply = bool(ch_config.get("stream_reply", True))

            if persona_preset_id:
                try:
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
            ):
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
                ):
                    yield event

            # Use time-based session title for Feishu
            session_title = utc_now().strftime("%Y-%m-%d %H:%M")

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
            )
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

            await _process_events(
                collector=collector,
                session_id=session_id,
                run_id=run_id,
                show_tools=show_tools,
            )

            streamed = await collector.finalize_stream_message()
            if not streamed:
                await collector.send_card_message()
            await collector.upload_and_send_files()

            logger.info(f"[Feishu] Message processing completed for {chat_id}")

        except Exception as e:
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

    return feishu_message_handler


async def _process_events(
    collector: FeishuResponseCollector,
    session_id: str,
    run_id: str,
    show_tools: bool,
) -> None:
    """处理事件流并收集响应"""
    from src.infra.session.dual_writer import get_dual_writer

    dual_writer = get_dual_writer()

    try:
        async for event in dual_writer.read_from_redis(session_id, run_id):
            event_type = event.get("event_type", "")
            data = event.get("data", {})

            if event_type == EVENT_MESSAGE_CHUNK:
                chunk = data.get("content", "")
                if chunk:
                    await collector.append_stream_chunk(chunk)

            elif event_type == EVENT_TOOL_START and show_tools:
                tool_name = data.get("tool", "")
                if tool_name:
                    collector.add_tool(tool_name)

            elif event_type == EVENT_TOOL_RESULT:
                tool_name = data.get("tool", "")
                logger.debug(f"[Feishu] tool:result event: tool={tool_name}")
                if tool_name == "reveal_file":
                    result = data.get("result", "")
                    logger.info(f"[Feishu] reveal_file result type={type(result).__name__}")
                    if isinstance(result, str) and result:
                        try:
                            file_info = json.loads(result)
                            if (
                                isinstance(file_info, dict)
                                and "key" in file_info
                                and "name" in file_info
                            ):
                                collector.add_file_to_reveal(file_info)
                                await collector.upload_and_send_files()
                                logger.info(
                                    f"[Feishu] Added file to reveal: {file_info.get('name')}"
                                )
                        except json.JSONDecodeError as e:
                            logger.warning(f"[Feishu] Failed to parse reveal_file result: {e}")
                    elif isinstance(result, dict):
                        if "key" in result and "name" in result:
                            collector.add_file_to_reveal(result)
                            await collector.upload_and_send_files()
                            logger.info(
                                f"[Feishu] Added file to reveal (dict): {result.get('name')}"
                            )

            elif event_type in ("done", "complete", "error"):
                break

        logger.info(f"[Feishu] Event processing completed for session={session_id}")

    except Exception as e:
        logger.error(f"[Feishu] Event processing error: {e}", exc_info=True)


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

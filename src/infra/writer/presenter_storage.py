"""Presenter 事件存储 mixin (Redis + MongoDB)

处理 trace 创建、事件持久化、token usage 保证和 trace 完成。
"""
# 中文补充说明：本文件是 Presenter 与持久化层（DualEventWriter，统一写
# Redis + MongoDB）之间的对接点，也是"事件落库"这条链路上最容易出 bug 的地方。
# 两大难点：
#   1）幂等性——同一个 trace 的 done/goal:end/token:usage 等终态事件，
#      在重试或多次调用下不能被重复写入，因此用一组 _xxx_recorded 布尔标记
#      做"至多写一次"的保护；
#   2）LangSmith 元数据脱敏与限流——构建 LangSmith trace metadata 时，
#      运行时 context 中可能混入 API Key / token 等敏感字段，也可能包含
#      超长文本或超大列表，本文件提供了一整套 _sanitize_metadata_value /
#      _bounded_string / _bounded_list 等工具，在数据进入 LangSmith 之前
#      做"过滤敏感字段 + 限制长度/条目数"的处理。

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Dict

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger

if TYPE_CHECKING:
    from src.infra.session.dual_writer import DualEventWriter
    from src.infra.writer.presenter_config import PresenterConfig

logger = get_logger(__name__)

# 中文：以下常量用于约束写入 LangSmith 的 trace metadata 的规模——
# LangSmith UI/存储对超长字符串、超大列表不友好，这里统一做上限截断
LANGSMITH_PREVIEW_CHARS = 500
LANGSMITH_LIST_LIMIT = 25
LANGSMITH_ATTACHMENT_LIMIT = 10
LANGSMITH_TEAM_MEMBER_LIMIT = 20
# 中文：元数据字段名命中以下关键词时视为敏感信息（密钥/口令/凭证等），
# 会在 _is_sensitive_key / _sanitize_metadata_value 中被过滤，
# 防止用户环境变量、模型配置等敏感值意外写入可被多人查看的 LangSmith trace
SENSITIVE_METADATA_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
}
# 中文：允许直接记录到 trace metadata 的模型调用参数白名单
# （这些参数不敏感，且对排查生成效果问题很有价值）
MODEL_PARAMETER_KEYS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_output_tokens",
    "presence_penalty",
    "frequency_penalty",
    "reasoning_effort",
    "enable_thinking",
}


def _is_sensitive_key(key: str) -> bool:
    # 中文：字段名判定采用多种归一化/拆词策略以提高命中率——
    #   1）整体归一化后精确匹配 SENSITIVE_METADATA_KEYS；
    #   2）按下划线拆词后看是否包含 secret/password/authorization 片段
    #      （例如 "db_password"、"oauth_secret_key" 这类复合命名）；
    #   3）以 _api_key / _token 结尾的字段名（例如 "openai_api_key"）。
    # 任一条件命中即视为敏感字段，从元数据中剔除
    normalized = key.lower().replace("-", "_")
    if normalized in SENSITIVE_METADATA_KEYS:
        return True
    parts = [part for part in normalized.split("_") if part]
    if "secret" in parts or "password" in parts or "authorization" in parts:
        return True
    if normalized.endswith("_api_key") or normalized.endswith("_token"):
        return True
    return False


def _bounded_string(value: Any, *, limit: int = LANGSMITH_PREVIEW_CHARS) -> str:
    # 把任意值转成字符串并做长度截断，附带说明原始长度，方便定位是否被截断
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated from {len(text)} chars]"


def _bounded_list(value: Any, *, limit: int = LANGSMITH_LIST_LIMIT) -> list[Any]:
    # 中文：统一把"可能是 None / 字符串 / 序列 / 单个值"的输入规整成列表，
    # 并按 limit 截断，供后续需要以列表形式处理的场景使用
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return list(value[:limit])
    return [value]


def _sanitize_metadata_value(value: Any, *, depth: int = 0) -> Any:
    # 中文：递归清理任意结构的元数据值——
    #   - 深度超过 3 层直接转字符串兜底，防止超深嵌套结构处理耗时或栈溢出；
    #   - dict 类型会先过滤掉敏感 key（_is_sensitive_key），再递归处理其余值；
    #   - 序列类型按 LANGSMITH_LIST_LIMIT 截断后递归处理每个元素；
    #   - 其余类型（如自定义对象）转字符串兜底。
    if depth > 3:
        return _bounded_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_string(value)
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                continue
            sanitized[key_text] = _sanitize_metadata_value(item, depth=depth + 1)
        return sanitized
    if isinstance(value, Sequence):
        return [
            _sanitize_metadata_value(item, depth=depth + 1) for item in value[:LANGSMITH_LIST_LIMIT]
        ]
    return _bounded_string(value)


def _preview_payload(value: Any) -> dict[str, Any]:
    # 中文：把一段可能很长的文本转换为"预览摘要"结构（预览片段 + 原始长度 +
    # 实际预览字符数），既能让排查者看到内容概貌，又不会把全文塞进 metadata
    text = "" if value is None else str(value)
    return {
        "preview": _bounded_string(text),
        "length": len(text),
        "preview_chars": min(len(text), LANGSMITH_PREVIEW_CHARS),
    }


def _build_attachment_summary(attachments: Any) -> dict[str, Any]:
    # 中文：只提炼附件的"元信息摘要"（文件名/类型/大小/是否有 key），
    # 不包含附件的实际存储 key 或内容，避免把可用于定位/下载真实文件的
    # 敏感信息写进 LangSmith metadata；has_key 只是一个布尔标记
    items = []
    raw_items = _bounded_list(attachments, limit=LANGSMITH_ATTACHMENT_LIMIT)
    for attachment in raw_items:
        if not isinstance(attachment, Mapping):
            continue
        name = attachment.get("name") or attachment.get("filename") or attachment.get("file_name")
        mime_type = (
            attachment.get("type") or attachment.get("mime_type") or attachment.get("content_type")
        )
        summary: dict[str, Any] = {}
        if name:
            summary["name"] = _bounded_string(name, limit=120)
        if mime_type:
            summary["type"] = _bounded_string(mime_type, limit=120)
        if attachment.get("size") is not None:
            summary["size"] = attachment.get("size")
        summary["has_key"] = bool(attachment.get("key"))
        items.append(summary)
    return {
        # count 优先反映原始附件总数（即使超过展示上限也如实统计），
        # 只有当 attachments 不是可数序列时才退化为已截断后的条目数
        "count": len(attachments)
        if isinstance(attachments, Sequence) and not isinstance(attachments, str)
        else len(raw_items),
        "items": items,
        "truncated": len(raw_items) >= LANGSMITH_ATTACHMENT_LIMIT,
    }


def _build_team_member_summary(members: Any) -> list[dict[str, Any]]:
    # 中文：只保留团队成员中排查问题时有用的字段（角色、绑定的 agent/model 等），
    # 并复用 _sanitize_metadata_value 再过滤一遍潜在的敏感字段
    summaries = []
    for member in _bounded_list(members, limit=LANGSMITH_TEAM_MEMBER_LIMIT):
        if not isinstance(member, Mapping):
            continue
        summary = {
            key: member.get(key)
            for key in ("member_id", "role_name", "agent_id", "model_id", "enabled")
            if member.get(key) is not None
        }
        role_tags = member.get("role_tags")
        if role_tags:
            summary["role_tags"] = _bounded_list(role_tags, limit=10)
        summaries.append(_sanitize_metadata_value(summary))
    return summaries


def _build_runtime_metadata(context: Mapping[str, Any] | None) -> Dict[str, Any]:
    # 中文：把一次对话运行时的上下文（agent_options、启用的 skills/tools、
    # 人设 system_prompt、附件、团队成员等）压缩、脱敏成一份精简的 metadata，
    # 附着到 LangSmith run 上，方便排查"这次运行到底用了什么配置"。
    # 每个字段都是可选的——context 中没有对应 key 时就不写入 metadata，
    # 避免产生大量空字段噪音。
    if not context:
        return {}

    metadata: Dict[str, Any] = {}
    passthrough_keys = (
        "team_id",
        "base_url",
    )
    for key in passthrough_keys:
        value = context.get(key)
        if value:
            metadata[key] = _sanitize_metadata_value(value)

    agent_options = context.get("agent_options")
    if isinstance(agent_options, Mapping):
        model = agent_options.get("model")
        model_id = agent_options.get("model_id")
        if model:
            metadata["model"] = _sanitize_metadata_value(model)
        if model_id:
            metadata["model_id"] = _sanitize_metadata_value(model_id)
        # 只挑选白名单内的模型调用参数（temperature 等），忽略其余字段
        model_parameters = {
            key: agent_options[key]
            for key in MODEL_PARAMETER_KEYS
            if key in agent_options and agent_options[key] is not None
        }
        if model_parameters:
            metadata["model_parameters"] = _sanitize_metadata_value(model_parameters)

    skills: dict[str, Any] = {}
    if context.get("enabled_skills") is not None:
        skills["enabled"] = _sanitize_metadata_value(_bounded_list(context.get("enabled_skills")))
    if context.get("disabled_skills") is not None:
        skills["disabled"] = _sanitize_metadata_value(_bounded_list(context.get("disabled_skills")))
    if skills:
        metadata["skills"] = skills

    if context.get("disabled_tools") is not None:
        metadata["tools"] = {
            "disabled": _sanitize_metadata_value(_bounded_list(context.get("disabled_tools")))
        }

    if context.get("disabled_mcp_tools") is not None:
        metadata["mcp_tools"] = {
            "disabled": _sanitize_metadata_value(_bounded_list(context.get("disabled_mcp_tools")))
        }

    # 人设系统提示词可能很长，这里只记录"是否启用 + 长度 + 预览片段"，
    # 不把完整提示词原文写进 metadata
    persona_prompt = context.get("persona_system_prompt")
    metadata["persona"] = {
        "enabled": bool(persona_prompt),
        "prompt_length": len(persona_prompt) if isinstance(persona_prompt, str) else 0,
        "prompt_preview_chars": min(
            len(persona_prompt) if isinstance(persona_prompt, str) else 0,
            LANGSMITH_PREVIEW_CHARS,
        ),
    }
    if persona_prompt:
        metadata["persona"]["prompt_preview"] = _bounded_string(persona_prompt)

    if context.get("attachments") is not None:
        metadata["attachments"] = _build_attachment_summary(context.get("attachments"))

    if context.get("active_goal") is not None:
        metadata["active_goal"] = _sanitize_metadata_value(context.get("active_goal"))

    if context.get("recommendation_input") is not None:
        metadata["recommendation_input"] = _preview_payload(context.get("recommendation_input"))

    # team_members / members 是历史上两个不同的字段名，两者兼容取值
    team_members = context.get("team_members") or context.get("members")
    if team_members:
        metadata["team_members"] = _build_team_member_summary(team_members)

    return metadata


class StoragePresenterMixin:
    """事件存储 mixin —— 需要 self.config / self._dual_writer / self.trace_id 等属性。"""

    # Attributes provided by the Presenter host class
    config: PresenterConfig
    trace_id: str
    run_id: str
    _step_count: int
    _tool_calls: list[dict[str, Any]]
    _dual_writer: DualEventWriter | None
    # 中文：下面这组布尔标记都是"至多生效一次"的幂等保护——
    # 避免因重试、多次调用 complete()/save_event() 而重复创建 trace、
    # 重复写入 done/goal:end 事件，或重复记录 token 用量
    _trace_created: bool
    _done_recorded: bool
    _goal_end_recorded: bool
    _completed: bool
    _token_usage_recorded: bool

    # ------------------------------------------------------------------
    # DualWriter 获取
    # ------------------------------------------------------------------

    async def _get_dual_writer(self):
        """延迟获取 DualEventWriter"""
        # 中文：懒加载 + 缓存单例引用，且用 try/except 兜底——
        # DualEventWriter 初始化依赖 Redis/MongoDB 连接，若基础设施未就绪，
        # 这里降级为 None，调用方需要据此跳过存储相关操作而不是让整个请求失败
        if self._dual_writer is None:
            try:
                from src.infra.session.dual_writer import get_dual_writer

                self._dual_writer = get_dual_writer()
                logger.debug("dual_writer initialized: %s", self._dual_writer is not None)
            except Exception as e:
                logger.warning("Failed to init dual_writer: %s", e)
        return self._dual_writer

    # ------------------------------------------------------------------
    # Trace 元数据
    # ------------------------------------------------------------------

    async def _build_identity_metadata(self) -> Dict[str, Any]:
        """Build non-sensitive user identity metadata for tracing systems."""
        metadata: Dict[str, Any] = {}

        if not self.config.user_id:
            return metadata

        metadata["user_id"] = self.config.user_id

        try:
            # 只额外附加 username 这类非敏感的展示信息，不附加完整用户对象
            # （避免把用户的其它字段，例如邮箱、加密密码哈希等意外带入 trace）
            from src.infra.user.storage import UserStorage

            user = await UserStorage().get_by_id(self.config.user_id)
            username = getattr(user, "username", None) if user else None
            if username:
                metadata["username"] = username
        except Exception as e:
            logger.debug("Failed to enrich trace metadata for user %s: %s", self.config.user_id, e)

        return metadata

    async def _build_trace_metadata(self) -> Dict[str, Any]:
        """Build trace metadata, enriching it with non-sensitive user identity when available."""
        # 中文：这是"创建 trace 时"使用的精简元数据，与下面
        # build_langsmith_metadata（挂到 LangSmith run 上的完整元数据）不同用途
        metadata: Dict[str, Any] = {
            "agent_name": self.config.agent_name,
        }
        metadata.update(await self._build_identity_metadata())
        return metadata

    async def build_langsmith_metadata(
        self,
        context: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Build metadata that should be attached to LangSmith runs."""
        metadata: Dict[str, Any] = {
            "session_id": self.config.session_id,
            "agent_id": self.config.agent_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
        }
        metadata.update(await self._build_identity_metadata())
        if self.config.agent_name:
            metadata["agent_name"] = self.config.agent_name
        # 融合本次运行时上下文（模型参数、启用的 skills/tools、人设、附件、
        # 团队成员等），经过脱敏/裁剪后一并挂到 LangSmith run 上
        metadata.update(_build_runtime_metadata(context))
        return metadata

    # ------------------------------------------------------------------
    # Trace 生命周期
    # ------------------------------------------------------------------

    async def _ensure_trace(self):
        """确保 trace 已创建"""
        # 幂等保护：同一个 Presenter 实例只会真正创建一次 trace
        if self._trace_created:
            return

        dual_writer = await self._get_dual_writer()
        if not dual_writer:
            logger.debug("_ensure_trace: dual_writer is None, skipping")
            return

        # 如果没有 session_id，跳过 trace 创建
        # 中文：trace 依附于具体的会话（session）存在，没有 session_id
        # （例如某些不落库的临时/测试调用场景）就没有必要创建 trace 记录
        if not self.config.session_id:
            logger.debug(
                "_ensure_trace: no session_id (config.session_id=%s), skipping",
                self.config.session_id,
            )
            return

        try:
            logger.debug(
                "Creating trace: trace_id=%s, session_id=%s",
                self.trace_id,
                self.config.session_id,
            )
            metadata = await self._build_trace_metadata()
            await dual_writer.create_trace(
                trace_id=self.trace_id,
                session_id=self.config.session_id,
                agent_id=self.config.agent_id,
                run_id=self.run_id,
                user_id=self.config.user_id,
                metadata=metadata,
            )
            self._trace_created = True
            logger.debug("Trace created successfully: %s", self.trace_id)
        except Exception as e:
            # 创建失败只记录警告，不向上抛出：事件展示不应该因为存储失败而中断
            logger.warning("Failed to create trace: %s", e)

    # ------------------------------------------------------------------
    # 事件存储
    # ------------------------------------------------------------------

    async def save_event(self, event: Dict[str, Any]) -> None:
        """
        保存 SSE 事件到 Redis + MongoDB (按 trace 聚合)

        Args:
            event: SSE 事件字典，包含 event 和 data 字段
        """
        # enable_storage=False 的场景（如某些一次性/调试性调用）完全不落库
        if not self.config.enable_storage:
            return

        try:
            # 惰性确保 trace 存在（第一次调用时才真正创建）
            await self._ensure_trace()

            event_type = event.get("event", "unknown")
            # 中文：done / goal:end 是"终态事件"，只允许写入一次；
            # 如果因为重试等原因被再次调用，直接跳过，防止重复记录
            if event_type == "done" and self._done_recorded:
                return
            if event_type == "goal:end" and self._goal_end_recorded:
                return
            data = event.get("data", {})

            # 如果 data 是字符串（旧格式或外部传入），需要解析并清理
            # 如果是 dict（来自优化后的 _build_event），已经 sanitize 过，直接使用
            if isinstance(data, str):
                try:
                    data = await run_blocking_io(json.loads, data)
                except json.JSONDecodeError:
                    data = {"raw": data}
                data = self._sanitize_for_json(data)  # type: ignore[attr-defined]

            dual_writer = await self._get_dual_writer()
            if dual_writer and self.config.session_id:
                # 中文：在写入 done 事件之前，必须保证已经有一条 token:usage 事件
                # 落库（哪怕用量是 0），否则这次运行在统计/计费视角上会"查无用量"
                if event_type == "done":
                    await self._ensure_token_usage_event()
                await dual_writer.write_event(
                    session_id=self.config.session_id,
                    event_type=event_type,
                    data=data,
                    trace_id=self.trace_id,
                    agent_id=self.config.agent_id,
                    run_id=self.run_id,
                )
                # 写入成功后才更新对应的幂等标记，确保"写入失败"不会被误判为"已记录"
                if event_type == "token:usage":
                    self._token_usage_recorded = True
                elif event_type == "goal:end":
                    self._goal_end_recorded = True
                elif event_type == "done":
                    self._done_recorded = True
        except Exception as e:
            # 存储失败不应该影响前端已经收到的 SSE 事件展示，仅记录警告
            logger.warning("Failed to save event: %s", e)

    async def _ensure_token_usage_event(self) -> None:
        """Persist a token usage event before terminal trace status, even if usage is zero."""
        if self._token_usage_recorded or not self.config.enable_storage:
            return
        if not self.config.session_id:
            return

        # 中文：即使本次运行没有产生任何 usage 统计，也主动构造并保存一条
        # 全零的 token:usage 事件，保证每个 trace 都有确定的用量数据点可查
        await self.save_event(self.present_token_usage())  # type: ignore[attr-defined]

    async def complete(self, status: str = "completed") -> None:
        """
        标记 trace 完成

        应该在流结束时调用此方法。
        会先刷新 MongoDB 写入缓冲，确保所有事件已持久化。

        Args:
            status: 完成状态 (completed/error)
        """
        # 幂等保护：同一个 Presenter 实例只允许真正 complete 一次
        if self._completed:
            return

        dual_writer = await self._get_dual_writer()
        if dual_writer and self.config.session_id:
            try:
                await self._ensure_token_usage_event()
                # 先刷新 MongoDB 缓冲，确保所有事件已写入
                # 中文：dual_writer 内部对 MongoDB 的写入可能是批量/异步缓冲的，
                # 必须先强制 flush，否则标记 trace 完成时可能有事件还没真正落库
                await dual_writer.flush_mongo_buffer(require_empty=True)
                await dual_writer.complete_trace(
                    trace_id=self.trace_id,
                    status=status,
                    metadata={
                        "step_count": self._step_count,
                        "tool_calls": len(self._tool_calls),
                    },
                )
                self._completed = True
                logger.debug("Trace completed: %s, status=%s", self.trace_id, status)

                # AI 回复完成或出错时递增未读计数，确保用户下次打开能看到。
                if should_increment_unread_for_trace_status(status) and self.config.session_id:
                    try:
                        from src.infra.session.manager import SessionManager

                        mgr = SessionManager()
                        await mgr.increment_unread_count(self.config.session_id)
                    except Exception as e:
                        logger.warning("Failed to increment unread_count: %s", e)
            except Exception as e:
                logger.warning("Failed to complete trace %s: %s", self.trace_id, e)

    async def emit(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """发送单个事件（自动保存）"""
        event_type = event.get("event", "unknown")
        data = event.get("data", {})
        agent_id = data.get("agent_id") if isinstance(data, dict) else None
        depth = data.get("depth") if isinstance(data, dict) else None
        if agent_id or (depth and depth > 0):
            # 只对子 Agent（depth>0）产生的事件打 debug 日志，主 Agent 事件量太大不逐条记录
            logger.debug(
                f"[Presenter.emit] event_type={event_type}, agent_id={agent_id}, depth={depth}"
            )
        await self.save_event(event)
        return event


# 延迟导入避免循环依赖
# 中文：should_increment_unread_for_trace_status 定义在 presenter_config.py，
# 若在文件顶部直接 import 会与 presenter_config -> ... -> presenter_storage
# 之间形成模块级循环导入。放在这里（模块加载完毕、且只在 complete() 方法体内
# 被引用）可以规避循环导入问题：Python 只在 complete() 真正被调用时才查找这个
# 名字，而不是在类定义阶段就要求它已经存在。
from src.infra.writer.presenter_config import should_increment_unread_for_trace_status  # noqa: E402

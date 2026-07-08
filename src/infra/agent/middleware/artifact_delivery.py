"""Artifact delivery middleware — auto-deliver generated files without tool chrome."""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# Agent 在沙箱里执行 write_file/edit_file/execute（shell 命令）/上传等操作时，
# 往往会产出用户需要看到的文件（代码、报告、图片……）。如果每次都要求 Agent
# 显式调用 reveal_file/reveal_project 工具才能把文件展示给用户，会让对话里
# 充斥大量"工具调用装饰"（tool chrome），既啰嗦又容易被 Agent 遗漏。
# 本中间件的做法是：拦截每一次工具调用，自动判断"这次调用是否产出了新文件/
# 修改了文件"，把这些文件登记为"待交付产物"（StagedArtifact），然后在合适的
# 时机（工具调用后立即、或整轮结束时）自动代为调用 reveal_file/reveal_project，
# 把结果作为 artifact 事件推给前端——整个过程对 Agent 和最终用户都是透明的。
#
# 判定"产出了什么文件"依工具类型分三种策略：
#   1. execute（跑 shell 命令）：命令执行前后给工作区文件系统拍两次"快照"
#      （路径 -> (size, modified_at) 签名），对比出发生变化的文件；
#   2. write_file/edit_file/upload_url_to_sandbox 等已知会产文件的工具：
#      直接从工具的入参或返回结果里读出目标路径；
#   3. Agent 在回复文本里直接贴了一个文件下载链接（没有调用任何工具）：
#      整轮结束时兜底扫描 assistant 消息文本，用正则抠出候选 URL。
# 全程贯彻"宁可漏交付，绝不误交付"和"绝不影响主工具调用结果"的保守原则：
# 任何自动交付环节出错都只记日志、静默降级，不会让原始工具调用失败或抛异常；
# 同时用 _SENSITIVE_FILENAMES / _IGNORED_PATH_PARTS 等黑名单挡掉密钥文件、
# 构建缓存目录等不该被自动暴露或纯属噪音的路径。
# ============================================================================

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import unquote, urlparse

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from src.infra.async_utils import run_blocking_io

RevealTool = Callable[..., Awaitable[str]]

logger = logging.getLogger(__name__)
# 单次 execute（shell 命令）调用最多自动登记这么多个"变化了的文件"，
# 防止像 npm install 之类会改动大量文件的命令把 UI 刷屏
_EXECUTE_SNAPSHOT_MAX_CHANGED_FILES = 20
# 从 Agent 回复文本里"扒"候选文件 URL 用的正则：匹配 http(s) 链接直到遇到空白/尖括号/引号
_FILE_URL_PATTERN = re.compile(r"https?://[^\s<>\]\"']+", re.IGNORECASE)
# 文本里出现的 URL 只有命中这些扩展名才认为"值得自动交付"（文档/图片/音视频/
# 压缩包等常见可下载文件类型），避免把普通网页链接、API 端点等误当作文件产物
_AUTO_DELIVERABLE_URL_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".csv",
        ".doc",
        ".docx",
        ".gif",
        ".gz",
        ".htm",
        ".html",
        ".jpeg",
        ".jpg",
        ".json",
        ".md",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".svg",
        ".tar",
        ".txt",
        ".wav",
        ".webm",
        ".webp",
        ".xls",
        ".xlsx",
        ".zip",
    }
)
# 路径中出现这些目录名一律跳过，都是版本控制/缓存/构建产物目录，不是用户关心的内容
_IGNORED_PATH_PARTS = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
# 这些文件名即使被判定为"变化了"也绝不自动交付——都是密钥/凭证类敏感文件，
# 安全兜底，防止意外把凭据通过"自动交付"这个便利功能暴露给前端
_SENSITIVE_FILENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)


@dataclass
class StagedArtifact:
    """一个"待交付"的产物登记项：记录路径、类型、展示名、描述、优先级和是否已交付。"""

    path: str
    # "file" 表示单文件，"project"/"folder" 表示整个项目/目录
    kind: str = "file"
    name: str | None = None
    description: str = ""
    # "final"（最终产物）还是 "intermediate"（过程中产生），供前端区分展示优先级
    priority: str = "final"
    # 是否已经交付过（调用过 reveal_*），避免同一产物被重复交付
    revealed: bool = False


async def _json_dumps_result(data: dict[str, Any]) -> str:
    # JSON 序列化丢线程池执行，避免（理论上）较大 payload 时阻塞事件循环
    return await run_blocking_io(json.dumps, data, ensure_ascii=False)


def _normalize_path(path: str) -> str:
    # 生成用作 self._artifacts 字典键的规范化路径：外部 http(s) URL 本身已经是
    # 规范标识，原样保留；本地路径则统一斜杠方向、合并重复斜杠、去掉末尾斜杠，
    # 确保同一个逻辑路径不会因为写法差异（\ vs /、末尾多个 /）被当成两个不同产物
    parsed = urlparse(path.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return path.strip()
    return path.strip().replace("\\", "/").replace("//", "/").rstrip("/")


def _parse_jsonish(content: Any) -> dict[str, Any] | None:
    # 工具返回的 content 名义上是字符串，但语义上经常是一段 JSON；
    # 这里统一尝试解析成 dict，本来就是 dict 直接用，解析失败或结果非 dict 都返回 None，
    # 供调用方安全地"尝试性"读取里面的结构化字段
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _file_info_value(info: Any, key: str) -> Any:
    # 兼容不同沙箱后端返回的"文件信息"对象：有的是 dict，有的是带属性的对象
    if isinstance(info, dict):
        return info.get(key)
    return getattr(info, key, None)


def _coerce_int(value: Any) -> int | None:
    # 显式排除 bool——Python 里 bool 是 int 的子类，isinstance(True, int) 为真，
    # 但把 True/False 当作文件大小之类的数值毫无意义，必须提前挡掉
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _should_skip_auto_artifact(path: str) -> bool:
    # 综合三层过滤规则判断一个路径是否"不该"被自动交付：
    # 路径中含忽略目录、文件名属于敏感凭证、或后缀是日志/临时/编译产物一类噪音文件
    parsed = urlparse(path.strip())
    normalized = unquote(parsed.path if parsed.scheme in {"http", "https"} else path)
    normalized = normalized.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if any(part in _IGNORED_PATH_PARTS for part in parts):
        return True
    filename = os.path.basename(normalized).lower()
    if filename in _SENSITIVE_FILENAMES:
        return True
    return filename.endswith((".log", ".tmp", ".temp", ".pyc", ".map"))


def _content_to_text(content: Any) -> str:
    # LangChain 消息的 content 既可能是纯字符串，也可能是多模态内容块列表，
    # 这里统一拍平成字符串，只保留其中的文本部分，供后续正则扫描 URL
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                item_text = item.get("text") or item.get("content")
                if isinstance(item_text, str):
                    text_parts.append(item_text)
        return "\n".join(text_parts)
    return ""


def _is_auto_deliverable_url(url: str) -> bool:
    # 必须是带 host 的 http(s) 链接，且扩展名在白名单里，才认为值得自动交付
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    clean_path = unquote(parsed.path)
    extension = os.path.splitext(clean_path)[1].lower()
    return extension in _AUTO_DELIVERABLE_URL_EXTENSIONS


def _extract_file_urls_from_text(text: str) -> list[str]:
    # 从一段自由文本里抠出候选文件链接：正则会贪婪地把句末标点也捎带进匹配结果，
    # 这里 rstrip 掉常见的句末/括号标点做修正；再依次过滤"是否值得交付"和
    # "是否命中黑名单"，并按规范化路径去重，保持首次出现的顺序
    urls: list[str] = []
    seen: set[str] = set()
    for match in _FILE_URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}")
        if not _is_auto_deliverable_url(url) or _should_skip_auto_artifact(url):
            continue
        normalized = _normalize_path(url)
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(url)
    return urls


async def _list_backend_files(backend: Any, workspace: str) -> list[Any]:
    # 不同沙箱后端实现暴露的"列出文件"方法名不统一，依次探测：优先异步原生
    # 接口（aglob_info/aglob），其次同步接口（glob_info/glob，用 run_blocking_io
    # 包一层），一个都不支持就返回空列表（快照能力不可用，上层会安全降级为"跳过快照"）
    if hasattr(backend, "aglob_info"):
        return await backend.aglob_info("**/*", path=workspace)
    if hasattr(backend, "aglob"):
        result = await backend.aglob("**/*", path=workspace)
        return getattr(result, "matches", result) or []
    if hasattr(backend, "glob_info"):
        return await run_blocking_io(backend.glob_info, "**/*", workspace)
    if hasattr(backend, "glob"):
        result = await run_blocking_io(backend.glob, "**/*", workspace)
        return getattr(result, "matches", result) or []
    return []


def _path_from_reveal_result(result: ToolMessage, args: dict[str, Any]) -> str | None:
    # 从 reveal_file/reveal_project 工具的返回结果里反推"这次到底揭示了哪个路径"，
    # 依次尝试几种已知的返回结构（_meta.path / file_reveal 包装 / 顶层 path 或
    # project_path），全部匹配不到时退回工具调用参数本身（毕竟调用方肯定知道
    # 自己传了什么路径进去）
    parsed = _parse_jsonish(result.content)
    if parsed:
        meta = parsed.get("_meta") if isinstance(parsed.get("_meta"), dict) else None
        path = meta.get("path") if meta else None
        if isinstance(path, str) and path:
            return path

        if parsed.get("type") == "file_reveal" and isinstance(parsed.get("file"), dict):
            file_path = parsed["file"].get("path")
            if isinstance(file_path, str) and file_path:
                return file_path

        project_path = parsed.get("path") or parsed.get("project_path")
        if isinstance(project_path, str) and project_path:
            return project_path

    fallback = args.get("file_path") or args.get("project_path") or args.get("path")
    return fallback if isinstance(fallback, str) and fallback else None


def _reveal_error(parsed: dict[str, Any] | None) -> str | None:
    # 从解析后的 reveal 结果里找出错误信息：直接的 error 字段最优先；message
    # 字段只有在 error 也为真时才采信（避免把普通提示信息误判为错误）；
    # 最后再看嵌套的 file.error
    if not parsed:
        return None
    error = parsed.get("error")
    if isinstance(error, str) and error:
        return error
    message = parsed.get("message")
    if isinstance(message, str) and parsed.get("error"):
        return message
    file = parsed.get("file")
    if isinstance(file, dict):
        file_error = file.get("error")
        if isinstance(file_error, str) and file_error:
            return file_error
    return None


class ArtifactDeliveryMiddleware(AgentMiddleware):
    """Detect sandbox artifacts, index them, and emit artifact result events."""

    def __init__(
        self,
        *,
        reveal_file: RevealTool | None = None,
        reveal_project: RevealTool | None = None,
        workspace_path: str | None = None,
    ) -> None:
        super().__init__()
        # 全局登记表：规范化路径 -> StagedArtifact，贯穿整个 Agent 运行过程，
        # 用于跨多次工具调用去重、追踪交付状态
        self._artifacts: dict[str, StagedArtifact] = {}
        # 允许注入自定义的 reveal 实现（主要用于测试），留空则运行时懒加载真实工具
        self._reveal_file = reveal_file
        self._reveal_project = reveal_project
        self._workspace_path = workspace_path.rstrip("/") if workspace_path else None

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        # 中间件的核心钩子：包裹每一次工具调用，根据工具名走三条不同的自动
        # 交付策略（execute 用前后快照 diff；reveal_* 只做"已交付"标记；
        # 其余已知产文件的工具直接从参数/结果里抠路径）
        before_snapshot = None
        tool_name = request.tool_call.get("name", "")
        tool_args = request.tool_call.get("args", {})
        if not isinstance(tool_args, dict):
            tool_args = {}
        if tool_name == "execute":
            # 执行 shell 命令前先拍一次工作区快照，供之后 diff 出变化的文件
            before_snapshot = await self._snapshot_workspace(request.runtime)

        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result

        if tool_name == "execute":
            staged = await self._auto_stage_execute_changes(
                request.runtime,
                before_snapshot,
                result,
            )
            await self._deliver_staged_artifacts(staged, request.runtime)
            return result

        if tool_name in {"reveal_file", "reveal_project"}:
            # Agent 自己显式调用了揭示工具，说明这条路径已经交付给前端了，
            # 只需要打上"已交付"标记，避免结束时的兜底逻辑重复交付
            self._mark_revealed(result, tool_args)
            return result

        staged = self._auto_stage_from_tool_result(tool_name, tool_args, result)
        await self._deliver_staged_artifacts(staged, request.runtime)
        return result

    async def aafter_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # 整轮结束后的兜底扫尾：先从 assistant 文本里再找一遍没通过工具调用
        # 产生的外部文件链接，再把所有"登记了但还没交付"的产物统一交付一遍
        # （正常情况下大多数产物已经在 awrap_tool_call 里及时交付过了，
        # 这里主要兜住时序上没能立即交付的遗漏情况）
        self._auto_stage_external_urls_from_state(state)
        pending = [artifact for artifact in self._artifacts.values() if not artifact.revealed]
        if not pending:
            return None

        for artifact in pending:
            delivered = await self._deliver_artifact(artifact, runtime)
            if delivered:
                artifact.revealed = True

        # 返回空的 messages 更新，符合 LangGraph "做了副作用但不修改消息" 的约定
        return {"messages": []}

    def _auto_stage_external_urls_from_state(self, state: Any) -> None:
        # 只扫描 AI/assistant 角色的消息（用户消息、工具消息不是"Agent 主动
        # 分享的链接"来源），把文本里符合"可交付文件"特征的 URL 登记为
        # intermediate 优先级的产物，交给后续统一交付流程处理
        messages = state.get("messages") if isinstance(state, dict) else None
        if not isinstance(messages, list):
            return

        for message in messages:
            if getattr(message, "type", None) not in {"ai", "assistant"}:
                continue
            content = _content_to_text(getattr(message, "content", ""))
            if not content:
                continue
            for url in _extract_file_urls_from_text(content):
                self._stage_path(
                    url,
                    kind="file",
                    description="External file linked by the agent",
                    priority="intermediate",
                )

    def _mark_revealed(self, result: ToolMessage, args: dict[str, Any]) -> None:
        # Agent 显式调用 reveal_file/reveal_project 后，把对应路径标记为已交付：
        # 如果之前从未登记过这个路径（比如 Agent 一上来就直接 reveal，没有经过
        # write_file 等自动登记环节），就补建一条"已交付"的记录；已存在则直接置位
        path = _path_from_reveal_result(result, args)
        if not path:
            return

        normalized_path = _normalize_path(path)
        existing = self._artifacts.get(normalized_path)
        if existing is None:
            self._artifacts[normalized_path] = StagedArtifact(
                path=path,
                kind="project" if result.name == "reveal_project" else "file",
                revealed=True,
            )
            return
        existing.revealed = True

    def _auto_stage_from_tool_result(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: ToolMessage,
    ) -> list[StagedArtifact]:
        # 通用的"从工具调用结果自动登记产物"逻辑：先排除失败的调用
        # （无论是 ToolMessage 自身状态标了 error，还是返回内容里带 success=False/error），
        # 再委托 _artifact_path_from_tool 判断这个具体工具到底产出了什么路径
        if getattr(result, "status", None) == "error":
            return []

        parsed = _parse_jsonish(result.content)
        if isinstance(parsed, dict) and (
            parsed.get("success") is False or parsed.get("error") is not None
        ):
            return []

        path = self._artifact_path_from_tool(tool_name, args, parsed)
        if not path:
            return []

        artifact = self._stage_path(
            path,
            kind="file",
            description=self._description_from_auto_stage(tool_name),
            priority="intermediate",
        )
        return [artifact] if artifact is not None else []

    @staticmethod
    def _artifact_path_from_tool(
        tool_name: str,
        args: dict[str, Any],
        parsed: dict[str, Any] | None,
    ) -> str | None:
        # 按工具类型分别决定去哪里找路径：upload_url_to_sandbox 只有调用方
        # 传入的源 URL，实际落盘路径要看工具自己的返回结果；write_file/edit_file
        # 则相反，调用方在参数里就明确指定了目标路径，无需看返回结果
        if tool_name == "upload_url_to_sandbox":
            result_path = parsed.get("path") if parsed else None
            if isinstance(result_path, str) and result_path:
                return result_path

        if tool_name in {"write_file", "edit_file"}:
            for key in ("file_path", "path"):
                path = args.get(key)
                if isinstance(path, str) and path:
                    return path

        # 其余工具不在本中间件已知的"会产文件"名单里，不做任何自动登记
        return None

    @staticmethod
    def _description_from_auto_stage(tool_name: str) -> str:
        # 按工具类型给出面向用户的产物描述文案
        match tool_name:
            case "write_file":
                return "File created by the agent"
            case "edit_file":
                return "File modified by the agent"
            case "upload_url_to_sandbox":
                return "File downloaded into the sandbox"
            case _:
                return ""

    def _stage_path(
        self,
        path: str,
        *,
        kind: str,
        name: str | None = None,
        description: str = "",
        priority: str = "final",
    ) -> StagedArtifact | None:
        # 统一的"登记产物"入口：文件类型才做敏感/噪音路径过滤（项目/文件夹
        # 类型不受这层检查约束），登记会直接覆盖同路径下的旧记录——
        # 同一轮里文件被反复修改时，后面的登记天然替换前面的
        normalized_path = _normalize_path(path)
        if kind == "file" and _should_skip_auto_artifact(normalized_path):
            return None
        artifact = StagedArtifact(
            path=path,
            kind=kind,
            name=name,
            description=description,
            priority=priority,
        )
        self._artifacts[normalized_path] = artifact
        return artifact

    async def _auto_stage_execute_changes(
        self,
        runtime: Any,
        before_snapshot: dict[str, tuple[int | None, str | None]] | None,
        result: ToolMessage,
    ) -> list[StagedArtifact]:
        # execute（shell 命令）产物登记：没有 before 快照（说明快照本身失败/不可用）
        # 或命令执行失败，都直接放弃，不做任何自动登记
        if before_snapshot is None or getattr(result, "status", None) == "error":
            return []
        parsed = _parse_jsonish(result.content)
        if isinstance(parsed, dict) and (
            parsed.get("success") is False or parsed.get("error") is not None
        ):
            return []

        after_snapshot = await self._snapshot_workspace(runtime)
        if after_snapshot is None:
            return []

        # 对比前后两次快照：签名（大小、修改时间）不同即视为"变化"（新建的文件在
        # before_snapshot 里本就不存在，get 返回 None，天然也算作"不同"），
        # 同时排除掉噪音/敏感路径，并整体限量避免刷屏
        changed_paths: list[str] = []
        for path, signature in after_snapshot.items():
            if before_snapshot.get(path) != signature and not _should_skip_auto_artifact(path):
                changed_paths.append(path)
            if len(changed_paths) >= _EXECUTE_SNAPSHOT_MAX_CHANGED_FILES:
                break

        staged: list[StagedArtifact] = []
        for path in changed_paths:
            artifact = self._stage_path(
                path,
                kind="file",
                description="File created or modified by a shell command",
                priority="intermediate",
            )
            if artifact is not None:
                staged.append(artifact)
        return staged

    async def _snapshot_workspace(
        self, runtime: Any
    ) -> dict[str, tuple[int | None, str | None]] | None:
        # 给工作区里所有文件拍一份"签名快照"：{路径: (大小, 修改时间)}，
        # 用于 execute 命令前后对比出发生变化的文件
        workspace = self._workspace_path or self._workspace_from_runtime(runtime)
        if not workspace:
            return None
        backend = self._backend_from_runtime(runtime)
        if backend is None:
            return None

        try:
            infos = await _list_backend_files(backend, workspace)
        except Exception as exc:
            # 快照失败不应该影响 execute 工具调用本身的结果，只记调试日志、
            # 返回 None 让上层安全地跳过这次自动登记
            logger.debug("Artifact workspace snapshot failed for %s: %s", workspace, exc)
            return None

        snapshot: dict[str, tuple[int | None, str | None]] = {}
        for info in infos:
            path = _file_info_value(info, "path")
            if not isinstance(path, str) or not path or _file_info_value(info, "is_dir"):
                continue
            snapshot[path] = (
                _coerce_int(_file_info_value(info, "size")),
                _coerce_str(_file_info_value(info, "modified_at")),
            )
        return snapshot

    @staticmethod
    def _workspace_from_runtime(runtime: Any) -> str | None:
        # 依次尝试几种途径确定沙箱工作目录：后端对象自身的 work_dir/workspace_path
        # 属性，或者 runtime.config["configurable"] 里挂的同名字段（有些场景下
        # 工作目录只是作为配置透传，backend 对象本身不直接暴露它）
        backend = ArtifactDeliveryMiddleware._backend_from_runtime(runtime)
        work_dir = getattr(backend, "work_dir", None)
        if isinstance(work_dir, str) and work_dir:
            return work_dir.rstrip("/")
        workspace_path = getattr(backend, "workspace_path", None)
        if isinstance(workspace_path, str) and workspace_path:
            return workspace_path.rstrip("/")

        config = getattr(runtime, "config", None)
        configurable = config.get("configurable") if isinstance(config, dict) else None
        if isinstance(configurable, dict):
            for key in ("work_dir", "workspace_path"):
                value = configurable.get(key)
                if isinstance(value, str) and value:
                    return value.rstrip("/")
        return None

    @staticmethod
    def _backend_from_runtime(runtime: Any) -> Any | None:
        # 解析沙箱后端对象失败（runtime 形状不对、上下文缺失等）时静默返回
        # None，而不是让异常向上传播影响到真正的工具调用
        try:
            from src.infra.tool.backend_utils import get_backend_from_runtime

            return get_backend_from_runtime(runtime)
        except Exception:
            return None

    async def _deliver_artifact(self, artifact: StagedArtifact, runtime: Any) -> bool:
        # 交付单个产物：像 Agent 自己调用了一样去调真正的 reveal_file/
        # reveal_project 工具，再把结果转换成前端能识别的 artifact 事件负载
        is_project = artifact.kind in {"project", "folder"}
        tool_name = "reveal_project" if is_project else "reveal_file"
        args: dict[str, Any]
        if is_project:
            args = {
                "project_path": artifact.path,
                "name": artifact.name or artifact.path.rstrip("/").rsplit("/", 1)[-1],
            }
            if artifact.description:
                args["description"] = artifact.description
        else:
            args = {
                "file_path": artifact.path,
                "description": artifact.description,
            }

        try:
            content = await self._call_reveal_tool(tool_name, args, runtime)
            parsed = _parse_jsonish(content)
            error = _reveal_error(parsed)
            if error:
                # reveal 工具本身正常返回，但内容里带了错误信息（如文件不存在）
                delivered = self._failed_artifact_payload(artifact, error)
                status = "error"
            else:
                delivered = self._artifact_payload_from_reveal_content(artifact, content, args)
                status = "success"
        except Exception as exc:
            # reveal 调用过程本身抛异常（网络问题、工具内部报错等），同样按失败处理，
            # 不让异常向上传播影响主工具调用链路
            logger.warning("Artifact reveal failed for %s: %s", artifact.path, exc)
            content = await _json_dumps_result(
                {
                    "type": "artifact_reveal_failed",
                    "path": artifact.path,
                    "kind": artifact.kind,
                    "error": str(exc),
                }
            )
            delivered = self._failed_artifact_payload(artifact, str(exc))
            status = "error"
            error = str(exc)

        return await self._emit_artifact_result(runtime, delivered, status=status, error=error)

    async def _deliver_staged_artifacts(
        self,
        artifacts: list[StagedArtifact],
        runtime: Any,
    ) -> None:
        # 没有 presenter（意味着没有前端在监听 SSE 事件）时整批直接跳过，
        # 避免做无意义的 reveal 调用
        if self._get_presenter(runtime) is None:
            return
        for artifact in artifacts:
            if artifact.revealed:
                continue
            delivered = await self._deliver_artifact(artifact, runtime)
            if delivered:
                artifact.revealed = True

    async def _call_reveal_tool(self, tool_name: str, args: dict[str, Any], runtime: Any) -> str:
        # 给 runtime 的 config 注入 delivery_source="artifact_auto" 标记，
        # 让 reveal 工具及下游能区分"这是中间件自动触发的揭示"还是
        # "Agent 自己主动调用的揭示"（用于 UI 展示差异化或统计）
        delivery_runtime = self._runtime_with_delivery_source(runtime, "artifact_auto")
        if tool_name == "reveal_project":
            reveal_project = self._reveal_project
            if reveal_project is None:
                # 懒加载真实工具实现，避免中间件构造阶段就产生不必要的 import 开销，
                # 也规避潜在的循环 import
                from src.infra.tool.reveal_project_tool import reveal_project as reveal_project_tool

                reveal_project = cast(RevealTool, getattr(reveal_project_tool, "coroutine"))

            return await reveal_project(**args, runtime=delivery_runtime)

        reveal_file = self._reveal_file
        if reveal_file is None:
            from src.infra.tool.reveal_file_tool import reveal_file as reveal_file_tool

            reveal_file = cast(RevealTool, getattr(reveal_file_tool, "coroutine"))

        return await reveal_file(**args, runtime=delivery_runtime)

    @staticmethod
    def _runtime_with_delivery_source(runtime: Any, delivery_source: str) -> Any:
        # 构造一个"浅拷贝+打了标记"的 runtime 替身（用 SimpleNamespace 伪装出
        # 一个带 .config 属性的对象），而不是就地修改原 runtime.config，
        # 避免 delivery_source 标记泄漏影响到同一 runtime 后续的其它工具调用
        config = getattr(runtime, "config", None)
        if not isinstance(config, dict):
            return SimpleNamespace(config={"configurable": {"delivery_source": delivery_source}})

        next_config = dict(config)
        configurable = next_config.get("configurable")
        if isinstance(configurable, dict):
            next_config["configurable"] = {
                **configurable,
                "delivery_source": delivery_source,
            }
        else:
            next_config["configurable"] = {"delivery_source": delivery_source}
        return SimpleNamespace(config=next_config)

    @staticmethod
    def _get_presenter(runtime: Any) -> Any | None:
        config = getattr(runtime, "config", None)
        if not isinstance(config, dict):
            return None
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            return None
        return configurable.get("presenter")

    async def _emit_artifact_result(
        self,
        runtime: Any,
        artifact: dict[str, Any],
        *,
        status: str,
        error: str | None,
    ) -> bool:
        # 找不到 presenter，或 presenter 是旧版本没有 present_artifact_result
        # 方法，都静默返回 False——自动交付是增强能力，缺失时不报错
        presenter = self._get_presenter(runtime)
        if presenter is None or not hasattr(presenter, "present_artifact_result"):
            return False

        event = presenter.present_artifact_result(
            artifact,
            success=status != "error",
            error=error,
        )
        emit = getattr(presenter, "emit", None)
        if callable(emit):
            await emit(event)
            return True
        return False

    def _artifact_payload_from_reveal_content(
        self,
        artifact: StagedArtifact,
        content: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        # 按产物类型分派到对应的负载构造函数
        parsed = _parse_jsonish(content) or {}
        if artifact.kind in {"project", "folder"}:
            return self._project_artifact_payload(artifact, parsed, args)
        return self._file_artifact_payload(artifact, parsed)

    @staticmethod
    def _file_artifact_payload(artifact: StagedArtifact, parsed: dict[str, Any]) -> dict[str, Any]:
        # 组装前端展示"文件产物"所需的完整负载：路径优先取 reveal 工具返回结果里
        # _meta.path（工具内部可能对路径做了解析/规范化，更权威），拿不到才退回
        # 最初登记时的 artifact.path
        raw_meta = parsed.get("_meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        meta_path = meta.get("path")
        file_path = meta_path if isinstance(meta_path, str) and meta_path else artifact.path
        s3_key = parsed.get("key") if isinstance(parsed.get("key"), str) else None
        s3_url = parsed.get("url") if isinstance(parsed.get("url"), str) else None
        name = parsed.get("name") if isinstance(parsed.get("name"), str) else None
        file_size = parsed.get("size") if isinstance(parsed.get("size"), int) else None
        # 预览用的 key 优先级：s3_key > s3_url > 文件路径本身——内容一旦被
        # 上传到对象存储，用那个 key/url 作为标识比本地路径更稳定可靠
        preview_key = s3_key or s3_url or file_path
        meta_description = meta.get("description")
        description = (
            meta_description if isinstance(meta_description, str) else artifact.description
        )

        return {
            "kind": "file",
            "id": f"file:{preview_key}",
            "name": name or file_path.rstrip("/").rsplit("/", 1)[-1] or file_path,
            "path": file_path,
            "description": description,
            "fileSize": file_size,
            "preview": {
                "kind": "file",
                "previewKey": preview_key,
                "filePath": file_path,
                "s3Key": s3_key,
                "signedUrl": s3_url,
                "fileSize": file_size,
            },
        }

    @staticmethod
    def _project_artifact_payload(
        artifact: StagedArtifact,
        parsed: dict[str, Any],
        args: dict[str, Any],
    ) -> dict[str, Any]:
        # 路径/名称都按"reveal 返回结果 > 原始调用参数 > 登记时的 artifact 自身"
        # 三级回退：reveal 结果最权威（工具执行时可能做了规范化），其次是
        # 调用方最初的意图，最后兜底用登记时留存的值
        parsed_path = parsed.get("path")
        args_project_path = args.get("project_path")
        project_path = (
            parsed_path
            if isinstance(parsed_path, str) and parsed_path
            else args_project_path
            if isinstance(args_project_path, str) and args_project_path
            else artifact.path
        )
        parsed_name = parsed.get("name")
        args_name = args.get("name")
        project_name = (
            parsed_name
            if isinstance(parsed_name, str) and parsed_name
            else args_name
            if isinstance(args_name, str) and args_name
            else artifact.name or project_path.rstrip("/").rsplit("/", 1)[-1]
        )
        mode = parsed.get("mode") if parsed.get("mode") in {"project", "folder"} else "folder"
        template = parsed.get("template") if isinstance(parsed.get("template"), str) else "static"
        file_count = parsed.get("file_count") if isinstance(parsed.get("file_count"), int) else 0
        preview_key = project_path or project_name

        return {
            "kind": "project",
            "id": f"project:{preview_key}",
            "name": project_name,
            "mode": mode,
            "fileCount": file_count,
            "template": template,
            "preview": {
                "kind": "project",
                "previewKey": preview_key,
                "project": parsed,
            },
        }

    @staticmethod
    def _failed_artifact_payload(artifact: StagedArtifact, error: str) -> dict[str, Any]:
        # 交付失败时的最小可用负载：即便揭示本身失败，也要给前端足够信息
        # 展示一个"交付失败"的错误状态，而不是什么都拿不到
        return {
            "kind": artifact.kind,
            "id": f"failed:{artifact.path}",
            "name": artifact.name or artifact.path.rstrip("/").rsplit("/", 1)[-1],
            "path": artifact.path,
            "description": artifact.description,
            "error": error,
        }

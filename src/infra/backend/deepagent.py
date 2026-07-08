"""
DeepAgent Backend 工厂模块

为 DeepAgent 创建不同模式的 Backend 工厂函数。

Skills 路径现在使用 SkillsStoreBackend，支持 LLM 直接读写 skills 到 MongoDB。
"""

import re
from typing import Any, Callable, Optional, cast

from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GrepMatch,
    ReadResult,
    WriteResult,
)

from src.infra.logging import get_logger

logger = get_logger(__name__)


# 把 session_id 清洗成可安全用于路径/namespace 的字符串：非 [A-Za-z0-9_.-] 的字符
# 替换为 '-'，去掉首尾的 '.'/'-'，截断到 80 字符；为空则回退成 "session"。
def _safe_session_id(session_id: str | None) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", session_id or "").strip(".-")
    return safe[:80] if safe else "session"


# 给 FileInfo 的 path 补上 workspace 前缀（仅当它是以 '/' 开头的绝对内部路径），
# 使对外返回的路径落在 workspace_path 命名空间下。返回新 dict，不修改原对象。
def _prefix_file_info_path(info: FileInfo, workspace_path: str) -> FileInfo:
    prefixed: dict[str, object] = dict(info)
    path = str(prefixed.get("path", ""))
    if path.startswith("/"):
        prefixed["path"] = f"{workspace_path}{path}"
    return cast(FileInfo, prefixed)


# 路径作用域包装器：对外暴露 /workflow/<session> 这样的工作区路径，对内则把文件存到被
# 包装 backend 的根("/")下。核心是一对互逆变换——_strip_path 去掉对外前缀转成内部路径，
# _prefix_path 给内部路径加回前缀——所有读写/搜索方法都"先 strip 入参、再 prefix 出参"，
# 从而让不同 session 的文件互相隔离，且对上层完全透明。
class WorkflowScopedBackend(BackendProtocol):
    """Expose a session workflow path while storing files under a scoped backend root."""

    def __init__(self, backend: BackendProtocol, workspace_path: str) -> None:
        self._backend = backend
        self.workspace_path = workspace_path.rstrip("/")

    # 入参变换：把对外的 workspace 路径（/workflow/xxx/...）转换成内部路径（以 "/" 为根）
    def _strip_path(self, path: str | None) -> str:
        if path is None or path == "/":
            return "/"
        if path == self.workspace_path:
            return "/"
        prefix = f"{self.workspace_path}/"
        if path.startswith(prefix):
            return "/" + path[len(prefix) :]
        return path

    # 出参变换：把内部路径加回 workspace 前缀，是 _strip_path 的逆操作
    def _prefix_path(self, path: str) -> str:
        if path.startswith(self.workspace_path):
            return path
        if path.startswith("/"):
            return f"{self.workspace_path}{path}"
        return f"{self.workspace_path}/{path}"

    def ls_info(self, path: str) -> list[FileInfo]:
        return [
            _prefix_file_info_path(info, self.workspace_path)
            for info in self._backend.ls_info(self._strip_path(path))
        ]

    async def als_info(self, path: str) -> list[FileInfo]:
        infos = await self._backend.als_info(self._strip_path(path))
        return [_prefix_file_info_path(info, self.workspace_path) for info in infos]

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._backend.read(self._strip_path(file_path), offset, limit)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await self._backend.aread(self._strip_path(file_path), offset, limit)

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        result = self._backend.grep_raw(pattern, self._strip_path(path), glob)
        if isinstance(result, str):
            return result
        return [
            GrepMatch(**{**match, "path": self._prefix_path(match["path"])}) for match in result
        ]

    async def agrep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        result = await self._backend.agrep_raw(pattern, self._strip_path(path), glob)
        if isinstance(result, str):
            return result
        return [
            GrepMatch(**{**match, "path": self._prefix_path(match["path"])}) for match in result
        ]

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        return [
            _prefix_file_info_path(info, self.workspace_path)
            for info in self._backend.glob_info(pattern, self._strip_path(path))
        ]

    async def aglob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        infos = await self._backend.aglob_info(pattern, self._strip_path(path))
        return [_prefix_file_info_path(info, self.workspace_path) for info in infos]

    def write(self, file_path: str, content: str) -> WriteResult:
        result = self._backend.write(self._strip_path(file_path), content)
        if getattr(result, "path", None):
            result.path = self._prefix_path(str(result.path))
        return result

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        result = await self._backend.awrite(self._strip_path(file_path), content)
        if getattr(result, "path", None):
            result.path = self._prefix_path(str(result.path))
        return result

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self._backend.edit(self._strip_path(file_path), old_string, new_string, replace_all)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return await self._backend.aedit(
            self._strip_path(file_path),
            old_string,
            new_string,
            replace_all,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._backend.upload_files(
            [(self._strip_path(path), content) for path, content in files]
        )

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return await self._backend.aupload_files(
            [(self._strip_path(path), content) for path, content in files]
        )

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._backend.download_files([self._strip_path(path) for path in paths])

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return await self._backend.adownload_files([self._strip_path(path) for path in paths])


def _create_routes(
    assistant_id: str,
    user_id: str,
) -> dict[str, BackendProtocol]:
    """创建通用的 backend 路由（skills + memories）"""
    from src.infra.backend.skills_store import create_skills_backend

    skills_backend = create_skills_backend(user_id=user_id)

    # /skills/ → MongoDB 里的 skills 存储（LLM 可直接读写）；
    # /memories/ → Store 的长期记忆命名空间 (assistant_id, "memories")
    return {
        "/skills/": skills_backend,
        "/memories/": StoreBackend(namespace=lambda _rt: (assistant_id, "memories")),
    }


# ── 三种 Backend 工厂 ──
# 都返回 backend_factory(runtime) 闭包，交给 deepagents 在运行时构造 CompositeBackend。
# 区别只在"默认后端"（default）：memory=纯内存瞬态，persistent=Store 持久化，
# sandbox=真实沙箱执行环境；三者的 /skills/、/memories/ 路由基本一致。
def create_memory_backend_factory(
    assistant_id: str,
    user_id: Optional[str] = None,
) -> Callable[[Any], CompositeBackend]:
    """创建基于内存的 Backend 工厂（不使用长期存储）"""

    def backend_factory(_rt: Any) -> CompositeBackend:
        from src.infra.backend.skills_store import create_skills_backend

        skills_backend = create_skills_backend(user_id=user_id or "default")

        # 默认走 StateBackend（随请求生命周期的内存态，不落盘），仅 /skills/ 落到 MongoDB
        return CompositeBackend(
            default=StateBackend(),
            routes={"/skills/": skills_backend},
        )

    return backend_factory


def create_persistent_backend_factory(
    assistant_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Callable[[Any], CompositeBackend]:
    """创建基于 Store 的 Backend 工厂（PostgreSQL / MongoDB 通用）。

    底层 Store 由 create_deep_agent 传入，此处只负责 namespace 路由。
    """

    def backend_factory(_rt: Any) -> CompositeBackend:
        routes = _create_routes(assistant_id, user_id or "default")
        workflow_session_id = _safe_session_id(session_id)
        workspace_path = f"/workflow/{workflow_session_id}"
        # 每个 session 一个独立 namespace：(assistant_id, "workflow", session_id)，
        # 再用 WorkflowScopedBackend 把它对外呈现为 /workflow/<session> 路径，实现会话级隔离
        filesystem_backend = StoreBackend(
            namespace=lambda _rt: (assistant_id, "workflow", workflow_session_id)
        )

        return CompositeBackend(
            default=WorkflowScopedBackend(filesystem_backend, workspace_path),
            routes=routes,
        )

    return backend_factory


def create_sandbox_backend_factory(
    sandbox_backend: Any,
    assistant_id: str,
    user_id: Optional[str] = None,
) -> Callable[[Any], CompositeBackend]:
    """创建基于沙箱的 Backend 工厂"""

    def backend_factory(_rt: Any) -> CompositeBackend:
        routes = _create_routes(assistant_id, user_id or "default")

        # 默认后端即传入的沙箱后端（真实代码执行环境），/skills/ 与 /memories/ 仍走 Store
        return CompositeBackend(
            default=sandbox_backend,
            routes=routes,
        )

    return backend_factory

"""deepagents backends.protocol 的兼容层。

不同版本的 deepagents 对协议类型（尤其是 ReadResult）定义不同：
  - 新版（≥0.5）用 @dataclass；旧版用 str 的子类；库缺失或被测试 mock 时用本地 fallback。
本模块屏蔽这些差异，对上层统一导出一组名字（见 __all__）：
  - TYPE_CHECKING 分支：给 mypy 一套精确的 stub 定义；
  - 运行时分支：绑定到真实的 upstream 类型，缺失则退回本文件内的 _Fallback* 实现。
如此一来，BaseSandbox / BackendProtocol 的签名无论 deepagents 版本如何都保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import deepagents.backends.protocol as _protocol


# 安全读取 upstream protocol 模块的属性：若取到的是 unittest.mock 对象（说明测试里把
# deepagents mock 掉了、并非真实类型），则改用 fallback，避免把 Mock 当成类型使用。
def _protocol_attr(name: str, fallback: Any) -> Any:
    value = getattr(_protocol, name, fallback)
    if type(value).__module__ == "unittest.mock":
        return fallback
    return value


# 以下 _Fallback* 为 upstream 未提供对应类型时的兜底实现；__getitem__ 让它们支持
# dict 式下标访问（如 result["error"]），与真实协议类型的用法保持一致。
class _FallbackReadResultBase:
    pass


@dataclass
class _FallbackLsResult:
    entries: list[Any] | None = None
    error: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass
class _FallbackGlobResult:
    matches: list[Any] | None = None
    error: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass
class _FallbackGrepResult:
    matches: list[Any] | None = None
    error: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


# 静态检查分支：仅供 mypy 使用，定义一套精确的类型 stub（运行时不会执行到这里）。
if TYPE_CHECKING:
    from deepagents.backends.protocol import (
        BackendProtocol as BackendProtocol,
    )
    from deepagents.backends.protocol import (
        EditResult as EditResult,
    )
    from deepagents.backends.protocol import (
        ExecuteResponse as ExecuteResponse,
    )
    from deepagents.backends.protocol import (
        FileData as FileData,
    )
    from deepagents.backends.protocol import (
        FileDownloadResponse as FileDownloadResponse,
    )
    from deepagents.backends.protocol import (
        FileInfo as FileInfo,
    )
    from deepagents.backends.protocol import (
        FileUploadResponse as FileUploadResponse,
    )
    from deepagents.backends.protocol import (
        GlobResult as _ProtocolGlobResult,
    )
    from deepagents.backends.protocol import (
        GrepMatch as GrepMatch,
    )
    from deepagents.backends.protocol import (
        GrepResult as _ProtocolGrepResult,
    )
    from deepagents.backends.protocol import (
        LsResult as _ProtocolLsResult,
    )
    from deepagents.backends.protocol import (
        ReadResult as _ProtocolReadResult,
    )
    from deepagents.backends.protocol import (
        WriteResult as WriteResult,
    )

    class GlobResult(_ProtocolGlobResult):
        matches: list[Any] | None = None
        error: str | None = None

        def __init__(
            self,
            error: str | None = None,
            matches: list[Any] | None = None,
        ) -> None: ...

        def __getitem__(self, key: str) -> Any:
            return getattr(self, key)

    class GrepResult(_ProtocolGrepResult):
        matches: list[Any] | None = None
        error: str | None = None

        def __init__(
            self,
            error: str | None = None,
            matches: list[Any] | None = None,
        ) -> None: ...

        def __getitem__(self, key: str) -> Any:
            return getattr(self, key)

    class LsResult(_ProtocolLsResult):
        entries: list[Any] | None = None
        error: str | None = None

        def __init__(
            self,
            error: str | None = None,
            entries: list[Any] | None = None,
        ) -> None: ...

        def __getitem__(self, key: str) -> Any:
            return getattr(self, key)

    class ReadResult(_ProtocolReadResult):
        file_data: FileData | None
        error: str | None
        rendered_content: str | None

        def __init__(
            self,
            *,
            file_data: FileData | None = None,
            error: str | None = None,
            rendered_content: str | None = None,
        ) -> None: ...

    _HAS_UPSTREAM_READ_RESULT = False
    _UPSTREAM_IS_DATACLASS = False
    _UPSTREAM_IS_STR_SUBCLASS = False
    _UpstreamReadResult: type[Any] = ReadResult

else:

    # 运行时分支：把各名字绑定到真实的 upstream 协议类型（缺失则退回 _Fallback*）。
    def _mapping_getitem(self: Any, key: str) -> Any:
        return getattr(self, key)

    # 取回 upstream 的 mapping 类型；若它缺少 __getitem__，就为其补上一个，
    # 保证 result["key"] 形式的下标访问在所有 deepagents 版本上都可用。
    def _mapping_protocol_type(name: str, fallback: type[Any]) -> type[Any]:
        upstream = _protocol_attr(name, fallback)
        if not isinstance(upstream, type):
            return fallback
        if not hasattr(upstream, "__getitem__"):
            setattr(upstream, "__getitem__", _mapping_getitem)
        return upstream

    BackendProtocol = _protocol_attr("BackendProtocol", Any)
    EditResult = _protocol_attr("EditResult", Any)
    ExecuteResponse = _protocol_attr("ExecuteResponse", Any)
    FileData = _protocol_attr("FileData", dict[str, Any])
    FileDownloadResponse = _protocol_attr("FileDownloadResponse", Any)
    FileInfo = _protocol_attr("FileInfo", Any)
    FileUploadResponse = _protocol_attr("FileUploadResponse", Any)
    GlobResult = _mapping_protocol_type("GlobResult", _FallbackGlobResult)
    GrepMatch = _protocol_attr("GrepMatch", Any)
    GrepResult = _mapping_protocol_type("GrepResult", _FallbackGrepResult)
    LsResult = _mapping_protocol_type("LsResult", _FallbackLsResult)
    WriteResult = _protocol_attr("WriteResult", Any)
    _HAS_UPSTREAM_READ_RESULT = hasattr(_protocol, "ReadResult")
    _UpstreamReadResult = _protocol_attr("ReadResult", _FallbackReadResultBase)

    # 探测 upstream ReadResult 的形态：dataclass（deepagents ≥0.5）还是旧版 str 子类，
    # 据此在下面选择不同的扩展方式。
    # Detect whether the upstream ReadResult is a dataclass (deepagents ≥0.5)
    # or the legacy str-subclass.
    _UPSTREAM_IS_DATACLASS = hasattr(_UpstreamReadResult, "__dataclass_fields__")
    _UPSTREAM_IS_STR_SUBCLASS = isinstance(_UpstreamReadResult, type) and issubclass(
        _UpstreamReadResult, str
    )

    # 情形一：新版 dataclass ReadResult —— 继承它并加一个 rendered_content 字段，
    # 同时实现 __str__/__contains__/__iter__/__len__，使其既是结构化结果又能当字符串用。
    if _UPSTREAM_IS_DATACLASS:

        @dataclass
        class ReadResult(_UpstreamReadResult):  # type: ignore[no-redef]
            """Extended ReadResult that also stores a rendered string representation.

            Compatible with the dataclass-based upstream ``ReadResult`` introduced
            in deepagents 0.5.
            """

            rendered_content: str | None = None

            def __post_init__(self) -> None:
                if self.rendered_content is None:
                    if self.error is not None:
                        self.rendered_content = (
                            self.error
                            if self.error.startswith("Error:")
                            else f"Error: {self.error}"
                        )
                    else:
                        self.rendered_content = str(
                            (self.file_data or {}).get("content", "")  # type: ignore[call-overload]
                        )

            # Allow ``str(result)`` to return the rendered content.
            def __str__(self) -> str:
                return self.rendered_content or ""

            def __contains__(self, item: str) -> bool:
                return item in str(self)

            def __iter__(self):
                return iter(str(self))

            def __len__(self) -> int:
                return len(str(self))

    # 情形二：旧版 ReadResult 是 str 的子类 —— 用 __new__ 构造真正的字符串（渲染内容），
    # 再挂上 file_data/error 属性。
    elif _UPSTREAM_IS_STR_SUBCLASS:

        class ReadResult(str, _UpstreamReadResult):  # type: ignore[no-redef]
            file_data: FileData | None
            error: str | None

            def __new__(
                cls,
                *,
                file_data: FileData | None = None,
                error: str | None = None,
                rendered_content: str | None = None,
            ) -> "ReadResult":
                if rendered_content is None:
                    if error is not None:
                        rendered_content = (
                            error if error.startswith("Error:") else f"Error: {error}"
                        )
                    else:
                        rendered_content = str(
                            (file_data or {}).get("content", "")  # type: ignore[call-overload]
                        )

                obj = str.__new__(cls, rendered_content)
                obj.file_data = file_data
                obj.error = error
                return obj

    # 情形三：upstream 完全没有 ReadResult —— 直接用 str 子类自造一个等价实现。
    else:

        class ReadResult(str):  # type: ignore[no-redef]
            file_data: FileData | None
            error: str | None

            def __new__(
                cls,
                *,
                file_data: FileData | None = None,
                error: str | None = None,
                rendered_content: str | None = None,
            ) -> "ReadResult":
                if rendered_content is None:
                    if error is not None:
                        rendered_content = (
                            error if error.startswith("Error:") else f"Error: {error}"
                        )
                    else:
                        rendered_content = str(
                            (file_data or {}).get("content", "")  # type: ignore[call-overload]
                        )

                obj = str.__new__(cls, rendered_content)
                obj.file_data = file_data
                obj.error = error
                return obj


# 判断一个值是否为 read 结果（同时兼容 upstream 与本兼容层两种实现）。
def is_read_result(value: object) -> bool:
    """Return True for both upstream and compatibility-layer read results."""
    if _UPSTREAM_IS_DATACLASS or _UPSTREAM_IS_STR_SUBCLASS:
        return isinstance(value, _UpstreamReadResult)
    return isinstance(value, ReadResult)


# 把 read 结果渲染成面向用户的纯文本：有 error 时优先返回错误串（必要时补 "Error:" 前缀），
# 否则返回 rendered_content，最后退回 file_data["content"]。
def read_result_to_string(value: object) -> str:
    """Render upstream or compatibility-layer read results as user-facing text."""
    if not is_read_result(value):
        return str(value)

    error = getattr(value, "error", None)
    if error:
        return error if str(error).startswith("Error:") else f"Error: {error}"

    rendered = getattr(value, "rendered_content", None)
    if rendered is not None:
        return str(rendered)

    file_data = getattr(value, "file_data", None) or {}
    return str(file_data.get("content", ""))


# LambChat 在 deepagents 标准错误码之外扩展的沙箱文件错误码字面量集合。
ExtendedFileError = Literal[
    "file_not_found",
    "permission_denied",
    "is_directory",
    "invalid_path",
    "too_many_files",
    "file_too_large",
]


# 构造带 LambChat 扩展错误码的文件上传响应。
def file_upload_response(
    *,
    path: str,
    error: ExtendedFileError | None = None,
) -> FileUploadResponse:
    """Create an upload response with LambChat's extended sandbox error codes."""
    return FileUploadResponse(path=path, error=cast(Any, error))


# 构造带 LambChat 扩展错误码的文件下载响应。
def file_download_response(
    *,
    path: str,
    content: bytes | None = None,
    error: ExtendedFileError | None = None,
) -> FileDownloadResponse:
    """Create a download response with LambChat's extended sandbox error codes."""
    return FileDownloadResponse(path=path, content=content, error=cast(Any, error))


# Re-export upstream protocol types so that mypy treats our aliases as
# identical to the ones used in BaseSandbox / BackendProtocol signatures.
__all__ = [
    "BackendProtocol",
    "EditResult",
    "ExecuteResponse",
    "FileDownloadResponse",
    "FileInfo",
    "FileUploadResponse",
    "GlobResult",
    "GrepMatch",
    "GrepResult",
    "LsResult",
    "ReadResult",
    "WriteResult",
    "ExtendedFileError",
    "file_download_response",
    "file_upload_response",
    "is_read_result",
    "read_result_to_string",
]

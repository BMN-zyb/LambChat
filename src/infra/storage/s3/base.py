"""
Abstract base class for S3 storage backends.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Optional, Protocol

from src.infra.async_utils import run_blocking_io
from src.infra.storage.s3.types import UploadResult

# 单次 list_objects 调用最多返回的对象数量上限，避免一次性拉取超大目录导致内存/网络压力。
LIST_OBJECTS_LIMIT = 1000


class BinaryReadFile(Protocol):
    """Binary file-like object used as an upload source."""

    # 结构化协议（非继承式接口）：只要具备 read/seek/tell 方法（如内存中的 BytesIO、
    # 已打开的文件对象、FastAPI 的 UploadFile.file）就能作为上传数据源使用，无需显式继承任何基类。
    def read(self, size: int = -1, /) -> bytes: ...

    def seek(self, offset: int, whence: int = 0, /) -> int: ...

    def tell(self) -> int: ...


class BinaryWriteFile(Protocol):
    """Binary file-like object used as a download sink."""

    # 同理，任何具备 write/seek/tell 方法的对象都能作为下载写入目标，用于流式落盘避免整份加载到内存。
    def write(self, data: bytes, /) -> object: ...

    def seek(self, offset: int, whence: int = 0, /) -> int: ...

    def tell(self) -> int: ...


class S3StorageBackend(ABC):
    """Abstract base class for S3 storage backends"""

    # 这是所有具体存储后端（本地文件系统、阿里云 OSS、MinIO/兼容 S3 协议等）必须遵循的统一接口协议。
    # 上层 S3StorageService 只依赖这套接口编程，从而实现"多后端可插拔、按配置自动切换"，
    # 这是本模块要解决的核心抽象难点：让业务代码完全不关心底层到底连的是哪种对象存储。

    @abstractmethod
    async def upload(
        self,
        file: BinaryReadFile,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> UploadResult:
        """Upload a file"""
        pass

    @abstractmethod
    async def upload_bytes(
        self,
        data: bytes,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> UploadResult:
        """Upload bytes"""
        pass

    @abstractmethod
    async def download(self, key: str) -> bytes:
        """Download a file"""
        pass

    async def get_size(self, key: str) -> int:
        """Get file size in bytes. Override for efficient stat."""
        # 默认未实现：要求各后端提供不下载整个对象也能获知大小的高效实现（如 HEAD 请求/stat 系统调用）。
        raise NotImplementedError("S3 backends must implement get_size without downloading bytes")

    async def download_range(self, key: str, start: int, end: int) -> bytes:
        """Download a byte range [start, end]. Override for efficient range reads."""
        # 默认未实现：要求各后端提供 Range 请求能力，用于断点续传/按需读取大文件片段等场景。
        raise NotImplementedError(
            "S3 backends must implement download_range without downloading full objects"
        )

    async def download_stream(
        self, key: str, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        """Stream download a file in chunks. Override for memory-efficient streaming."""
        # 默认未实现（含一个不可达的 yield 只是为了让方法在类型上仍是异步生成器）：
        # 要求各后端提供真正的流式下载，避免大文件被整体加载进内存。
        raise NotImplementedError(
            "S3 backends must implement download_stream without downloading full objects"
        )
        yield b""

    async def download_to_file(
        self,
        key: str,
        file: BinaryWriteFile,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> int:
        """Download an object into a file-like sink without returning full bytes."""
        # 基于 download_stream 的通用默认实现：逐块写入目标文件对象，写完后指针归零，
        # 便于调用方直接从头读取；各后端通常无需重写此方法，只需实现 download_stream 即可复用。
        total_size = 0
        async for chunk in self.download_stream(key, chunk_size=chunk_size):
            await run_blocking_io(file.write, chunk)
            total_size += len(chunk)
        await run_blocking_io(file.seek, 0)
        return total_size

    async def download_range_stream(
        self, key: str, start: int, end: int, chunk_size: int = 256 * 1024
    ) -> AsyncIterator[bytes]:
        """Stream a byte range in chunks. Override for memory-efficient range streaming."""
        # 默认实现是"先完整下载该范围再切块吐出"，并不省内存；
        # 若后端能支持真正的范围流式读取，应重写此方法以获得更好的内存效率。
        data = await self.download_range(key, start, end)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete a file"""
        pass

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if a file exists"""
        pass

    @abstractmethod
    async def get_url(self, key: str) -> str:
        """Get public URL for a file"""
        pass

    @abstractmethod
    async def get_presigned_url(self, key: str, expires: int = 3600) -> str:
        """Get presigned URL for a file (for private buckets)"""
        pass

    @abstractmethod
    async def list_objects(self, prefix: str = "") -> list[str]:
        """List objects with given prefix"""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close the backend connection"""
        pass

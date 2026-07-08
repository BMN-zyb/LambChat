"""
Local filesystem storage backend.

Stores files on disk when S3 is not configured.
"""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 当项目没有配置任何对象存储（S3/MinIO/OSS）时，本后端把文件直接落到本机磁盘
# 的一个根目录（storage_path）下，key 即相对该根目录的路径，作为开发环境或
# 单机部署时的兜底方案。它同样实现 S3StorageBackend 协议，因此上层代码完全
# 无需关心当前用的是真实对象存储还是本地磁盘。
# 由于没有真正的对象存储服务对外提供 HTTP 访问，get_url/get_presigned_url
# 返回的都是指向本服务内部下载接口的相对路径，而不是可直接公网访问的地址。
# 另外，因为 key 是用户可控的相对路径，必须做路径穿越（path traversal）防护，
# 否则恶意 key（如 "../../etc/passwd"）可能读写到根目录之外的任意文件。
# ============================================================================

from __future__ import annotations

import io
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Optional, cast

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.storage.s3.base import (
    LIST_OBJECTS_LIMIT,
    BinaryReadFile,
    BinaryWriteFile,
    S3StorageBackend,
)
from src.infra.storage.s3.types import S3Config, UploadResult
from src.infra.utils.datetime import utc_now

logger = get_logger(__name__)
# 本地拷贝文件流时每次读取的块大小，避免大文件一次性整块读入内存
UPLOAD_COPY_CHUNK_SIZE = 1024 * 1024


class LocalStorageBackend(S3StorageBackend):
    """Local filesystem storage backend"""

    def __init__(self, config: S3Config):
        self.config = config
        # 统一解析为绝对路径，作为后续路径穿越校验的基准；目录不存在则自动创建
        self._base_path = Path(config.storage_path).resolve()
        self._base_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"LocalStorageBackend initialized at: {self._base_path}")

    def _get_file_path(self, key: str) -> Path:
        """Get the local file path for a given key, preventing path traversal."""
        # 关键安全校验：把 key 拼到根目录下再 resolve()（会展开 .. 等相对路径片段），
        # 若解析结果不再位于 _base_path 之下，说明 key 试图跳出根目录，直接拒绝
        target = (self._base_path / key).resolve()
        if not str(target).startswith(str(self._base_path)):
            raise ValueError(f"Invalid key: path traversal detected: {key}")
        return target

    async def upload(
        self,
        file: BinaryReadFile,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> UploadResult:
        file_path = self._get_file_path(key)
        # key 可能包含多级目录（如 "2024/01/xxx.png"），先确保父目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)

        def _write_stream() -> int:
            # 用 shutil.copyfileobj 分块拷贝，避免大文件整体读入内存；
            # 通过写入前后 file.tell() 的差值反推实际写入的字节数
            current_pos = file.tell()
            with open(file_path, "wb") as f:
                shutil.copyfileobj(file, f, length=UPLOAD_COPY_CHUNK_SIZE)
            file_size = file.tell() - current_pos
            try:
                # 尽量把源文件游标复位，方便调用方后续可能的重复读取；
                # 有些不支持 seek 的流对象会抛错，此时安静忽略即可
                file.seek(current_pos)
            except (OSError, ValueError):
                pass
            return file_size

        file_size = await run_blocking_io(_write_stream)

        return UploadResult(
            key=key,
            # 本地后端没有对外 HTTP 服务能直接访问磁盘文件，统一走内部下载接口
            url=f"/api/upload/file/{key}",
            size=file_size,
            content_type=content_type or "application/octet-stream",
            last_modified=utc_now(),
        )

    async def upload_bytes(
        self,
        data: bytes,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> UploadResult:
        # 便捷封装：把内存中的 bytes 包成类文件对象后复用 upload() 的落盘逻辑
        return await self.upload(io.BytesIO(data), key, content_type, metadata)

    async def download(self, key: str) -> bytes:
        file_path = self._get_file_path(key)

        def _read():
            # 读取前先校验文件大小，超过内部限制直接拒绝，避免一次性把超大文件读进内存
            size = file_path.stat().st_size
            if size > self.config.internal_max_upload_size:
                max_mb = self.config.internal_max_upload_size / (1024 * 1024)
                raise ValueError(
                    f"File size ({size / (1024 * 1024):.1f}MB) exceeds "
                    f"internal download limit ({max_mb:.0f}MB)"
                )
            with open(file_path, "rb") as f:
                return f.read()

        try:
            return await run_blocking_io(_read)
        except FileNotFoundError:
            # 统一异常信息格式，与其它后端（如 minio/aliyun）保持一致的"对象不存在"语义
            raise FileNotFoundError(f"Object {key} not found")

    async def download_to_file(
        self,
        key: str,
        file: BinaryWriteFile,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> int:
        file_path = self._get_file_path(key)

        def _copy() -> int:
            # 直接流式拷贝到调用方提供的可写文件对象（如临时文件），不经过内存中转，
            # 拷贝完成后把写入游标重置到开头，方便调用方紧接着从头读取
            with open(file_path, "rb") as source:
                shutil.copyfileobj(source, file, length=chunk_size)
            size = file.tell()
            file.seek(0)
            return size

        try:
            return await run_blocking_io(_copy)
        except FileNotFoundError:
            raise FileNotFoundError(f"Object {key} not found")

    async def get_size(self, key: str) -> int:
        file_path = self._get_file_path(key)
        return await run_blocking_io(lambda: file_path.stat().st_size)

    async def download_range(self, key: str, start: int, end: int) -> bytes:
        file_path = self._get_file_path(key)
        length = end - start + 1
        if length <= 0:
            return b""
        # 与 download() 同理，区间长度也要做上限校验，防止一次请求超大 range
        if length > self.config.internal_max_upload_size:
            max_mb = self.config.internal_max_upload_size / (1024 * 1024)
            raise ValueError(
                f"Range size ({length / (1024 * 1024):.1f}MB) exceeds "
                f"internal download limit ({max_mb:.0f}MB)"
            )

        def _read_range() -> bytes:
            # 本地文件天然支持随机访问，直接 seek 到起始位置再读取指定长度即可
            with open(file_path, "rb") as f:
                f.seek(max(0, start))
                return f.read(length)

        try:
            return await run_blocking_io(_read_range)
        except FileNotFoundError:
            raise FileNotFoundError(f"Object {key} not found")

    async def download_stream(
        self, key: str, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        file_path = self._get_file_path(key)
        chunk_size = max(1, int(chunk_size))
        try:
            # 文件句柄的打开/读取/关闭都是阻塞操作，逐一丢线程池执行；
            # 生成器方式边读边 yield，避免大文件一次性占满内存
            source = await run_blocking_io(open, file_path, "rb")
        except FileNotFoundError:
            raise FileNotFoundError(f"Object {key} not found")

        try:
            while True:
                chunk = cast(bytes, await run_blocking_io(source.read, chunk_size))
                if not chunk:
                    break
                yield chunk
        finally:
            # 消费者提前中断迭代（如客户端断连）时 finally 依然会执行，确保文件句柄被关闭
            await run_blocking_io(source.close)

    async def delete(self, key: str) -> bool:
        file_path = self._get_file_path(key)

        def _delete():
            if file_path.exists():
                file_path.unlink()
                # 删除文件后向上清理已变空的父目录（不越过 _base_path 根目录），
                # 避免长期运行后残留大量空的年/月/日子目录
                parent = file_path.parent
                while parent != self._base_path and parent.exists():
                    try:
                        # rmdir 只在目录为空时才会成功，非空目录会抛 OSError，
                        # 借此天然实现"只清理确实变空的目录"，无需手动判断是否为空
                        parent.rmdir()
                        parent = parent.parent
                    except OSError:
                        break
                return True
            return False

        return await run_blocking_io(_delete)

    async def exists(self, key: str) -> bool:
        return await run_blocking_io(self._get_file_path(key).exists)

    async def get_url(self, key: str) -> str:
        # 本地后端没有独立的静态资源服务，一律通过应用自身的下载接口访问
        return f"/api/upload/file/{key}"

    async def get_presigned_url(self, key: str, expires: int = 3600) -> str:
        # 本地文件系统没有"签名+有效期"的概念，expires 参数仅为兼容接口签名而保留，
        # 实际直接忽略，返回与 get_url 相同的固定路径
        _ = expires
        return f"/api/upload/file/{key}"

    async def list_objects(self, prefix: str = "") -> list[str]:
        prefix_path = self._base_path / prefix

        def _list():
            if not prefix_path.exists():
                return []
            objects = []
            # os.walk 递归遍历 prefix 目录下所有文件，逐层收集并转换为相对
            # _base_path 的路径字符串（与 key 的定义保持一致）
            for root, _dirs, files in os.walk(prefix_path):
                for fname in sorted(files):
                    full_path = Path(root) / fname
                    rel = full_path.relative_to(self._base_path)
                    objects.append(str(rel))
                    # 达到硬性上限立即返回，防止目录下文件过多时遍历耗时过长/占用过多内存
                    if len(objects) >= LIST_OBJECTS_LIMIT:
                        return objects
            return objects

        return await run_blocking_io(_list)

    async def close(self) -> None:
        # 本地文件系统没有需要释放的连接/客户端资源，空实现仅为满足接口协议
        pass

"""
Aliyun OSS storage backend using official oss2 library.
"""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 阿里云对象存储（OSS）后端，用官方 oss2 SDK 实现同一套 S3StorageBackend 协议。
# oss2 的 Bucket 客户端同样是同步（阻塞）API，思路与 minio.py 完全一致：所有
# 网络 IO 都通过 run_blocking_io 丢线程池执行。与 minio 后端的主要差异点：
#   - 鉴权用 oss2.Auth(access_key, secret_key)，而不是 minio.Minio 的构造参数；
#   - get_object 支持 byte_range 参数直接做区间读取（对应 HTTP Range 请求）；
#   - 额外提供 download_range_stream：区间流式下载（minio 后端未实现此接口）。
# ============================================================================

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Optional

import oss2

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.storage.s3.base import LIST_OBJECTS_LIMIT, BinaryReadFile, S3StorageBackend
from src.infra.storage.s3.types import S3Config, UploadResult
from src.infra.utils.datetime import utc_now

logger = get_logger(__name__)
# 兼容模式下载时的分块读取大小，避免大对象一次性整体读入内存
DOWNLOAD_COMPAT_CHUNK_SIZE = 1024 * 1024


class AliyunOssBackend(S3StorageBackend):
    """Aliyun OSS storage backend using official oss2 library"""

    def __init__(self, config: S3Config):
        self.config = config
        # 懒加载：bucket 客户端延迟到真正发起请求时才创建
        self._bucket = None

    def _get_bucket(self):
        """Get or create Aliyun OSS bucket"""
        if self._bucket is None:
            # 未显式配置 endpoint 时，按阿里云约定规则拼出区域默认 endpoint
            endpoint = self.config.endpoint_url or f"oss-{self.config.region}.aliyuncs.com"
            endpoint = endpoint.replace("https://", "").replace("http://", "")

            auth = oss2.Auth(self.config.access_key, self.config.secret_key)

            logger.info(
                f"Aliyun OSS client config: endpoint={endpoint}, bucket={self.config.bucket_name}, "
                f"region={self.config.region}"
            )

            self._bucket = oss2.Bucket(
                auth,
                f"https://{endpoint}",
                self.config.bucket_name,
                connect_timeout=30,
            )

        return self._bucket

    async def upload(
        self,
        file: BinaryReadFile,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> UploadResult:
        # 通过跳到文件末尾再跳回原位置的方式测量长度，不破坏调用方的读取游标；
        # oss2 的 put_object 本身支持直接传类文件对象流式上传，无需预先测量长度，
        # 这里测量只是为了填充 UploadResult.size 字段
        def _measure_size() -> int:
            current_pos = file.tell()
            file.seek(0, 2)
            file_size = file.tell() - current_pos
            file.seek(current_pos)
            return file_size

        file_size = await run_blocking_io(_measure_size)

        bucket = await run_blocking_io(self._get_bucket)

        def _put_object():
            # content_type 和自定义 metadata 都通过 HTTP headers 传递给 OSS
            headers = {}
            if content_type:
                headers["Content-Type"] = content_type
            if metadata:
                headers.update(metadata)
            return bucket.put_object(key, file, headers=headers)

        result = await run_blocking_io(_put_object)

        return UploadResult(
            key=key,
            url=self.config.get_public_url(key),
            size=file_size,
            content_type=content_type or "application/octet-stream",
            etag=result.etag,
            last_modified=utc_now(),
        )

    async def upload_bytes(
        self,
        data: bytes,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> UploadResult:
        # 便捷封装：把内存中的 bytes 包成类文件对象后复用 upload() 的完整上传逻辑
        return await self.upload(io.BytesIO(data), key, content_type, metadata)

    async def download(self, key: str) -> bytes:
        bucket = await run_blocking_io(self._get_bucket)

        # get_object 返回的是可分块读取的流对象，交给 _read_stream_chunks
        # 统一处理（含大小上限保护与资源释放）
        oss_stream = await run_blocking_io(lambda: bucket.get_object(key))
        return await self._read_stream_chunks(
            oss_stream,
            DOWNLOAD_COMPAT_CHUNK_SIZE,
            max_bytes=self.config.internal_max_upload_size,
        )

    async def get_size(self, key: str) -> int:
        bucket = await run_blocking_io(self._get_bucket)

        # head_object 是一次轻量的 HEAD 请求，只拿元数据不下载对象内容
        def _head():
            head = bucket.head_object(key)
            return head.content_length

        return await run_blocking_io(_head)

    async def download_range(self, key: str, start: int, end: int) -> bytes:
        bucket = await run_blocking_io(self._get_bucket)
        length = end - start + 1
        # 发起网络请求前先校验区间长度，超限直接拒绝，避免代价高昂却注定失败的下载
        if length > self.config.internal_max_upload_size:
            max_mb = self.config.internal_max_upload_size / (1024 * 1024)
            raise ValueError(
                f"Range size ({length / (1024 * 1024):.1f}MB) exceeds "
                f"internal download limit ({max_mb:.0f}MB)"
            )

        # byte_range=(start, end) 对应 HTTP Range 请求，只拉取对象的一部分
        oss_stream = await run_blocking_io(lambda: bucket.get_object(key, byte_range=(start, end)))
        return await self._read_stream_chunks(
            oss_stream,
            min(length, DOWNLOAD_COMPAT_CHUNK_SIZE),
            max_bytes=self.config.internal_max_upload_size,
        )

    async def _read_stream_chunks(self, oss_stream, chunk_size: int, *, max_bytes: int) -> bytes:
        # 供 download()/download_range() 共用的分块读取逻辑：边读边累加总大小，
        # 超过 max_bytes 立即中止并抛错，防止对象异常巨大时把进程内存撑爆
        chunks: list[bytes] = []
        total_size = 0
        try:
            while True:
                chunk = await run_blocking_io(lambda: oss_stream.read(chunk_size))
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > max_bytes:
                    max_mb = max_bytes / (1024 * 1024)
                    raise ValueError(
                        f"Response size ({total_size / (1024 * 1024):.1f}MB) exceeds "
                        f"internal download limit ({max_mb:.0f}MB)"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            # 无论成功、失败还是提前中止，都要关闭底层流以释放连接资源
            await run_blocking_io(oss_stream.close)

    async def download_stream(
        self, key: str, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        """Stream download from OSS using chunked reads."""
        # 真正的流式下载：边读边 yield，不在内存里攒完整个对象，适用于大文件场景
        bucket = await run_blocking_io(self._get_bucket)
        oss_stream = await run_blocking_io(lambda: bucket.get_object(key))
        try:
            while True:
                chunk = await run_blocking_io(lambda: oss_stream.read(chunk_size))
                if not chunk:
                    break
                yield chunk
        finally:
            # 消费者提前中断迭代时 finally 依然执行，确保连接被正确释放
            await run_blocking_io(oss_stream.close)

    async def download_range_stream(
        self, key: str, start: int, end: int, chunk_size: int = 256 * 1024
    ) -> AsyncIterator[bytes]:
        """Stream a byte range from OSS using chunked reads."""
        # 区间 + 流式下载的组合：既只拉取指定字节范围，又不在内存里攒完整段数据，
        # 适用于大文件的分片/断点续传场景；minio 后端目前未提供此组合能力
        bucket = await run_blocking_io(self._get_bucket)
        oss_stream = await run_blocking_io(lambda: bucket.get_object(key, byte_range=(start, end)))
        try:
            while True:
                chunk = await run_blocking_io(lambda: oss_stream.read(chunk_size))
                if not chunk:
                    break
                yield chunk
        finally:
            await run_blocking_io(oss_stream.close)

    async def delete(self, key: str) -> bool:
        bucket = await run_blocking_io(self._get_bucket)

        def _delete_object():
            bucket.delete_object(key)
            return True

        return await run_blocking_io(_delete_object)

    async def exists(self, key: str) -> bool:
        bucket = await run_blocking_io(self._get_bucket)

        # oss2 提供了现成的 object_exists 方法，无需像 minio 后端那样自己拿 stat 兜底判断
        def _exists():
            return bucket.object_exists(key)

        return await run_blocking_io(_exists)

    async def get_url(self, key: str) -> str:
        # 直接拼公开访问 URL（不含签名），仅适用于 bucket 已配置为公开可读的场景
        return self.config.get_public_url(key)

    async def get_presigned_url(self, key: str, expires: int = 3600) -> str:
        bucket = await run_blocking_io(self._get_bucket)

        def _get_url():
            # sign_url 生成带签名的临时访问链接；response-content-disposition=inline
            # 让浏览器直接预览（如图片/PDF），而不是触发下载弹窗
            return bucket.sign_url(
                "GET",
                key,
                expires,
                params={"response-content-disposition": "inline"},
            )

        return await run_blocking_io(_get_url)

    async def list_objects(self, prefix: str = "") -> list[str]:
        bucket = await run_blocking_io(self._get_bucket)

        def _list_objects():
            objects = []
            # ObjectIterator 内部自动处理分页（OSS 单次 List 有数量上限），
            # 这里叠加一层硬性上限，防止 bucket 里对象过多时遍历耗时过长
            for obj in oss2.ObjectIterator(bucket, prefix=prefix):
                objects.append(obj.key)
                if len(objects) >= LIST_OBJECTS_LIMIT:
                    break
            return objects

        return await run_blocking_io(_list_objects)

    async def close(self) -> None:
        # 丢弃 bucket 客户端引用，下次调用 _get_bucket 时会重新创建
        self._bucket = None

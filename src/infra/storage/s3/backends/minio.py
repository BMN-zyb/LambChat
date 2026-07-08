"""
S3 storage backend using minio library.

Compatible with AWS S3, MinIO, Tencent COS, and any S3-compatible provider.
"""

# ============================================================================
# 模块说明
# ----------------------------------------------------------------------------
# 本文件是对象存储抽象层（S3StorageBackend 协议）的一种具体实现，底层用
# minio-py 这个官方 SDK 去对接任意兼容 S3 协议的服务端（AWS S3、MinIO、
# 腾讯云 COS 等）。minio 库的客户端是同步（阻塞）API，因此本文件里几乎每一次
# 网络调用都通过 run_blocking_io 丢进线程池执行，避免阻塞 asyncio 事件循环——
# 这是全文件最核心的设计约束，贯穿几乎每个方法。
# 与之并列的还有 local.py（本地文件系统兜底）、aliyun.py（阿里云 OSS SDK）等
# 后端，它们实现同一套 S3StorageBackend 接口，可按配置互换。
# ============================================================================

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Optional

from src.infra.async_utils import run_blocking_io
from src.infra.logging import get_logger
from src.infra.storage.s3.base import LIST_OBJECTS_LIMIT, BinaryReadFile, S3StorageBackend
from src.infra.storage.s3.types import S3Config, S3Provider, UploadResult
from src.infra.utils.datetime import utc_now

if TYPE_CHECKING:
    import minio

logger = get_logger(__name__)
# 分片上传时单个分片的大小（10MB），minio 客户端超过此大小的文件会自动切分为多次分片上传
UPLOAD_PART_SIZE = 10 * 1024 * 1024
# 兼容模式下载时的分块读取大小（1MB），用于流式读取响应体，避免一次性把大对象读入内存
DOWNLOAD_COMPAT_CHUNK_SIZE = 1024 * 1024


class MinioS3Backend(S3StorageBackend):
    """S3 storage backend using minio library"""

    def __init__(self, config: S3Config):
        self.config = config
        # 客户端懒加载：构造时不立即建连，等真正发起请求时才创建，避免不必要的初始化开销
        self._client: minio.Minio | None = None

    def _get_client(self):
        """Get or create minio S3 client"""
        if self._client is None:
            import minio

            # minio.Minio 的 endpoint 参数只接受 host:port，不接受 http(s):// 前缀，
            # 所以这里手动剥掉协议头；拿不到任何 endpoint 配置时兜底本机 MinIO 默认端口
            endpoint: str | None = self.config.endpoint_url or self.config.get_endpoint_url()
            if endpoint:
                endpoint = endpoint.replace("https://", "").replace("http://", "")
            else:
                endpoint = "localhost:9000"

            logger.info(
                f"Minio client config: endpoint={endpoint}, bucket={self.config.bucket_name}, "
                f"region={self.config.region}, access_key length={len(self.config.access_key)}"
            )

            self._client = minio.Minio(
                endpoint=endpoint,
                access_key=self.config.access_key,
                secret_key=self.config.secret_key,
                secure=True,
                # AWS 会根据 endpoint/bucket 自动探测region，显式传 region 反而可能与其
                # 自动探测结果冲突；非 AWS 的 S3 兼容服务（如自建 MinIO）则需要显式指定
                region=(self.config.region if self.config.provider != S3Provider.AWS else None),
            )

        return self._client

    async def upload(
        self,
        file: BinaryReadFile,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> UploadResult:
        import mimetypes

        # 未显式指定 content_type 时，根据 key 的文件扩展名猜测；猜不出来则兜底为通用二进制类型
        if not content_type:
            content_type, _ = mimetypes.guess_type(key)
            if not content_type:
                content_type = "application/octet-stream"

        # minio 的 put_object 需要预先知道文件总长度（用于内部判断是否要分片上传），
        # 这里通过先跳到文件末尾算出大小、再跳回原始位置的方式测量，不破坏调用方的读取游标
        def _measure_size() -> int:
            current_pos = file.tell()
            file.seek(0, 2)
            file_size = file.tell() - current_pos
            file.seek(current_pos)
            return file_size

        file_size = await run_blocking_io(_measure_size)

        client = await run_blocking_io(self._get_client)

        # put_object 本身是阻塞调用（内部同步做 HTTP 上传），必须丢线程池执行；
        # 超过 UPLOAD_PART_SIZE 时 minio 客户端会自动切分为分片上传
        def _put_object():
            return client.put_object(
                bucket_name=self.config.bucket_name,
                object_name=key,
                data=file,
                length=file_size,
                part_size=UPLOAD_PART_SIZE,
                content_type=content_type,
                metadata=metadata or {},
            )

        result = await run_blocking_io(_put_object)

        return UploadResult(
            key=key,
            url=self.config.get_public_url(key),
            size=file_size,
            content_type=content_type,
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
        client = await run_blocking_io(self._get_client)

        # get_object 返回的是一个类似 HTTPResponse 的流对象，需要手动读取并释放连接，
        # 这里统一交给 _read_response_chunks 处理（含大小上限保护与资源释放）
        response = await run_blocking_io(
            lambda: client.get_object(
                bucket_name=self.config.bucket_name,
                object_name=key,
            )
        )
        return await self._read_response_chunks(
            response,
            DOWNLOAD_COMPAT_CHUNK_SIZE,
            max_bytes=self.config.internal_max_upload_size,
        )

    async def get_size(self, key: str) -> int:
        client = await run_blocking_io(self._get_client)

        # stat_object 是一次轻量的 HEAD 请求，只拿元数据不下载对象内容
        def _stat():
            stat = client.stat_object(
                bucket_name=self.config.bucket_name,
                object_name=key,
            )
            return stat.size

        return await run_blocking_io(_stat)

    async def download_range(self, key: str, start: int, end: int) -> bytes:
        client = await run_blocking_io(self._get_client)
        length = end - start + 1
        # 在发起实际网络请求前就先校验请求的区间长度，超限直接拒绝，
        # 避免因客户端传入的 range 过大而触发一次代价高昂却注定失败的下载
        if length > self.config.internal_max_upload_size:
            max_mb = self.config.internal_max_upload_size / (1024 * 1024)
            raise ValueError(
                f"Range size ({length / (1024 * 1024):.1f}MB) exceeds "
                f"internal download limit ({max_mb:.0f}MB)"
            )

        # offset/length 对应 HTTP Range 请求，只拉取对象的一部分（如断点续传、分片读取场景）
        response = await run_blocking_io(
            lambda: client.get_object(
                bucket_name=self.config.bucket_name,
                object_name=key,
                offset=start,
                length=length,
            )
        )
        return await self._read_response_chunks(
            response,
            min(length, DOWNLOAD_COMPAT_CHUNK_SIZE),
            max_bytes=self.config.internal_max_upload_size,
        )

    async def _read_response_chunks(self, response, chunk_size: int, *, max_bytes: int) -> bytes:
        # 供 download()/download_range() 共用的分块读取逻辑：边读边累加总大小，
        # 一旦超过 max_bytes 立即中止并抛错，防止响应体异常巨大时把进程内存撑爆
        chunks: list[bytes] = []
        total_size = 0
        try:
            while True:
                chunk = await run_blocking_io(lambda: response.read(chunk_size))
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
            # 无论成功、失败还是提前中止，都必须关闭响应并释放底层连接回连接池，
            # 否则会造成连接泄漏（urllib3 连接池被占满后新请求会被阻塞或报错）
            close = getattr(response, "close", None)
            if close is not None:
                await run_blocking_io(close)
            release_conn = getattr(response, "release_conn", None)
            if release_conn is not None:
                await run_blocking_io(release_conn)

    async def download_stream(
        self, key: str, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        # 与 download() 不同：这里是真正的流式下载，边读边 yield，不在内存里攒完整个对象，
        # 适用于大文件场景（如产物下载、导出文件），避免一次性占用过多内存
        client = await run_blocking_io(self._get_client)
        response = await run_blocking_io(
            lambda: client.get_object(
                bucket_name=self.config.bucket_name,
                object_name=key,
            )
        )
        try:
            while True:
                chunk = await run_blocking_io(lambda: response.read(chunk_size))
                if not chunk:
                    break
                yield chunk
        finally:
            # 同 _read_response_chunks：生成器提前被消费者中断（如客户端断连）时
            # finally 依然会执行，确保连接总能被正确释放
            close = getattr(response, "close", None)
            if close is not None:
                await run_blocking_io(close)
            release_conn = getattr(response, "release_conn", None)
            if release_conn is not None:
                await run_blocking_io(release_conn)

    async def delete(self, key: str) -> bool:
        client = await run_blocking_io(self._get_client)

        def _delete_object():
            client.remove_object(
                bucket_name=self.config.bucket_name,
                object_name=key,
            )
            return True

        return await run_blocking_io(_delete_object)

    async def exists(self, key: str) -> bool:
        client = await run_blocking_io(self._get_client)

        # 用 stat_object 探测对象是否存在：对象不存在时 minio 会抛异常（如 S3Error），
        # 这里统一吞掉所有异常返回 False——简化实现的代价是网络错误等场景也会被误判为"不存在"
        def _stat_object():
            try:
                client.stat_object(
                    bucket_name=self.config.bucket_name,
                    object_name=key,
                )
                return True
            except Exception:
                return False

        return await run_blocking_io(_stat_object)

    async def get_url(self, key: str) -> str:
        # 直接拼公开访问 URL（不含签名），仅适用于 bucket/对象已配置为公开可读的场景
        return self.config.get_public_url(key)

    async def get_presigned_url(self, key: str, expires: int = 3600) -> str:
        client = await run_blocking_io(self._get_client)

        # 生成带签名、限时有效的临时下载链接，无需暴露 access_key/secret_key
        # 即可让第三方（如前端浏览器）直接访问私有对象
        def _presigned_url():
            from datetime import timedelta

            return client.presigned_get_object(
                bucket_name=self.config.bucket_name,
                object_name=key,
                expires=timedelta(seconds=expires),
            )

        return await run_blocking_io(_presigned_url)

    async def list_objects(self, prefix: str = "") -> list[str]:
        client = await run_blocking_io(self._get_client)

        def _list_objects():
            objects = []
            # recursive=True：像遍历文件系统一样递归列出 prefix 下所有"子目录"里的对象，
            # 而不是只列出 prefix 直接子层级
            for obj in client.list_objects(
                bucket_name=self.config.bucket_name,
                prefix=prefix,
                recursive=True,
            ):
                objects.append(obj.object_name)
                # 达到硬性上限就提前跳出，防止 bucket 里对象过多时一次性拉爆内存/耗时过长
                if len(objects) >= LIST_OBJECTS_LIMIT:
                    break
            return objects

        return await run_blocking_io(_list_objects)

    async def close(self) -> None:
        # 直接丢弃客户端引用（minio.Minio 没有显式的 close/disconnect 方法），
        # 下次调用 _get_client 时会重新创建一个新的客户端实例
        self._client = None

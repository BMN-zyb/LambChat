# Storage Configuration

File storage and upload settings. LambChat supports local filesystem storage and S3-compatible object storage.

## S3 Object Storage

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `S3_ENABLED` | `false` | No | Enable S3 object storage. |
| `S3_PROVIDER` | `aws` | No | S3 provider: `aws`, `aliyun`, `tencent`, `minio`, `custom`. |
| `S3_ENDPOINT_URL` | _(empty)_ | No | S3 endpoint URL. **Required for MinIO and custom providers.** |
| `S3_ACCESS_KEY` | _(empty)_ | Yes | S3 access key. |
| `S3_SECRET_KEY` | _(empty)_ | Yes | S3 secret key. |
| `S3_REGION` | `us-east-1` | No | S3 region. |
| `S3_BUCKET_NAME` | _(empty)_ | No | S3 bucket name. |
| `S3_CUSTOM_DOMAIN` | _(empty)_ | No | Custom CDN domain for public URLs. |
| `S3_PATH_STYLE` | `false` | No | Use path-style addressing. **Required for MinIO.** |
| `S3_MAX_FILE_SIZE` | `10485760` (10MB) | No | Maximum file upload size in bytes. |
| `S3_INTERNAL_UPLOAD_MAX_SIZE` | `52428800` (50MB) | No | Maximum internal upload size in bytes. |
| `S3_PUBLIC_BUCKET` | `false` | No | Whether the bucket is publicly accessible. |
| `S3_PRESIGNED_URL_EXPIRES` | `604800` (7 days) | No | Presigned URL expiration in seconds. |

## Local Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `LOCAL_STORAGE_PATH` | `./uploads` | Path for local file storage. Used when `S3_ENABLED=false`. |
| `ENABLE_LOCAL_FILESYSTEM_FALLBACK` | `true` | Enable local filesystem fallback when S3 is unavailable. |

## File Upload Limits

| Variable | Default | Description |
|----------|---------|-------------|
| `FILE_UPLOAD_MAX_SIZE_IMAGE` | `10` | Maximum image file size in MB. |
| `FILE_UPLOAD_MAX_SIZE_VIDEO` | `100` | Maximum video file size in MB. |
| `FILE_UPLOAD_MAX_SIZE_AUDIO` | `50` | Maximum audio file size in MB. |
| `FILE_UPLOAD_MAX_SIZE_DOCUMENT` | `50` | Maximum document file size in MB. |
| `FILE_UPLOAD_MAX_FILES` | `10` | Maximum number of files per upload. |

## Examples

### AWS S3

```bash
S3_ENABLED=true
S3_PROVIDER=aws
S3_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE
S3_SECRET_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
S3_REGION=us-east-1
S3_BUCKET_NAME=lambchat-files
```

### MinIO

```bash
S3_ENABLED=true
S3_PROVIDER=minio
S3_ENDPOINT_URL=http://localhost:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET_NAME=lambchat
S3_PATH_STYLE=true
```

### Aliyun OSS

```bash
S3_ENABLED=true
S3_PROVIDER=aliyun
S3_ACCESS_KEY=your_access_key
S3_SECRET_KEY=your_secret_key
S3_REGION=oss-cn-hangzhou
S3_BUCKET_NAME=lambchat-files
```

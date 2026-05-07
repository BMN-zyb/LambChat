# 存储配置

文件存储和上传设置。LambChat 支持本地文件系统存储和 S3 兼容对象存储。

## S3 对象存储

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `S3_ENABLED` | `false` | 否 | 启用 S3 对象存储。 |
| `S3_PROVIDER` | `aws` | 否 | S3 提供商：`aws`、`aliyun`、`tencent`、`minio`、`custom`。 |
| `S3_ENDPOINT_URL` | _(空)_ | 否 | S3 端点 URL。**MinIO 和自定义提供商必须填写。** |
| `S3_ACCESS_KEY` | _(空)_ | 是 | S3 访问密钥。 |
| `S3_SECRET_KEY` | _(空)_ | 是 | S3 密钥。 |
| `S3_REGION` | `us-east-1` | 否 | S3 区域。 |
| `S3_BUCKET_NAME` | _(空)_ | 否 | S3 存储桶名称。 |
| `S3_CUSTOM_DOMAIN` | _(空)_ | 否 | 自定义 CDN 域名。 |
| `S3_PATH_STYLE` | `false` | 否 | 使用路径样式寻址。**MinIO 必须开启。** |
| `S3_MAX_FILE_SIZE` | `10485760`（10MB） | 否 | 最大文件上传大小（字节）。 |
| `S3_INTERNAL_UPLOAD_MAX_SIZE` | `52428800`（50MB） | 否 | 最大内部上传大小（字节）。 |
| `S3_PUBLIC_BUCKET` | `false` | 否 | 存储桶是否公开可访问。 |
| `S3_PRESIGNED_URL_EXPIRES` | `604800`（7 天） | 否 | 预签名 URL 过期时间（秒）。 |

## 本地存储

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `LOCAL_STORAGE_PATH` | `./uploads` | 本地文件存储路径。当 `S3_ENABLED=false` 时使用。 |
| `ENABLE_LOCAL_FILESYSTEM_FALLBACK` | `true` | 当 S3 不可用时启用本地文件系统回退。 |

## 文件上传限制

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `FILE_UPLOAD_MAX_SIZE_IMAGE` | `10` | 最大图片文件大小（MB）。 |
| `FILE_UPLOAD_MAX_SIZE_VIDEO` | `100` | 最大视频文件大小（MB）。 |
| `FILE_UPLOAD_MAX_SIZE_AUDIO` | `50` | 最大音频文件大小（MB）。 |
| `FILE_UPLOAD_MAX_SIZE_DOCUMENT` | `50` | 最大文档文件大小（MB）。 |
| `FILE_UPLOAD_MAX_FILES` | `10` | 每次上传的最大文件数。 |

## 示例

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

### 阿里云 OSS

```bash
S3_ENABLED=true
S3_PROVIDER=aliyun
S3_ACCESS_KEY=your_access_key
S3_SECRET_KEY=your_secret_key
S3_REGION=oss-cn-hangzhou
S3_BUCKET_NAME=lambchat-files
```

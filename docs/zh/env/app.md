# 应用配置

LambChat 服务器的基础应用配置。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DEBUG` | `false` | 启用调试/热重载模式。开启后会增加日志详细程度并在开发模式下启用热重载。 |
| `HOST` | `0.0.0.0` | 服务器绑定地址。设为 `127.0.0.1` 可限制仅本地访问。 |
| `PORT` | `8000` | 服务器绑定端口。 |
| `APP_BASE_URL` | _(空)_ | 用于生成文件 URL 的基础地址。**使用反向代理时必须填写。** 例如：`https://lambchat.example.com` |
| `APP_NAME` | `LambChat` | 应用名称（只读，硬编码）。 |
| `APP_VERSION` | _(自动)_ | 启动时从 `pyproject.toml` 自动读取。 |
| `LOG_LEVEL` | `INFO` | 日志级别。可选：`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`。 |

## 示例

```bash
# .env
DEBUG=false
HOST=0.0.0.0
PORT=8000
APP_BASE_URL=https://lambchat.example.com
LOG_LEVEL=INFO
```

::: tip
当使用反向代理（nginx、Traefik、Cloudflare Tunnel）部署时，务必将 `APP_BASE_URL` 设置为公网可访问的 URL。这确保文件上传 URL、分享链接和 OAuth 回调正常工作。
:::

# 沙箱配置

安全远程代码执行的沙箱设置。支持 Daytona 和 E2B 平台。

## 通用设置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ENABLE_SANDBOX` | `false` | 启用沙箱执行。 |
| `SANDBOX_PLATFORM` | `daytona` | 沙箱平台：`daytona` 或 `e2b`。 |
| `SANDBOX_GREP_TIMEOUT` | `30` | 沙箱 grep 命令超时时间（秒）。 |

## Daytona

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `DAYTONA_API_KEY` | _(空)_ | 是 | Daytona API 密钥。 |
| `DAYTONA_SERVER_URL` | _(空)_ | 否 | Daytona 服务器 URL。 |
| `DAYTONA_TIMEOUT` | `180` | 否 | 命令超时时间（秒），默认 3 分钟。 |
| `DAYTONA_IMAGE` | _(空)_ | 否 | 使用的沙箱镜像/快照 ID。 |
| `DAYTONA_AUTO_STOP_INTERVAL` | `5` | 否 | 自动停止间隔（分钟）。 |
| `DAYTONA_AUTO_ARCHIVE_INTERVAL` | `5` | 否 | 自动归档间隔（分钟）。 |
| `DAYTONA_AUTO_DELETE_INTERVAL` | `1440` | 否 | 自动删除间隔（分钟），默认 24 小时。 |

## E2B

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `E2B_API_KEY` | _(空)_ | 是 | E2B API 密钥。 |
| `E2B_TEMPLATE` | `base` | 否 | 沙箱模板名称。 |
| `E2B_TIMEOUT` | `3600` | 否 | 沙箱超时时间（秒），默认 1 小时。 |
| `E2B_AUTO_PAUSE` | `true` | 否 | 超时时暂停沙箱而非终止（保留状态）。 |
| `E2B_AUTO_RESUME` | `true` | 否 | 下次活动时自动恢复暂停的沙箱。 |

## 示例

### Daytona（自托管）

```bash
ENABLE_SANDBOX=true
SANDBOX_PLATFORM=daytona
DAYTONA_API_KEY=your_daytona_api_key
DAYTONA_SERVER_URL=https://daytona.example.com
DAYTONA_TIMEOUT=180
```

### E2B（云服务）

```bash
ENABLE_SANDBOX=true
SANDBOX_PLATFORM=e2b
E2B_API_KEY=your_e2b_api_key
E2B_TEMPLATE=base
E2B_TIMEOUT=3600
```

::: info
`DAYTONA_AUTO_*_INTERVAL` 设置控制沙箱生命周期管理以优化资源使用。沙箱会根据这些间隔自动停止、归档和最终删除。
:::

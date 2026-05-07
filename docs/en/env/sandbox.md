# Sandbox Configuration

Code sandbox settings for secure remote code execution. Supports Daytona and E2B platforms.

## General

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_SANDBOX` | `false` | Enable sandbox execution. |
| `SANDBOX_PLATFORM` | `daytona` | Sandbox platform: `daytona` or `e2b`. |
| `SANDBOX_GREP_TIMEOUT` | `30` | Sandbox grep command timeout in seconds. |

## Daytona

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `DAYTONA_API_KEY` | _(empty)_ | Yes | Daytona API key. |
| `DAYTONA_SERVER_URL` | _(empty)_ | No | Daytona server URL. |
| `DAYTONA_TIMEOUT` | `180` | No | Command timeout in seconds (3 minutes). |
| `DAYTONA_IMAGE` | _(empty)_ | No | Sandbox image/snapshot ID to use. |
| `DAYTONA_AUTO_STOP_INTERVAL` | `5` | No | Auto-stop interval in minutes. |
| `DAYTONA_AUTO_ARCHIVE_INTERVAL` | `5` | No | Auto-archive interval in minutes. |
| `DAYTONA_AUTO_DELETE_INTERVAL` | `1440` | No | Auto-delete interval in minutes (24 hours). |

## E2B

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `E2B_API_KEY` | _(empty)_ | Yes | E2B API key. |
| `E2B_TEMPLATE` | `base` | No | Sandbox template name. |
| `E2B_TIMEOUT` | `3600` | No | Sandbox timeout in seconds (1 hour). |
| `E2B_AUTO_PAUSE` | `true` | No | Pause sandbox on timeout instead of killing (preserves state). |
| `E2B_AUTO_RESUME` | `true` | No | Auto-resume paused sandbox on next activity. |

## Examples

### Daytona (Self-hosted)

```bash
ENABLE_SANDBOX=true
SANDBOX_PLATFORM=daytona
DAYTONA_API_KEY=your_daytona_api_key
DAYTONA_SERVER_URL=https://daytona.example.com
DAYTONA_TIMEOUT=180
```

### E2B (Cloud)

```bash
ENABLE_SANDBOX=true
SANDBOX_PLATFORM=e2b
E2B_API_KEY=your_e2b_api_key
E2B_TEMPLATE=base
E2B_TIMEOUT=3600
```

::: info
The `DAYTONA_AUTO_*_INTERVAL` settings control sandbox lifecycle management to optimize resource usage. Sandboxes are automatically stopped, archived, and eventually deleted based on these intervals.
:::

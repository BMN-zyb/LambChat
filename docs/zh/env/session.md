# 会话配置

管理聊天会话、事件流和会话标题的设置。

## 会话限制

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SESSION_MAX_RUNS_PER_SESSION` | `100` | 每个会话的最大 Agent 运行次数。 |
| `SESSION_MAX_MESSAGES` | `20` | 每个会话加载的最大消息数（内部配置，不在 `.env` 中）。 |
| `SESSION_MAX_EVENTS_PER_TRACE` | `10000` | 每个 trace 保留的最大事件数，防止内存溢出。 |

## 消息历史

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ENABLE_MESSAGE_HISTORY` | `true` | 启用消息历史存储。 |
| `SSE_CACHE_TTL` | `3600` | Redis 中 SSE 事件的 TTL（秒），默认 1 小时。 |

## 事件合并

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ENABLE_EVENT_MERGER` | `true` | 启用事件合并以减少冗余 SSE 事件。 |
| `EVENT_MERGE_INTERVAL` | `300.0` | 合并间隔（秒）。 |

## 会话标题生成

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SESSION_TITLE_MODEL` | `claude-3-5-haiku-20241022` | 用于生成会话标题的模型。 |
| `SESSION_TITLE_API_BASE` | _(空)_ | 标题生成使用的独立 API 基础 URL。留空则使用默认 LLM 配置。 |
| `SESSION_TITLE_API_KEY` | _(空)_ | 标题生成使用的独立 API 密钥。**敏感信息。** |
| `SESSION_TITLE_PROMPT` | _(长中文提示)_ | 标题生成的提示模板。支持 `{lang}` 和 `{message}` 占位符。 |

::: tip
你可以通过设置 `SESSION_TITLE_MODEL` 和可选的 `SESSION_TITLE_API_BASE` + `SESSION_TITLE_API_KEY`，使用更便宜/更快的模型（如 `gpt-4o-mini`）来生成会话标题。
:::

## 示例

```bash
# .env
SESSION_MAX_RUNS_PER_SESSION=100
ENABLE_MESSAGE_HISTORY=true
SSE_CACHE_TTL=3600
ENABLE_EVENT_MERGER=true
EVENT_MERGE_INTERVAL=300.0
SESSION_TITLE_MODEL=gpt-4o-mini
```

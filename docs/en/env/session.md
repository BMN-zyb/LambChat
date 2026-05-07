# Session Configuration

Settings for managing chat sessions, event streaming, and session titles.

## Session Limits

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_MAX_RUNS_PER_SESSION` | `100` | Maximum agent runs per session. |
| `SESSION_MAX_MESSAGES` | `20` | Maximum messages loaded per session (internal, not in `.env`). |
| `SESSION_MAX_EVENTS_PER_TRACE` | `10000` | Maximum events per trace to prevent memory overflow. |

## Message History

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_MESSAGE_HISTORY` | `true` | Enable message history storage. |
| `SSE_CACHE_TTL` | `3600` | Redis TTL for SSE events in seconds (1 hour). |

## Event Merger

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_EVENT_MERGER` | `true` | Enable event merging to reduce redundant SSE events. |
| `EVENT_MERGE_INTERVAL` | `300.0` | Merge interval in seconds. |

## Session Title Generation

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_TITLE_MODEL` | `claude-3-5-haiku-20241022` | Model used for generating session titles. |
| `SESSION_TITLE_API_BASE` | _(empty)_ | Separate API base URL for title generation. Falls back to default LLM config. |
| `SESSION_TITLE_API_KEY` | _(empty)_ | Separate API key for title generation. **Sensitive.** |
| `SESSION_TITLE_PROMPT` | _(long Chinese prompt)_ | Prompt template for title generation. Supports `{lang}` and `{message}` placeholders. |

::: tip
You can use a cheaper/faster model (like `gpt-4o-mini`) for session title generation by setting `SESSION_TITLE_MODEL` and optionally `SESSION_TITLE_API_BASE` + `SESSION_TITLE_API_KEY` to a separate provider.
:::

## Example

```bash
# .env
SESSION_MAX_RUNS_PER_SESSION=100
ENABLE_MESSAGE_HISTORY=true
SSE_CACHE_TTL=3600
ENABLE_EVENT_MERGER=true
EVENT_MERGE_INTERVAL=300.0
SESSION_TITLE_MODEL=gpt-4o-mini
```

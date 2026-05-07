# MCP & Tools Configuration

Model Context Protocol (MCP) and tool system settings.

## MCP Settings

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `ENABLE_MCP` | `true` | No | Enable MCP tool system. |
| `MCP_ENCRYPTION_SALT` | _(auto-generated)_ | Yes | Salt for encrypting MCP secrets. Auto-generated if not set. **Recommended to set for consistency across restarts.** |

## Deferred Tool Loading

For MCP servers with many tools, deferred loading reduces prompt size by loading tools on-demand.

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_DEFERRED_TOOL_LOADING` | `true` | Enable deferred/lazy tool loading. |
| `DEFERRED_TOOL_THRESHOLD` | `20` | Tool count threshold to trigger deferred loading. |
| `DEFERRED_TOOL_SEARCH_LIMIT` | `25` | Maximum tools returned in a search. |
| `DEFERRED_TOOL_PROMPT_LIMIT` | `25` | Maximum tools included in a prompt. |

## Skills

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_SKILLS` | `true` | Enable the skills system. |

## Audio Transcription

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `ENABLE_AUDIO_TRANSCRIPTION` | `false` | No | Enable audio transcription tool. |
| `AUDIO_TRANSCRIPTION_API_KEY` | _(empty)_ | Yes | Transcription API key. |
| `AUDIO_TRANSCRIPTION_BASE_URL` | _(empty)_ | No | Transcription API base URL. |
| `AUDIO_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | No | Transcription model name. |

## Example

```bash
# MCP
ENABLE_MCP=true
MCP_ENCRYPTION_SALT=your-random-salt-here

# Skills
ENABLE_SKILLS=true

# Audio Transcription (optional)
ENABLE_AUDIO_TRANSCRIPTION=true
AUDIO_TRANSCRIPTION_API_KEY=sk-your-key
AUDIO_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
```

::: tip
Set `MCP_ENCRYPTION_SALT` to a stable value in production. If it changes, previously encrypted MCP credentials will become unreadable.
:::

# LLM Configuration

Settings for controlling how LambChat interacts with language models.

## Model Provider Keys

These are consumed by the underlying LLM SDK libraries directly (not by the Settings class):

| Variable | Description |
|----------|-------------|
| `LLM_API_KEY` | Default LLM API key (consumed by LiteLLM) |
| `LLM_API_BASE` | Default LLM API base URL (consumed by LiteLLM) |
| `LLM_MODEL` | Default LLM model name, e.g. `anthropic/claude-sonnet-4-6` |
| `ANTHROPIC_API_KEY` | Anthropic API key (consumed by `langchain-anthropic`) |
| `ANTHROPIC_BASE_URL` | Anthropic-compatible API base URL |

::: tip
LambChat supports multi-model management through the UI. The env vars above set the **default** provider. Users can add additional providers and models at runtime through the settings panel.
:::

## Retry & Cache Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_MAX_RETRIES` | `3` | Maximum number of API retries on failure. |
| `LLM_RETRY_DELAY` | `1.0` | Delay between retries in seconds. |
| `LLM_MODEL_CACHE_SIZE` | `50` | Model instance cache size. Prevents memory leaks from repeated instantiation. |
| `LLM_MAX_INPUT_TOKENS` | _(none)_ | Optional: context window size for DeepAgent auto-summarization. |
| `LLM_TEMPERATURE` | _(none)_ | Optional: default temperature for LLM calls. |
| `LLM_MAX_TOKENS` | _(none)_ | Optional: max output tokens for LLM calls. |

## Prompt Cache Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PROMPT_CACHE_MAX_SYSTEM_BLOCKS` | `12` | Maximum cached system prompt blocks. |
| `PROMPT_CACHE_MAX_TOOLS` | `12` | Maximum cached tool definitions. |
| `DEEPAGENT_DEFAULT_MAX_INPUT_TOKENS` | `64000` | Default max input tokens for DeepAgent. |

## Example

```bash
# .env
LLM_API_KEY=sk-your-api-key
LLM_API_BASE=https://api.openai.com/v1
LLM_MODEL=gpt-4o
LLM_MAX_RETRIES=3
LLM_RETRY_DELAY=1.0
LLM_MODEL_CACHE_SIZE=50
```

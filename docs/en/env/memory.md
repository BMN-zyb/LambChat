# Memory Configuration

Cross-session memory system settings. LambChat uses a native MongoDB-backed memory system with optional embedding-powered semantic search.

## Master Switch

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_MEMORY` | `false` | Enable cross-session memory system. |

## Embedding Settings

For semantic search over memories. Leave empty for text-only (no embedding) mode.

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `NATIVE_MEMORY_EMBEDDING_API_BASE` | _(empty)_ | No | OpenAI-compatible embedding API base URL. Empty = text-only mode. |
| `NATIVE_MEMORY_EMBEDDING_API_KEY` | _(empty)_ | Yes | Embedding API key. |
| `NATIVE_MEMORY_EMBEDDING_MODEL` | `text-embedding-3-small` | No | Embedding model name. |

## Search & Index

| Variable | Default | Description |
|----------|---------|-------------|
| `NATIVE_MEMORY_INDEX_ENABLED` | `true` | Enable memory search index. |
| `NATIVE_MEMORY_INDEX_CACHE_TTL` | `300` | Index cache TTL in seconds. |
| `NATIVE_MEMORY_APPEND_MAX_DETAILS` | `8` | Maximum details per memory append. |
| `NATIVE_MEMORY_MAX_TOKENS` | `2000` | Maximum tokens for memory content. |
| `NATIVE_MEMORY_INLINE_CONTENT_MAX_CHARS` | `1200` | Maximum chars for inline memory content. |

## Reranking

Optional reranking for improved memory relevance.

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `NATIVE_MEMORY_RERANK_MODEL` | _(empty)_ | No | Rerank model name. |
| `NATIVE_MEMORY_RERANK_API_BASE` | _(empty)_ | No | Rerank API base URL. |
| `NATIVE_MEMORY_RERANK_API_KEY` | _(empty)_ | Yes | Rerank API key. |

## Storage & Policy

| Variable | Default | Sensitive | Description |
|----------|---------|-----------|-------------|
| `NATIVE_MEMORY_MODEL` | _(empty)_ | No | LLM model for memory extraction. |
| `NATIVE_MEMORY_API_BASE` | _(empty)_ | No | LLM API base for memory extraction. |
| `NATIVE_MEMORY_API_KEY` | _(empty)_ | Yes | LLM API key for memory extraction. |
| `NATIVE_MEMORY_STORE_NAMESPACE` | `memories` | No | LangGraph store namespace. |
| `NATIVE_MEMORY_STALENESS_DAYS` | `30` | No | Days before memory is considered stale. |
| `NATIVE_MEMORY_PRUNE_THRESHOLD` | `90` | No | Prune threshold percentage. |
| `NATIVE_MEMORY_RECALL_MIN_SCORE` | `0.3` | No | Minimum relevance score (0.0-1.0) for recalled memories. |

## Example

```bash
# Enable memory
ENABLE_MEMORY=true

# Embedding for semantic search
NATIVE_MEMORY_EMBEDDING_API_BASE=https://api.openai.com/v1
NATIVE_MEMORY_EMBEDDING_API_KEY=sk-your-key
NATIVE_MEMORY_EMBEDDING_MODEL=text-embedding-3-small

# LLM for memory extraction (optional, defaults to main LLM)
NATIVE_MEMORY_MODEL=gpt-4o-mini
NATIVE_MEMORY_API_BASE=https://api.openai.com/v1
NATIVE_MEMORY_API_KEY=sk-your-key
```

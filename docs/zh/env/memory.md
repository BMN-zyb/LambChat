# 记忆系统配置

跨会话记忆系统设置。LambChat 使用原生 MongoDB 支持的记忆系统，可选启用嵌入驱动的语义搜索。

## 主开关

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ENABLE_MEMORY` | `false` | 启用跨会话记忆系统。 |

## 嵌入设置

用于记忆的语义搜索。留空则使用纯文本模式（无嵌入）。

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `NATIVE_MEMORY_EMBEDDING_API_BASE` | _(空)_ | 否 | OpenAI 兼容的嵌入 API 基础 URL。空 = 纯文本模式。 |
| `NATIVE_MEMORY_EMBEDDING_API_KEY` | _(空)_ | 是 | 嵌入 API 密钥。 |
| `NATIVE_MEMORY_EMBEDDING_MODEL` | `text-embedding-3-small` | 否 | 嵌入模型名称。 |

## 搜索与索引

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `NATIVE_MEMORY_INDEX_ENABLED` | `true` | 启用记忆搜索索引。 |
| `NATIVE_MEMORY_INDEX_CACHE_TTL` | `300` | 索引缓存 TTL（秒）。 |
| `NATIVE_MEMORY_APPEND_MAX_DETAILS` | `8` | 每次记忆追加的最大详情数。 |
| `NATIVE_MEMORY_MAX_TOKENS` | `2000` | 记忆内容的最大 token 数。 |
| `NATIVE_MEMORY_INLINE_CONTENT_MAX_CHARS` | `1200` | 内联记忆内容的最大字符数。 |

## 重排序

可选的重排序以提高记忆相关性。

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `NATIVE_MEMORY_RERANK_MODEL` | _(空)_ | 否 | 重排序模型名称。 |
| `NATIVE_MEMORY_RERANK_API_BASE` | _(空)_ | 否 | 重排序 API 基础 URL。 |
| `NATIVE_MEMORY_RERANK_API_KEY` | _(空)_ | 是 | 重排序 API 密钥。 |

## 存储与策略

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `NATIVE_MEMORY_MODEL` | _(空)_ | 否 | 用于记忆提取的 LLM 模型。 |
| `NATIVE_MEMORY_API_BASE` | _(空)_ | 否 | 用于记忆提取的 LLM API 基础 URL。 |
| `NATIVE_MEMORY_API_KEY` | _(空)_ | 是 | 用于记忆提取的 LLM API 密钥。 |
| `NATIVE_MEMORY_STORE_NAMESPACE` | `memories` | 否 | LangGraph 存储命名空间。 |
| `NATIVE_MEMORY_STALENESS_DAYS` | `30` | 否 | 记忆被视为过期的天数。 |
| `NATIVE_MEMORY_PRUNE_THRESHOLD` | `90` | 否 | 裁剪阈值百分比。 |
| `NATIVE_MEMORY_RECALL_MIN_SCORE` | `0.3` | 否 | 召回记忆的最低相关性分数（0.0-1.0）。 |

## 示例

```bash
# 启用记忆
ENABLE_MEMORY=true

# 嵌入语义搜索
NATIVE_MEMORY_EMBEDDING_API_BASE=https://api.openai.com/v1
NATIVE_MEMORY_EMBEDDING_API_KEY=sk-your-key
NATIVE_MEMORY_EMBEDDING_MODEL=text-embedding-3-small

# 记忆提取 LLM（可选，默认使用主 LLM）
NATIVE_MEMORY_MODEL=gpt-4o-mini
NATIVE_MEMORY_API_BASE=https://api.openai.com/v1
NATIVE_MEMORY_API_KEY=sk-your-key
```

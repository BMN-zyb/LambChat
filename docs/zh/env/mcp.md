# MCP 与工具配置

Model Context Protocol (MCP) 和工具系统设置。

## MCP 设置

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `ENABLE_MCP` | `true` | 否 | 启用 MCP 工具系统。 |
| `MCP_ENCRYPTION_SALT` | _(自动生成)_ | 是 | 用于加密 MCP 密钥的盐值。未设置时自动生成。**建议设置以确保重启后一致性。** |

## 延迟工具加载

对于工具较多的 MCP 服务器，延迟加载通过按需加载工具来减少提示大小。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ENABLE_DEFERRED_TOOL_LOADING` | `true` | 启用延迟/懒加载工具。 |
| `DEFERRED_TOOL_THRESHOLD` | `20` | 触发延迟加载的工具数量阈值。 |
| `DEFERRED_TOOL_SEARCH_LIMIT` | `25` | 搜索返回的最大工具数。 |
| `DEFERRED_TOOL_PROMPT_LIMIT` | `25` | 提示中包含的最大工具数。 |

## 技能

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ENABLE_SKILLS` | `true` | 启用技能系统。 |

## 音频转写

| 变量名 | 默认值 | 敏感 | 说明 |
|--------|--------|------|------|
| `ENABLE_AUDIO_TRANSCRIPTION` | `false` | 否 | 启用音频转写工具。 |
| `AUDIO_TRANSCRIPTION_API_KEY` | _(空)_ | 是 | 转写 API 密钥。 |
| `AUDIO_TRANSCRIPTION_BASE_URL` | _(空)_ | 否 | 转写 API 基础 URL。 |
| `AUDIO_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | 否 | 转写模型名称。 |

## 示例

```bash
# MCP
ENABLE_MCP=true
MCP_ENCRYPTION_SALT=your-random-salt-here

# 技能
ENABLE_SKILLS=true

# 音频转写（可选）
ENABLE_AUDIO_TRANSCRIPTION=true
AUDIO_TRANSCRIPTION_API_KEY=sk-your-key
AUDIO_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
```

::: tip
在生产环境中务必设置 `MCP_ENCRYPTION_SALT` 为一个固定值。如果它发生变化，之前加密的 MCP 凭据将无法读取。
:::

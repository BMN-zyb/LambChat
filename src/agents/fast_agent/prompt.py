"""
Fast Agent 系统提示 - 简洁高效

角色身份通过 SectionPromptMiddleware 独立注入（见 persona.py），
基础提示词只包含能力描述，保证全局 KV 缓存稳定。
"""

# Fast Agent 的基础系统提示词：只描述“能力/文件系统/记忆约定”等稳定内容，
# 刻意不含角色人设（persona 在 nodes.py 由 SectionPromptMiddleware 动态注入）。
# 这样做是为了让 create_deep_agent 拿到的基础 system_prompt 逐字节稳定，
# 从而命中并复用全局 KV 缓存（prompt caching），降低每轮请求的成本与延迟。
FAST_SYSTEM_PROMPT = """## File System
| Path | Purpose |
|------|---------|
| `/workflow/<session-id>` | Current session workflow files |
| `/skills/` | Skill definitions (editable) |

The default persistent file workspace is scoped by the current session id. Use the current session workflow for new files unless the user explicitly asks to work in an existing path.

Cross-session memory: `memory_retain`, `memory_recall`, `memory_delete`.
Treat any memory index in the system prompt as lightweight hints only; recall full details before relying on an item.

**Proactive memory retention:** Store durable user facts, reasoned preferences, constrained project details, and explicit feedback via `memory_retain`. Do NOT store greetings, questions, code, or ephemeral state."""

# 延迟工具（deferred tool）加载功能对应的提示片段占位。
# Fast Agent 置空：其延迟工具的引导语改由 ToolSearchMiddleware 等中间件按需注入，
# 此处保留常量仅为与其它 agent 的接口保持一致。
DEFERRED_TOOL_GUIDE = ""

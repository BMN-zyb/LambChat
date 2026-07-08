"""
Search Agent 系统提示词
- SANDBOX_SYSTEM_PROMPT: 沙箱模式，独立远程存储
- DEFAULT_SYSTEM_PROMPT: 非沙箱模式，统一路径管理

角色身份通过 SectionPromptMiddleware 独立注入（见 persona.py），
基础提示词只包含能力描述，保证全局 KV 缓存稳定。
"""

# 沙箱模式的系统提示词：向模型讲清"沙箱本地 work_dir"与"远程虚拟 /skills/ 存储"
# 两套路径的访问方式，并强调 /skills/ 只能用文件工具、禁止 shell 直接访问。
# 作为 create_deep_agent 的 system_prompt 传入（见 nodes.py 的 _create_backend_and_prompt）。
SANDBOX_SYSTEM_PROMPT = """## Storage Architecture (CRITICAL)

| System | Paths | Access |
|--------|-------|--------|
| Sandbox Local | current session workspace (`work_dir`) | shell commands and file tools |
| Remote Storage | `/skills/` | read/write/edit_file tools |

`/skills/` is virtual remote storage, not a sandbox filesystem path. Use file tools for `/skills/`; never shell-access it (`python /skills/x.py`, `cat /skills/x.md`, `cp /skills/* .`). The sandbox local path is provided at runtime as `Current session workspace`; use that session-id-specific workspace for shell commands, file tools, and absolute upload paths. To run skill code, transfer it into the current session workspace with `transfer_file`/`transfer_path`, then execute the copied file.

## URL File Upload
Use `upload_url_to_sandbox(url, file_path)` to download URLs to sandbox. `file_path` must be absolute inside the current session workspace.
"""

# 沙箱运行时片段：含 {work_dir} 占位符，运行时用 .format 填入本会话的绝对工作目录。
# 因为它是"随会话 / 用户变化"的动态内容，被安排在静态提示块之后由
# SectionPromptMiddleware 注入，以尽量保护系统提示前缀的 KV 缓存稳定性（见 nodes.py 中间件顺序）。
SANDBOX_RUNTIME_SECTION = """## Sandbox Runtime

Current session workspace: `{work_dir}`

This is the initial/default working directory for this session and is derived from the session id. Use this absolute directory for shell-created files, file tools, and absolute `upload_url_to_sandbox` paths. Keep this runtime value out of durable docs unless the user specifically asks for internal paths.
"""

# 非沙箱模式的系统提示词：说明 /workflow/<session-id> 会话工作区与虚拟 /skills/ 库的用途，
# 同样强调 /skills/ 是 DB 支撑的虚拟存储，只能用文件工具访问。
DEFAULT_SYSTEM_PROMPT = """## File System
| Path | Purpose |
|------|---------|
| `/workflow/<session-id>` | Current session workflow files |
| `/skills/` | Skill library (editable, virtual — DB-backed) |

The default persistent file workspace is scoped by the current session id. Use the current session workflow for new files unless the user explicitly asks to work in an existing path.

`/skills/` is virtual storage, not a real filesystem directory. Use `ls`, `read_file`, `write_file`, and `edit_file` for skills; never shell-access `/skills/` (`ls -la /skills/`, `cat /skills/x.md`, `python /skills/x.py`). To execute a skill script, first copy it into the current session workflow or the sandbox session workspace via `transfer_file`/`transfer_path`.
"""

# 延迟工具加载相关的提示占位，当前留空，保留为后续扩展位
DEFERRED_TOOL_GUIDE = ""

"""
Search Agent 系统提示词
- SANDBOX_SYSTEM_PROMPT: 沙箱模式，独立远程存储
- DEFAULT_SYSTEM_PROMPT: 非沙箱模式，统一路径管理

角色身份通过 SectionPromptMiddleware 独立注入（见 persona.py），
基础提示词只包含能力描述，保证全局 KV 缓存稳定。
"""

from src.agents.core.subagent_prompts import SUBAGENT_TASK_GUIDE, WORKFLOW_SECTION

SANDBOX_SYSTEM_PROMPT = """## Storage Architecture (CRITICAL)

| System | Paths | Access |
|--------|-------|--------|
| Sandbox Local | `{work_dir}/` | shell commands |
| Remote Storage | `/skills/` | read/write/edit_file tools |

`/skills/` is virtual remote storage, not a sandbox filesystem path. Use file tools for `/skills/`; never shell-access it (`python /skills/x.py`, `cat /skills/x.md`, `cp /skills/* .`). To run skill code, transfer it into `{work_dir}` with `transfer_file`/`transfer_path`, then execute the copied file.

## URL File Upload
Use `upload_url_to_sandbox(url, file_path)` to download URLs to sandbox. `file_path` must be absolute (e.g., `{work_dir}/data.csv`).
"""

SANDBOX_SYSTEM_PROMPT = SANDBOX_SYSTEM_PROMPT + WORKFLOW_SECTION + SUBAGENT_TASK_GUIDE

DEFAULT_SYSTEM_PROMPT = """## File System
| Path | Purpose |
|------|---------|
| `/workspace` | Persistent files |
| `/skills/` | Skill library (editable, virtual — DB-backed) |

`/skills/` is virtual storage, not a real filesystem directory. Use `ls`, `read_file`, `write_file`, and `edit_file` for skills; never shell-access `/skills/` (`ls -la /skills/`, `cat /skills/x.md`, `python /skills/x.py`). To execute a skill script, first copy it into `/workspace` or the sandbox work directory via `transfer_file`/`transfer_path`.
"""

DEFAULT_SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT + WORKFLOW_SECTION + SUBAGENT_TASK_GUIDE

DEFERRED_TOOL_GUIDE = ""

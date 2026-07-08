"""Team Agent prompts."""

# re 用于把角色名 / 成员 ID 规整（slugify）成稳定的 task 工具子代理类型标识（见文件末尾函数）。
import re

# TOOL_PROGRESS_GUIDE 是各 agent 共享的"工具进度汇报"提示片段，会拼接进 router 提示词末尾。
from src.agents.core.subagent_prompts import TOOL_PROGRESS_GUIDE

# 团队模式下"路由/汇总"主代理的系统提示词模板。
# 职责链：理解需求 -> 拆解子任务 -> 用 `task` 工具分派给最合适的团队成员角色 -> 汇总各成员回执成统一答案。
# 其中 {team_members_description}/{team_instructions_section}/{default_role}/{tool_progress_guide}
# 等占位符由下方 build_team_router_system_prompt(...) 在运行时按具体团队填充。
TEAM_ROUTER_SYSTEM_PROMPT = """\
You are a team router agent. Your job is to:

1. Understand the user's request.
2. Decompose it into sub-tasks.
3. Dispatch each sub-task to the most appropriate team member role using the `task` tool.
4. Synthesize all handoff notes into a coherent final answer.

## Team Composition
You have the following team members available:

{team_members_description}

{team_instructions_section}

## Default Role
When a task does not clearly map to a specific role, dispatch it to the default role: {default_role}.

## Routing Rules
- Read each sub-task carefully and match it to the role whose persona best fits.
- The `task` tool is for work assignments only: send the actual user-requested work for a role to complete.
- Do not dispatch onboarding, coordination, reminder, or notification messages to team members. Subagents already return their work to you automatically.
- You may dispatch to multiple roles in parallel when sub-tasks are independent.
- Always forward the user's timestamp to every subagent.
- Synthesize handoff notes: deduplicate findings, resolve conflicts with direct evidence, and present a unified answer.
- If a subagent fails, report what succeeded and flag the failure clearly.
- Never claim work is done until all subagent results are collected and verified.

## Collaboration Contract
- For complex requests, form a short routing plan before dispatch: what can run in parallel, what is dependent work, and what evidence each role must return.
- Give each subagent a complete work order with scope boundaries, relevant context, expected evidence, and acceptance criteria.
- Dispatch independent work in parallel. For dependent work, wait for the prerequisite result, synthesize it, and then dispatch the next role with the updated context.
- Treat role outputs as evidence for natural synthesis, not a transcript. The final answer should read like one capable teammate completed the task with help from specialists.
- Do not expose internal coordination unless it helps the user understand a blocker, risk, or verification result.

## Output
Your final answer should be a clean synthesis of all role-specific findings, not a list of subagent outputs.

{tool_progress_guide}
"""

# 沙箱启用时追加到系统提示词的"存储架构"说明：区分沙箱本地工作区与 /skills/ 虚拟远端存储，
# 明确各自的访问方式，避免模型误把 /skills/ 当成 shell 可直接访问的路径。
SANDBOX_SYSTEM_PROMPT = """## Storage Architecture (CRITICAL)

| System | Paths | Access |
|--------|-------|--------|
| Sandbox Local | current session workspace (`work_dir`) | shell commands and file tools |
| Remote Storage | `/skills/` | read/write/edit_file tools |

`/skills/` is virtual remote storage, not a sandbox filesystem path. Use file tools for `/skills/`; never shell-access it (`python /skills/x.py`, `cat /skills/x.md`, `cp /skills/* .`). The sandbox local path is provided at runtime as `Current session workspace`; use that session-id-specific workspace for shell commands, file tools, and absolute upload paths. To run skill code, transfer it into the current session workspace with `transfer_file`/`transfer_path`, then execute the copied file.

## URL File Upload
Use `upload_url_to_sandbox(url, file_path)` to download URLs to sandbox. `file_path` must be absolute inside the current session workspace.
"""

# 运行时才知道的沙箱信息片段：把本会话专属的工作目录（work_dir，由 session id 派生）注入提示词，
# 供模型作为 shell / 文件工具 / 上传的绝对基准目录使用。{work_dir} 由调用方 format 填入。
SANDBOX_RUNTIME_SECTION = """## Sandbox Runtime

Current session workspace: `{work_dir}`

This is the initial/default working directory for this session and is derived from the session id. Use this absolute directory for shell-created files, file tools, and absolute `upload_url_to_sandbox` paths. Keep this runtime value out of durable docs unless the user specifically asks for internal paths.
"""


# 把团队成员列表渲染成文本，填入 router 提示词的 {team_members_description} 占位符。
# 每个成员一行 `- \`subagent_type\`: **role_name** (member_id: ...)`，可选附带能力摘要与角色指令；
# router 正是靠这段清单知道有哪些角色可用、各自擅长什么，从而决定把子任务分派给谁。
def build_team_members_description(team, role_summaries: dict[str, str] | None = None) -> str:
    """Build a text description of team members for the router prompt."""
    role_summaries = role_summaries or {}
    lines = []
    # 只遍历激活成员：每个成员生成 task 工具用的稳定子代理类型 + 展示名。
    for m in team.active_members:
        subagent_type = build_team_member_subagent_type(m)
        role_name = m.role_name or m.member_id
        lines.append(f"- `{subagent_type}`: **{role_name}** (member_id: {m.member_id})")
        role_summary = role_summaries.get(m.member_id)
        if role_summary:
            lines.append(f"  Capability summary: {role_summary}")
        if m.role_instructions:
            lines.append(f"  Instructions: {m.role_instructions}")
    return "\n".join(lines)


# 把某个角色完整的系统提示词压缩成一句能力摘要，供 router 提示词展示。
# 之所以要截断（默认 500 字符），是为了避免把每个角色的长 persona 全量塞进 router 提示，
# 既省 token 又让路由决策聚焦在"角色能干什么"而非细节。
def summarize_role_system_prompt(system_prompt: str, max_chars: int = 500) -> str:
    """Build a compact role capability summary for the router prompt."""
    text = " ".join(line.strip() for line in (system_prompt or "").splitlines() if line.strip())
    if len(text) <= max_chars:
        return text
    # 超长则截断并留出 3 个字符给省略号，保证结果不超过 max_chars。
    return text[: max_chars - 3].rstrip() + "..."


# 针对一个具体团队，用 TEAM_ROUTER_SYSTEM_PROMPT 模板填充出完整的 router 系统提示词。
# 关键参数 default_role：当子任务无法明确匹配某角色时的兜底分派目标（由调用方算好后传入）。
def build_team_router_system_prompt(
    team,
    *,
    default_role: str,
    role_summaries: dict[str, str] | None = None,
) -> str:
    """Build the router system prompt for a concrete team."""
    team_instructions = (getattr(team, "team_instructions", "") or "").strip()
    # 团队级指令为空时整段省略（不注入空标题），非空时才拼出 "## Team Instructions" 小节。
    team_instructions_section = (
        f"## Team Instructions\n{team_instructions}" if team_instructions else ""
    )
    return TEAM_ROUTER_SYSTEM_PROMPT.format(
        team_members_description=build_team_members_description(
            team,
            role_summaries=role_summaries,
        ),
        team_instructions_section=team_instructions_section,
        default_role=default_role,
        tool_progress_guide=TOOL_PROGRESS_GUIDE.strip(),
    )


# 内部子代理类型 -> 用户可见角色名 的映射：供前端把子代理活动展示成"某角色"而非内部标识。
def build_team_subagent_display_names(team) -> dict[str, str]:
    """Map internal team subagent types to user-facing role names."""
    return {
        build_team_member_subagent_type(member): (member.role_name or member.member_id)
        for member in team.active_members
    }


# 内部子代理类型 -> 角色头像 URL 的映射；仅收录设置了头像的成员，供前端展示子代理身份。
def build_team_subagent_avatars(team) -> dict[str, str]:
    """Map internal team subagent types to user-facing role avatar URLs."""
    return {
        build_team_member_subagent_type(member): member.role_avatar
        for member in team.active_members
        if member.role_avatar
    }


# 为团队成员生成稳定、URL/工具友好的 task 子代理类型标识，格式 `team-<member>-<role>`。
# 之所以要"稳定"：这个标识既写进 router 提示词供分派，又用作 deep agent 子代理的 name，
# 两处必须一致，否则 router 说要分派的角色在内层找不到对应子代理。
def build_team_member_subagent_type(member) -> str:
    """Build a stable task-tool subagent type for a team member."""
    # 角色名转小写后，把非字母数字统一替换为连字符并去掉首尾连字符。
    role_slug = re.sub(r"[^a-z0-9]+", "-", (member.role_name or "").lower()).strip("-")
    # 角色名为空（或全是特殊字符）时兜底为 "role"，保证标识片段非空。
    if not role_slug:
        role_slug = "role"
    # member_id 同样 slugify（允许保留已有连字符），确保标识里 member 段可读且合法。
    member_slug = re.sub(r"[^a-z0-9-]+", "-", member.member_id.lower()).strip("-")
    # member_id 规整后为空时兜底为 "member"。
    if not member_slug:
        member_slug = "member"
    return f"team-{member_slug}-{role_slug}"

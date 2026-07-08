"""Persona prompt helpers.

角色身份通过 middleware 注入，与基础提示词解耦。
通过 register_harness_profile 移除 BASE_AGENT_PROMPT 中的身份声明，
让 persona 系统完全控制角色身份，不再冲突。

最终 system message 结构：
  [Block 0] SANDBOX/DEFAULT/FAST_SYSTEM_PROMPT + BEHAVIOR_GUIDE      ← 全局稳定
  [Block 1-3] deepagents 内部 (write_todos, conventions, task)        ← 全局稳定
  [Block 4]   ## Persona (角色 + 行为合并)                             ← 同 persona 缓存命中
  [Block 5]   Skills                                                  ← 同 session 缓存命中
  [Block 6]   Memory guide                                            ← 同用户缓存命中
  [Block 7+]  Memory index / Tool search                              ← 每 turn 变化
"""

import importlib
from typing import Any

from src.kernel.config.base import settings

# 以"可选依赖"的方式加载 deepagents 包（真正驱动 ReAct 循环的内层 graph 引擎）。
# 用 importlib 动态导入并吞掉 ImportError，是为了兼容缺少该依赖或版本较旧的环境：
# 缺失时下面的 _HarnessProfile / _register_harness_profile 会退化为 None，本模块仍可正常导入。
_deepagents: Any = None
try:
    _deepagents = importlib.import_module("deepagents")
except ImportError:  # pragma: no cover - compatibility with older deepagents builds
    pass

# 从 deepagents 中取出 HarnessProfile 类型与 register_harness_profile 注册函数；
# 若包不可用或旧版本未提供这些符号，则取 None，后续的注册步骤会被相应跳过。
_HarnessProfile = getattr(_deepagents, "HarnessProfile", None) if _deepagents is not None else None
_register_harness_profile = (
    getattr(_deepagents, "register_harness_profile", None) if _deepagents is not None else None
)


# 默认角色身份：当调用方未提供自定义 persona（system_prompt 为空）时，
# persona 块回退使用这句通用身份声明，保证最终 system prompt 里始终存在明确的角色设定。
DEFAULT_ROLE = "You are an intelligent assistant with tools and skills."

# persona 块在最终 system message 中的 Markdown 小标题（对应模块 docstring 里的 [Block 4]）。
_PERSONA_HEADING = "## Persona"


# ---------------------------------------------------------------------------
# Strip the identity line from BASE_AGENT_PROMPT so persona has full control.
#
# Original first line: "You are a deep agent, an AI assistant that helps
# users accomplish tasks using tools. You respond with text and tool calls.
# The user can see your responses and tool outputs in real time."
#
# We keep everything else (Core Behavior, Professional Objectivity, Doing
# Tasks, etc.) because those are valuable behavioral guardrails that don't
# conflict with persona roles.
#
# Using a bare provider key ("anthropic") covers all Anthropic models.
# ---------------------------------------------------------------------------
def _build_behavior_guide() -> str:
    """Build the base behavior guide, conditionally including scheduled task instructions."""
    scheduled_task_section = ""
    # 仅当配置启用了定时任务能力时，才把"可主动定时给用户发消息"的说明拼进行为指南，
    # 避免在未开启该功能的部署里向模型宣称并不存在的能力。
    if settings.ENABLE_SCHEDULED_TASK:
        scheduled_task_section = """

You can also **proactively send messages to the user on a schedule** using the `scheduled_task_create` tool. When the user asks you to remind them, notify them, send periodic reports, or check something regularly — create a scheduled task. The system will automatically execute it and deliver the results to the user's conversation. Do NOT claim you cannot proactively message the user; you CAN via scheduled tasks."""

    return f"""You have access to tools and can respond with text and tool calls. The user can see your responses and tool outputs in real time.{scheduled_task_section}

## Core Behavior

- Be concise and direct. Don't over-explain unless asked.
- NEVER add unnecessary preamble ("Sure!", "Great question!", "I'll now...").
- Don't say "I'll now do X" — just do it.
- If the request is underspecified, ask only the minimum followup needed to take the next useful action.
- If asked how to approach something, explain first, then act.

## Professional Objectivity

- Prioritize accuracy over validating the user's beliefs
- Disagree respectfully when the user is incorrect
- Avoid unnecessary superlatives, praise, or emotional validation

## Doing Tasks

When the user asks you to do something:

1. **Understand first** — read relevant files, check existing patterns. Quick but thorough — gather enough evidence to start, then iterate. When reading files, always read enough to see the full picture — do not assume the default 100-line limit covers the whole file. Use offset/limit to continue reading, or increase the limit for larger files.
2. **Act** — implement the solution. Work quickly but accurately.
3. **Verify** — check your work against what was asked, not against your own output. Your first attempt is rarely correct — iterate.

Keep working until the task is fully complete. Don't stop partway and explain what you would do — just do it. Only yield back to the user when the task is done or you're genuinely blocked.

**When things go wrong:**
- If something fails repeatedly, stop and analyze *why* — don't keep retrying the same approach.
- If you're blocked, tell the user what's wrong and ask for guidance.

## Clarifying Requests

- Do not ask for details the user already supplied.
- Use reasonable defaults when the request clearly implies them.
- Prioritize missing semantics like content, delivery, detail level, or alert criteria.
- Avoid opening with a long explanation of tool, scheduling, or integration limitations when a concise blocking followup question would move the task forward.
- Ask domain-defining questions before implementation questions.
- For monitoring or alerting requests, ask what signals, thresholds, or conditions should trigger an alert.

## Progress Updates

For longer tasks, provide brief progress updates at reasonable intervals — a concise sentence recapping what you've done and what's next."""


# 在模块导入时构建一次行为指南字符串；其内容依赖 settings，构建完成后即固定不再变化。
_BEHAVIOR_GUIDE = _build_behavior_guide()

# 若 deepagents 可用，则以 "anthropic" 作为 provider key 注册一个 HarnessProfile，
# 把上面的行为指南作为该 provider 下所有 Anthropic 模型的基础 system prompt 注入内层 graph。
# 如此：角色身份交由 persona 系统掌控、通用行为准则由此处统一下发，二者解耦、互不冲突。
if _HarnessProfile is not None and _register_harness_profile is not None:
    # Register on import — this is idempotent (additive merge).
    _register_harness_profile("anthropic", _HarnessProfile(base_system_prompt=_BEHAVIOR_GUIDE))


def split_persona_prompt(system_prompt: str) -> tuple[str, str]:
    """Split a persona system_prompt into role identity and behavior body.

    The first paragraph (before the first blank line) is the *role identity*.
    Everything after the first blank line is *behavior instructions*.

    Returns (role, behavior).  Either may be empty.
    """
    text = system_prompt.strip()
    if not text:
        return "", ""

    parts = text.split("\n\n", 1)
    role = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else ""
    return role, body


def build_persona_prompt_sections(system_prompt: str | None) -> list[str]:
    """Build persona sections as content blocks for injection.

    Role and behavior are merged into a **single block** for stronger signal.
    Splitting into two blocks dilutes the persona identity — the model may
    latch onto the default role before reaching a separate behavior block.

    Always returns exactly one block (with default role when no persona).
    """
    role, body = split_persona_prompt(system_prompt or "")
    effective_role = role if role else DEFAULT_ROLE

    if body:
        return [f"{_PERSONA_HEADING}\n\n{effective_role}\n\n{body}"]
    return [f"{_PERSONA_HEADING}\n\n{effective_role}"]


def build_persona_prompt_section(system_prompt: str | None) -> str:
    """Legacy single-section builder. Prefer ``build_persona_prompt_sections``."""
    return build_persona_prompt_sections(system_prompt)[0]

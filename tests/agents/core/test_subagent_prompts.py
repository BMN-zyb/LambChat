from src.agents.core.subagent_prompts import SUBAGENT_PROMPT, SUBAGENT_TASK_GUIDE, WORKFLOW_SECTION
from src.agents.fast_agent.prompt import FAST_SYSTEM_PROMPT
from src.agents.search_agent.prompt import DEFAULT_SYSTEM_PROMPT, SANDBOX_SYSTEM_PROMPT


def test_subagent_prompt_requires_structured_handoff_notes() -> None:
    required_sections = [
        "## Handoff Notes",
        "Goal:",
        "What I checked:",
        "Key findings:",
        "Files / tools touched:",
        "Decisions or assumptions:",
        "Risks / blockers:",
        "Suggested next step:",
        "Memory-worthy notes:",
    ]

    for section in required_sections:
        assert section in SUBAGENT_PROMPT


def test_main_agent_guide_requires_synthesizing_subagent_results() -> None:
    required_guidance = [
        "synthesize",
        "deduplicate",
        "conflict",
        "handoff notes",
    ]

    guide = SUBAGENT_TASK_GUIDE.lower()
    for phrase in required_guidance:
        assert phrase in guide


def test_workflow_section_mentions_searching_deferred_tools() -> None:
    required_guidance = [
        "search_tools",
        "deferred",
        "load the matching schema",
        "already loaded",
    ]

    workflow = WORKFLOW_SECTION.lower()
    for phrase in required_guidance:
        assert phrase in workflow


def test_workflow_section_describes_skills_workspace_routing() -> None:
    required_guidance = [
        "/skills/*",
        "skill store",
        "transfer_file",
        "transfer_path",
        "never execute `/skills/...` directly",
    ]

    workflow = WORKFLOW_SECTION.lower()
    for phrase in required_guidance:
        assert phrase in workflow


def test_workflow_section_requires_path_checks_and_separate_workspaces() -> None:
    required_guidance = [
        "before creating files/directories",
        "check whether the target path exists",
        "do not develop inside it",
        "active writable workspace/work_dir",
        "unrelated to the current project",
        "only touch an existing project",
    ]

    workflow = WORKFLOW_SECTION.lower()
    for phrase in required_guidance:
        assert phrase in workflow


def test_subagent_prompt_requires_path_checks_and_separate_workspaces() -> None:
    required_guidance = [
        "before creating files/directories",
        "check whether the target path exists",
        "do not develop inside it",
        "active writable workspace/work_dir",
        "unrelated to the current project",
        "only touch an existing project",
    ]

    prompt = SUBAGENT_PROMPT.lower()
    for phrase in required_guidance:
        assert phrase in prompt


def test_fast_system_prompt_does_not_repeat_file_transfer_rules() -> None:
    assert FAST_SYSTEM_PROMPT.count("File Transfer") == 1


def test_workflow_section_keeps_core_operational_guidance() -> None:
    required_guidance = [
        "reveal_file",
        "write_file",
        "returned `url`",
        "reveal_project",
        "transfer_file",
        "transfer_path",
        "search_tools",
        "mcporter list",
        "ask_human",
    ]

    for phrase in required_guidance:
        assert phrase in WORKFLOW_SECTION


def test_fast_system_prompt_keeps_memory_guidance() -> None:
    required_guidance = [
        "memory_retain",
        "memory_recall",
        "memory_delete",
        "recall full details",
        "Do NOT store greetings",
    ]

    for phrase in required_guidance:
        assert phrase in FAST_SYSTEM_PROMPT


def test_search_prompts_keep_virtual_skills_and_transfer_guidance() -> None:
    for prompt in (DEFAULT_SYSTEM_PROMPT, SANDBOX_SYSTEM_PROMPT):
        for phrase in [
            "`/skills/` is virtual",
            "never shell-access",
            "transfer_file",
            "transfer_path",
        ]:
            assert phrase in prompt

    assert "upload_url_to_sandbox" in SANDBOX_SYSTEM_PROMPT

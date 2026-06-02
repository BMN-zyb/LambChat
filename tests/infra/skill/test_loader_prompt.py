import pytest

from src.infra.skill.loader import build_skills_prompt, load_skill_files


@pytest.mark.asyncio
async def test_build_skills_prompt_requires_transfer_before_execution() -> None:
    prompt = await build_skills_prompt(
        [{"name": "demo-skill", "description": "Run a demo script."}]
    )

    assert "transfer them out of `/skills/` into the sandbox workspace" in prompt
    assert "Use `transfer_file` or `transfer_path` to move skill files into the workspace" in prompt


@pytest.mark.asyncio
async def test_load_skill_files_uses_async_binary_ref_parser(monkeypatch) -> None:
    calls: list[str] = []

    class _SkillManager:
        def __init__(self, user_id):
            self.user_id = user_id

        async def get_effective_skills(self):
            return {
                "demo": {
                    "enabled": True,
                    "description": "Demo skill",
                    "files": {"SKILL.md": "hello"},
                }
            }

    async def fake_parse_binary_ref_async(content: str):
        calls.append(content)
        return None

    monkeypatch.setattr("src.infra.skill.loader.settings.ENABLE_SKILLS", True)
    monkeypatch.setattr("src.infra.skill.manager.SkillManager", _SkillManager)
    monkeypatch.setattr(
        "src.infra.skill.loader.parse_binary_ref_async",
        fake_parse_binary_ref_async,
        raising=False,
    )

    result = await load_skill_files("user-1")

    assert calls == ["hello"]
    assert "/demo/SKILL.md" in result["files"]

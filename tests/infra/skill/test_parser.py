from __future__ import annotations

from src.infra.skill.parser import parse_skill_md


class _NoFullSplitString(str):
    def splitlines(self, *args, **kwargs):
        raise AssertionError("parser should not split the full SKILL.md content")


def test_parse_skill_md_fallback_does_not_split_full_content() -> None:
    content = _NoFullSplitString(
        "name: planner\ndescription: Plan work\n" + ("large body\n" * 1000)
    )

    assert parse_skill_md(content) == ("planner", "Plan work", [])

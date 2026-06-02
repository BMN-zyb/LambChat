from __future__ import annotations

from src.infra.tool.deferred_manager import DeferredToolManager
from src.infra.tool.tool_search_tool import ToolSearchTool


class _FakeTool:
    def __init__(self, name: str, description: str, server: str = "server") -> None:
        self.name = name
        self.description = description
        self.server = server


class _HugeArgsSchema:
    @classmethod
    def model_json_schema(cls):
        return {
            "type": "object",
            "properties": {
                "choice": {
                    "type": "string",
                    "description": "Pick one generated option.",
                    "enum": [f"option-{idx:05d}" for idx in range(5000)],
                }
            },
            "required": ["choice"],
        }


async def test_search_tools_caps_oversized_schema_output() -> None:
    tool = _FakeTool("server:huge_schema", "huge schema test tool")
    tool.args_schema = _HugeArgsSchema
    manager = DeferredToolManager(all_deferred_tools=[tool], session_id="session-1")
    search_tool = ToolSearchTool(manager=manager, search_limit=5)

    result = await search_tool._arun("select:server:huge_schema")

    assert len(result) < 20_000
    assert "schema truncated" in result
    assert "option-00000" in result
    assert "option-04999" not in result


async def test_search_tools_offloads_search_and_schema_formatting(
    monkeypatch,
) -> None:
    from src.infra.tool import tool_search_tool

    calls: list[str] = []
    tool = _FakeTool("server:huge_schema", "huge schema test tool")
    tool.args_schema = _HugeArgsSchema
    manager = DeferredToolManager(all_deferred_tools=[tool], session_id="session-1")
    search_tool = ToolSearchTool(manager=manager, search_limit=5)

    async def fake_run_blocking_io(func, *args, **kwargs):
        calls.append(getattr(func, "__name__", "unknown"))
        return func(*args, **kwargs)

    monkeypatch.setattr(tool_search_tool, "run_blocking_io", fake_run_blocking_io, raising=False)

    result = await search_tool._arun("select:server:huge_schema")

    assert "Found 1 tool(s)" in result
    assert calls == ["_search_and_format_tool_results"]

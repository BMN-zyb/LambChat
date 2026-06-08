from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.agents.fast_agent import graph as fast_graph
from src.agents.search_agent import graph as search_graph
from src.agents.team_agent import graph as team_graph


class _DummyContext:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def setup(self) -> None:
        pass

    async def close(self) -> None:
        pass


class _DummyPresenter:
    run_id = "run-goal"
    trace_id = "trace-goal"

    def metadata(self) -> dict[str, Any]:
        return {"event": "metadata", "data": {"run_id": self.run_id}}

    async def build_langsmith_metadata(self) -> dict[str, Any]:
        return {}

    def error(self, message: str, error_type: str) -> dict[str, Any]:
        return {"event": "error", "data": {"message": message, "type": error_type}}

    def done(self) -> dict[str, Any]:
        return {"event": "done", "data": {"status": "completed"}}


class _CapturingGraph:
    def __init__(self) -> None:
        self.config: dict[str, Any] | None = None

    async def ainvoke(self, _state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        self.config = config
        return {}


class _FutureGraph:
    def __init__(self) -> None:
        self.config: dict[str, Any] | None = None

    def ainvoke(self, _state: dict[str, Any], config: dict[str, Any]) -> asyncio.Future[dict]:
        self.config = config
        future = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(future.set_result, {})
        return future


async def _drain_stream(agent: Any, graph: _CapturingGraph) -> None:
    agent._initialized = True
    agent._graph = graph

    async for _event in agent._stream(
        "continue",
        "session-goal",
        user_id="user-goal",
        presenter=_DummyPresenter(),
        active_goal={"objective": "ship it", "rubric": "- done"},
    ):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "agent_cls", "context_name"),
    [
        (fast_graph, fast_graph.FastAgent, "FastAgentContext"),
        (search_graph, search_graph.SearchAgent, "SearchAgentContext"),
        (team_graph, team_graph.TeamAgent, "TeamAgentContext"),
    ],
)
async def test_agent_stream_passes_active_goal_to_node_config(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    agent_cls: Any,
    context_name: str,
) -> None:
    monkeypatch.setattr(module, context_name, _DummyContext)
    graph = _CapturingGraph()

    await _drain_stream(agent_cls(), graph)

    assert graph.config is not None
    assert graph.config["configurable"]["active_goal"] == {
        "objective": "ship it",
        "rubric": "- done",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "agent_cls", "context_name"),
    [
        (fast_graph, fast_graph.FastAgent, "FastAgentContext"),
        (search_graph, search_graph.SearchAgent, "SearchAgentContext"),
        (team_graph, team_graph.TeamAgent, "TeamAgentContext"),
    ],
)
async def test_agent_stream_accepts_future_returned_by_graph_ainvoke(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    agent_cls: Any,
    context_name: str,
) -> None:
    monkeypatch.setattr(module, context_name, _DummyContext)
    graph = _FutureGraph()

    await _drain_stream(agent_cls(), graph)

    assert graph.config is not None

from __future__ import annotations

from typing import Any, ClassVar, Sequence, TypedDict

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.agents.core.node_utils import (
    build_nested_graph_configurable,
    isolated_nested_graph_run,
)


class _OuterState(TypedDict):
    input: str
    output: str


class _RecordingChatModel(BaseChatModel):
    calls: ClassVar[list[list[tuple[str, Any]]]] = []

    @property
    def _llm_type(self) -> str:
        return "recording-chat"

    def bind_tools(
        self,
        tools: Sequence[BaseTool | dict | Any] | None = None,
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> "_RecordingChatModel":
        del tools, tool_choice, kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        type(self).calls.append([(message.type, message.content) for message in messages])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_build_nested_graph_configurable_keeps_trace_id_for_tools() -> None:
    config = build_nested_graph_configurable(
        thread_id="session-1",
        checkpointer=object(),
        trace_id="trace-1",
    )

    assert config["trace_id"] == "trace-1"


@pytest.mark.asyncio
async def test_manually_nested_deep_agent_keeps_history_across_outer_graph_turns() -> None:
    _RecordingChatModel.calls = []
    checkpointer = MemorySaver()

    async def agent_node(state: _OuterState, config: RunnableConfig) -> dict[str, str]:
        del config
        inner_graph = create_deep_agent(
            model=_RecordingChatModel(),
            tools=[],
            checkpointer=checkpointer,
        )
        inner_config = {
            "configurable": build_nested_graph_configurable(
                thread_id="same-session",
                checkpointer=checkpointer,
            ),
            "recursion_limit": 50,
        }

        async with isolated_nested_graph_run():
            async for _ in inner_graph.astream_events(
                {"messages": [{"role": "user", "content": state["input"]}]},
                inner_config,
                version="v2",
            ):
                pass

        return {"output": "done"}

    builder = StateGraph(_OuterState)
    builder.add_node("agent", agent_node)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    outer_graph = builder.compile(checkpointer=None)

    await outer_graph.ainvoke(
        {"input": "first", "output": ""},
        {"configurable": {"thread_id": "outer-session"}},
    )
    await outer_graph.ainvoke(
        {"input": "second", "output": ""},
        {"configurable": {"thread_id": "outer-session"}},
    )

    assert len(_RecordingChatModel.calls) == 2
    second_call_user_messages = [
        content for message_type, content in _RecordingChatModel.calls[1] if message_type == "human"
    ]
    assert second_call_user_messages == ["first", "second"]

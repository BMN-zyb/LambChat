from __future__ import annotations

import asyncio
import json

from src.agents.core.recommendations import (
    MAX_RECOMMEND_PROMPT_CHARS,
    MAX_RECOMMEND_PROMPT_TOKENS,
    build_recommend_prompt,
    count_recommend_prompt_tokens,
    drain_recommend_background_tasks,
    format_history_context,
    format_history_from_messages,
    generate_recommend_questions,
    schedule_recommend_questions,
)
from src.agents.fast_agent.graph import FastAgent
from src.agents.search_agent.graph import SearchAgent
from src.agents.team_agent.graph import TeamAgent


class _FakePresenter:
    def __init__(self) -> None:
        self.questions = None

    async def emit_recommend_questions(self, questions):
        self.questions = questions
        return {"event": "recommend:questions", "data": {"questions": questions}}


class _RecordingBuilder:
    def __init__(self) -> None:
        self.nodes = []
        self.edges = []
        self.entry_point = None

    def add_node(self, name, func, description=""):
        self.nodes.append((name, func))
        return self

    def set_entry_point(self, node_name):
        self.entry_point = node_name
        return self

    def add_edge(self, from_node, to_node):
        self.edges.append((from_node, to_node))
        return self


class _FakeResponse:
    content = '["问题一？", "问题二？", "问题三？"]'


class _FakeModel:
    def __init__(self) -> None:
        self.prompts = []

    async def ainvoke(self, prompt: str):
        self.prompts.append(prompt)
        return _FakeResponse()


async def test_generate_recommend_questions_uses_session_title_model(monkeypatch) -> None:
    calls = []
    model = _FakeModel()

    async def fake_get_model(**kwargs):
        calls.append(kwargs)
        return model

    monkeypatch.setattr("src.infra.llm.client.LLMClient.get_model", fake_get_model)
    monkeypatch.setattr(
        "src.agents.core.recommendations.settings.SESSION_TITLE_MODEL",
        "title-model",
    )
    monkeypatch.setattr(
        "src.agents.core.recommendations.settings.SESSION_TITLE_API_BASE",
        "https://title.example/v1",
    )
    monkeypatch.setattr(
        "src.agents.core.recommendations.settings.SESSION_TITLE_API_KEY",
        "title-key",
    )

    questions = await generate_recommend_questions("如何准备半程马拉松？", "先建立基础跑量。")

    assert calls == [
        {
            "model": "title-model",
            "api_base": "https://title.example/v1",
            "api_key": "title-key",
            "max_tokens": 300,
            "max_retries": 3,
        }
    ]
    assert "如何准备半程马拉松？" in model.prompts[0]
    assert "先建立基础跑量。" in model.prompts[0]
    assert questions == ["问题一？", "问题二？", "问题三？"]


async def test_generate_recommend_questions_includes_history_context(monkeypatch) -> None:
    model = _FakeModel()

    async def fake_get_model(**kwargs):
        return model

    monkeypatch.setattr("src.infra.llm.client.LLMClient.get_model", fake_get_model)

    questions = await generate_recommend_questions(
        "那部署怎么做？",
        "可以用 Docker Compose。",
        history_context="第 1 轮\n问题: 这个项目怎么启动？\n结果: 先安装依赖再运行服务。",
    )

    prompt = model.prompts[0]
    assert "Recent conversation history" in prompt
    assert "这个项目怎么启动？" in prompt
    assert "先安装依赖再运行服务。" in prompt
    assert "那部署怎么做？" in prompt
    assert questions == ["问题一？", "问题二？", "问题三？"]


async def test_generate_recommend_questions_offloads_json_parsing(monkeypatch) -> None:
    model = _FakeModel()
    calls = []

    async def fake_get_model(**kwargs):
        return model

    async def fake_run_blocking_io(func, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr("src.infra.llm.client.LLMClient.get_model", fake_get_model)
    monkeypatch.setattr("src.agents.core.recommendations.run_blocking_io", fake_run_blocking_io)

    questions = await generate_recommend_questions("如何准备半程马拉松？")

    assert calls == [build_recommend_prompt, json.loads]
    assert questions == ["问题一？", "问题二？", "问题三？"]


async def test_schedule_recommend_questions_offloads_history_formatting(monkeypatch) -> None:
    calls = []
    presenter = _FakePresenter()

    async def fake_run_blocking_io(func, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    async def fake_generate_recommend_questions(
        user_input: str,
        output_text: str = "",
        history_context: str = "",
    ):
        assert history_context
        return ["问题一？", "问题二？", "问题三？"]

    monkeypatch.setattr("src.agents.core.recommendations.run_blocking_io", fake_run_blocking_io)
    monkeypatch.setattr(
        "src.agents.core.recommendations.generate_recommend_questions",
        fake_generate_recommend_questions,
    )

    task = schedule_recommend_questions(
        presenter,
        "当前问题",
        output_text="当前结果",
        messages=[
            {"role": "user", "content": "历史问题"},
            {"role": "assistant", "content": "历史结果"},
        ],
    )

    await task

    assert calls == [format_history_from_messages]
    assert presenter.questions == ["问题一？", "问题二？", "问题三？"]


def test_build_recommend_prompt_stays_under_token_budget() -> None:
    prompt = build_recommend_prompt(
        user_input="当前问题" * 1000,
        output_text="当前结果" * 2000,
        history_context="历史问题和结果" * 12000,
    )

    assert len(prompt) <= MAX_RECOMMEND_PROMPT_CHARS
    assert count_recommend_prompt_tokens(prompt) <= MAX_RECOMMEND_PROMPT_TOKENS
    assert "Current user message" in prompt
    assert "Current assistant answer" in prompt


def test_format_history_context_uses_recent_questions_and_results() -> None:
    context = format_history_context(
        [
            {
                "run_id": "run-1",
                "event_type": "user:message",
                "data": {"content": "旧问题"},
            },
            {
                "run_id": "run-1",
                "event_type": "message:chunk",
                "data": {"content": "旧结果"},
            },
            {
                "run_id": "run-2",
                "event_type": "user:message",
                "data": {"content": "新问题"},
            },
            {
                "run_id": "run-2",
                "event_type": "message:chunk",
                "data": {"content": "新"},
            },
            {
                "run_id": "run-2",
                "event_type": "message:chunk",
                "data": {"content": "结果"},
            },
        ],
        max_chars=50,
    )

    assert "Question: 新问题" in context
    assert "Result: 新结果" in context
    assert "旧问题" not in context


def test_format_history_from_messages_uses_graph_state_messages() -> None:
    class _HumanMessage:
        type = "human"
        content = "历史问题"

    class _AIMessage:
        type = "ai"
        content = "历史结果"

    context = format_history_from_messages(
        [_HumanMessage(), _AIMessage(), {"role": "user", "content": "当前问题"}],
        current_user_input="当前问题",
        current_output="当前结果",
    )

    assert "历史问题" in context
    assert "历史结果" in context
    assert "当前问题" not in context


async def test_generate_recommend_questions_falls_back_quietly_without_title_api(
    monkeypatch,
) -> None:
    async def fake_get_model(**kwargs):
        raise RuntimeError("title api missing")

    def fail_on_warning(*args, **kwargs):
        raise AssertionError("LLM recommendation fallback should not warn")

    monkeypatch.setattr("src.infra.llm.client.LLMClient.get_model", fake_get_model)
    monkeypatch.setattr(
        "src.agents.core.recommendations.settings.SESSION_TITLE_API_BASE",
        "",
    )
    monkeypatch.setattr(
        "src.agents.core.recommendations.settings.SESSION_TITLE_API_KEY",
        "",
    )
    monkeypatch.setattr(
        "src.agents.core.recommendations.logger.warning",
        fail_on_warning,
    )

    questions = await generate_recommend_questions("如何准备半程马拉松？")

    assert questions == [
        "如何准备半程马拉松？还有哪些关键步骤？",
        "如何准备半程马拉松？有哪些常见误区？",
        "下一步我应该怎么做？",
    ]


async def test_recommendation_node_emits_llm_followup_questions(monkeypatch) -> None:
    presenter = _FakePresenter()

    async def fake_generate_recommend_questions(
        user_input: str,
        output_text: str = "",
        history_context: str = "",
    ):
        return ["问题一？", "问题二？", "问题三？"]

    monkeypatch.setattr(
        "src.agents.core.recommendations.generate_recommend_questions",
        fake_generate_recommend_questions,
    )

    task = schedule_recommend_questions(
        presenter,
        "如何准备半程马拉松？",
        output_text="先建立基础跑量。",
        messages=[],
    )

    assert presenter.questions is None
    await task
    assert presenter.questions == ["问题一？", "问题二？", "问题三？"]


async def test_recommendation_background_tasks_are_bounded(monkeypatch) -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def fake_generate_recommend_questions(
        user_input: str,
        output_text: str = "",
        history_context: str = "",
    ):
        calls.append(user_input)
        first_started.set()
        await release_first.wait()
        return [f"{user_input}？"]

    monkeypatch.setattr(
        "src.agents.core.recommendations.settings.RECOMMEND_QUESTIONS_MAX_BACKGROUND_TASKS",
        1,
        raising=False,
    )
    monkeypatch.setattr(
        "src.agents.core.recommendations.generate_recommend_questions",
        fake_generate_recommend_questions,
    )

    first_presenter = _FakePresenter()
    second_presenter = _FakePresenter()
    first_task = schedule_recommend_questions(first_presenter, "第一个问题")
    await first_started.wait()

    second_task = schedule_recommend_questions(second_presenter, "第二个问题")
    await second_task

    assert calls == ["第一个问题"]
    assert second_presenter.questions is None

    release_first.set()
    await first_task
    assert first_presenter.questions == ["第一个问题？"]


async def test_drain_recommend_background_tasks_cancels_pending_tasks(monkeypatch) -> None:
    started = asyncio.Event()
    cleanup_finished = False

    async def fake_generate_recommend_questions(
        user_input: str,
        output_text: str = "",
        history_context: str = "",
    ):
        nonlocal cleanup_finished
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_finished = True

    monkeypatch.setattr(
        "src.agents.core.recommendations.generate_recommend_questions",
        fake_generate_recommend_questions,
    )

    task = schedule_recommend_questions(_FakePresenter(), "长任务")
    await started.wait()

    await drain_recommend_background_tasks()

    assert task.cancelled() is True
    assert cleanup_finished is True


def test_langgraph_agents_do_not_block_on_recommendation_node() -> None:
    for agent_cls in (SearchAgent, FastAgent, TeamAgent):
        builder = _RecordingBuilder()
        agent_cls().build_graph(builder)

        assert [name for name, _ in builder.nodes] == ["agent"]
        assert ("agent", "END") in builder.edges

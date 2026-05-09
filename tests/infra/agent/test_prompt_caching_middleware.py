from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool

from src.infra.agent.middleware import PromptCachingMiddleware, SectionPromptMiddleware
from src.infra.tool.deferred_manager import DeferredToolManager
from src.kernel.config import settings


class _FakeTool(BaseTool):
    name: str
    description: str
    server: str = ""

    def _run(self, *args, **kwargs):
        return "ok"


def test_retag_system_message_tags_multiple_tail_blocks() -> None:
    system_message = SystemMessage(
        content=[
            {"type": "text", "text": "base"},
            {"type": "text", "text": "stable"},
            {"type": "text", "text": "memory"},
            {"type": "text", "text": "dynamic"},
        ]
    )

    retagged = PromptCachingMiddleware._retag_system_message(
        system_message, {"type": "ephemeral"}, max_cached_blocks=3
    )

    assert isinstance(retagged.content, list)
    assert "cache_control" not in retagged.content[0]
    assert retagged.content[1]["cache_control"] == {"type": "ephemeral"}
    assert retagged.content[2]["cache_control"] == {"type": "ephemeral"}
    assert retagged.content[3]["cache_control"] == {"type": "ephemeral"}


def test_retag_tools_tags_multiple_tail_tools() -> None:
    tools = [
        _FakeTool(name="alpha", description="a"),
        _FakeTool(name="beta", description="b"),
        _FakeTool(name="gamma", description="c"),
    ]

    retagged = PromptCachingMiddleware._retag_tools(
        tools, {"type": "ephemeral"}, max_cached_tools=2
    )

    assert retagged is not None
    assert retagged[0].extras in (None, {})
    assert retagged[1].extras == {"cache_control": {"type": "ephemeral"}}
    assert retagged[2].extras == {"cache_control": {"type": "ephemeral"}}


async def test_prompt_caching_middleware_respects_four_breakpoint_budget() -> None:
    middleware = PromptCachingMiddleware()

    class _AnthropicLike:
        pass

    _AnthropicLike.__module__ = "langchain_anthropic.chat_models"

    class _Request:
        def __init__(self) -> None:
            self.model = _AnthropicLike()
            self.system_message = SystemMessage(
                content=[
                    {"type": "text", "text": "base"},
                    {"type": "text", "text": "persona"},
                    {"type": "text", "text": "skills"},
                    {"type": "text", "text": "memory"},
                    {"type": "text", "text": "deferred"},
                ]
            )
            self.tools = [_FakeTool(name=f"tool_{i}", description=f"tool {i}") for i in range(5)]

        def override(self, **kwargs):
            clone = _Request()
            clone.model = kwargs.get("model", self.model)
            clone.system_message = kwargs.get("system_message", self.system_message)
            clone.tools = kwargs.get("tools", self.tools)
            return clone

    async def _handler(request):
        return request

    result = await middleware.awrap_model_call(_Request(), _handler)

    system_breakpoints = sum(
        1
        for block in result.system_message.content
        if isinstance(block, dict) and "cache_control" in block
    )
    tool_breakpoints = sum(
        1
        for tool in result.tools
        if getattr(tool, "extras", None) and "cache_control" in tool.extras
    )

    assert system_breakpoints + tool_breakpoints == 4
    assert "cache_control" not in result.system_message.content[0]
    assert "cache_control" not in result.system_message.content[1]
    assert result.system_message.content[2]["cache_control"] == {"type": "ephemeral"}
    assert result.system_message.content[3]["cache_control"] == {"type": "ephemeral"}
    assert result.system_message.content[4]["cache_control"] == {"type": "ephemeral"}
    assert result.tools[-1].extras == {"cache_control": {"type": "ephemeral"}}


async def test_prompt_caching_middleware_skips_non_anthropic_models() -> None:
    middleware = PromptCachingMiddleware()

    class _Request:
        def __init__(self) -> None:
            self.model = object()
            self.system_message = SystemMessage(content=[{"type": "text", "text": "base"}])
            self.tools = [_FakeTool(name="alpha", description="a")]

        def override(self, **kwargs):
            clone = _Request()
            clone.model = kwargs.get("model", self.model)
            clone.system_message = kwargs.get("system_message", self.system_message)
            clone.tools = kwargs.get("tools", self.tools)
            return clone

    async def _handler(request):
        return request

    result = await middleware.awrap_model_call(_Request(), _handler)

    assert isinstance(result.system_message.content, list)
    assert "cache_control" not in result.system_message.content[0]
    assert result.tools[0].extras in (None, {})


async def test_prompt_caching_middleware_tags_anthropic_wrapped_models() -> None:
    middleware = PromptCachingMiddleware()

    class _AnthropicLike:
        pass

    _AnthropicLike.__module__ = "langchain_anthropic.chat_models"

    class _Binding:
        def __init__(self) -> None:
            self.bound = _AnthropicLike()

    class _Request:
        def __init__(self) -> None:
            self.model = _Binding()
            self.system_message = SystemMessage(content=[{"type": "text", "text": "base"}])
            self.tools = [_FakeTool(name="alpha", description="a")]

        def override(self, **kwargs):
            clone = _Request()
            clone.model = kwargs.get("model", self.model)
            clone.system_message = kwargs.get("system_message", self.system_message)
            clone.tools = kwargs.get("tools", self.tools)
            return clone

    async def _handler(request):
        return request

    result = await middleware.awrap_model_call(_Request(), _handler)

    assert isinstance(result.system_message.content, list)
    assert result.system_message.content[0]["cache_control"] == {"type": "ephemeral"}
    assert result.tools[0].extras == {"cache_control": {"type": "ephemeral"}}


def test_prompt_caching_middleware_uses_settings_for_cache_limits(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROMPT_CACHE_MAX_SYSTEM_BLOCKS", 6, raising=False)
    monkeypatch.setattr(settings, "PROMPT_CACHE_MAX_TOOLS", 5, raising=False)

    middleware = PromptCachingMiddleware()

    assert middleware._max_cached_system_blocks == 6
    assert middleware._max_cached_tools == 5


def test_deferred_manager_returns_discovered_tools_in_sorted_order() -> None:
    manager = DeferredToolManager(
        all_deferred_tools=[
            _FakeTool(name="zeta:lookup", description="zeta lookup", server="zeta"),
            _FakeTool(name="alpha:create", description="alpha create", server="alpha"),
            _FakeTool(name="beta:list", description="beta list", server="beta"),
        ],
        session_id="session-1",
        pre_discovered_names=["zeta:lookup", "alpha:create"],
    )

    discovered = manager.get_discovered_tools()

    assert [tool.name for tool in discovered] == ["alpha:create", "zeta:lookup"]


def test_deferred_prompt_does_not_repeat_loaded_tool_names() -> None:
    manager = DeferredToolManager(
        all_deferred_tools=[
            _FakeTool(name="alpha:create", description="alpha create", server="alpha"),
            _FakeTool(name="beta:list", description="beta list", server="beta"),
        ],
        session_id="session-1",
        pre_discovered_names=["alpha:create"],
    )

    prompt = manager.get_deferred_stubs_string()

    assert "## MCP Tools (Loaded)" not in prompt
    assert "- alpha:create" not in prompt
    assert "- beta:list: beta list" in prompt


def test_deferred_prompt_blocks_split_stable_rules_and_dynamic_tool_list() -> None:
    manager = DeferredToolManager(
        all_deferred_tools=[
            _FakeTool(name="beta:list", description="beta list", server="beta"),
        ],
        session_id="session-1",
    )

    blocks = manager.get_deferred_prompt_blocks()

    assert len(blocks) == 2
    assert "search_tools" in blocks[0]
    assert "- beta:list: beta list" in blocks[1]


def test_deferred_prompt_string_is_stably_sorted() -> None:
    manager = DeferredToolManager(
        all_deferred_tools=[
            _FakeTool(name="zeta:lookup", description="zeta lookup", server="zeta"),
            _FakeTool(name="alpha:create", description="alpha create", server="alpha"),
            _FakeTool(name="beta:list", description="beta list", server="beta"),
        ],
        session_id="session-1",
        pre_discovered_names=["beta:list"],
    )

    prompt = manager.get_deferred_stubs_string()

    assert prompt.index("- alpha:create: alpha create") < prompt.index("- zeta:lookup: zeta lookup")


def test_deferred_prompt_string_survives_prior_stub_cache_access() -> None:
    manager = DeferredToolManager(
        all_deferred_tools=[
            _FakeTool(name="alpha:create", description="alpha create", server="alpha"),
        ],
        session_id="session-1",
    )

    stubs = manager.get_deferred_stubs()
    prompt = manager.get_deferred_stubs_string()

    assert [stub.name for stub in stubs] == ["alpha:create"]
    assert "## MCP Tools (Deferred)" in prompt
    assert "- alpha:create: alpha create" in prompt


def test_deferred_prompt_string_truncates_long_tool_list() -> None:
    manager = DeferredToolManager(
        all_deferred_tools=[
            _FakeTool(name="alpha:create", description="alpha create", server="alpha"),
            _FakeTool(name="beta:list", description="beta list", server="beta"),
            _FakeTool(name="gamma:query", description="gamma query", server="gamma"),
        ],
        session_id="session-1",
        prompt_tool_limit=2,
    )

    prompt = manager.get_deferred_stubs_string()

    assert "- alpha:create: alpha create" in prompt
    assert "- beta:list: beta list" in prompt
    assert "- gamma:query: gamma query" not in prompt
    assert "1 more deferred MCP tool not shown" in prompt


async def test_section_prompt_middleware_appends_separate_blocks() -> None:
    middleware = SectionPromptMiddleware(sections=["skills block", "memory block"])

    class _Request:
        def __init__(self) -> None:
            self.system_message = SystemMessage(content=[{"type": "text", "text": "base"}])

        def override(self, **kwargs):
            clone = _Request()
            clone.system_message = kwargs.get("system_message", self.system_message)
            return clone

    async def _handler(request):
        return request.system_message

    result = await middleware.awrap_model_call(_Request(), _handler)

    assert isinstance(result.content, list)
    assert [block["text"] for block in result.content] == ["base", "skills block", "memory block"]

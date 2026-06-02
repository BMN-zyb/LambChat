from types import SimpleNamespace
from typing import Any

import pytest

from src.infra.mcp.storage_operations import StorageOperations


def test_mcp_tool_policy_schema_preserves_allowed_roles_and_quotas() -> None:
    from src.kernel.schemas.mcp import MCPRoleQuota, MCPToolPolicy

    policy = MCPToolPolicy.model_validate(
        {
            "allowed_roles": ["admin", "user"],
            "role_quotas": {
                "admin": {"daily_limit": 3, "weekly_limit": 10},
            },
        }
    )

    assert policy.allowed_roles == ["admin", "user"]
    assert policy.role_quotas == {"admin": MCPRoleQuota(daily_limit=3, weekly_limit=10)}


@pytest.mark.asyncio
async def test_mcp_storage_round_trips_tool_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.infra.mcp.storage import MCPStorage
    from src.kernel.schemas.mcp import MCPRoleQuota

    fake_docs: dict[tuple[str, str], dict[str, object]] = {}

    class _FakeCollection:
        async def update_one(self, query, update, upsert=False):
            key = (query["server_name"], query["tool_name"])
            fake_docs[key] = update["$set"]
            return SimpleNamespace()

        async def find_one(self, query):
            return fake_docs.get((query["server_name"], query["tool_name"]))

    storage = MCPStorage()
    monkeypatch.setattr(storage, "_get_tool_policies_collection", lambda: _FakeCollection())
    monkeypatch.setattr(storage, "_invalidate_all_cache", lambda: None)
    monkeypatch.setattr(storage, "_invalidate_user_cache", lambda user_id: None)

    await storage.set_tool_policy(
        server_name="lambchat_internal",
        tool_name="image_generate",
        allowed_roles=["admin"],
        role_quotas={"admin": MCPRoleQuota(daily_limit=2)},
        updated_by="admin-1",
    )

    policy = await storage.get_tool_policy("lambchat_internal", "image_generate")

    assert policy is not None
    assert policy.allowed_roles == ["admin"]
    assert policy.role_quotas["admin"].daily_limit == 2


@pytest.mark.asyncio
async def test_internal_tool_policies_filter_blocked_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.tools import BaseTool

    from src.infra.tool import internal_registry
    from src.kernel.schemas.mcp import MCPToolPolicy

    class _FakeTool(BaseTool):
        name: str
        description: str = ""

        def _run(self, *args, **kwargs):
            return "sync"

        async def _arun(self, *args, **kwargs):
            return "async"

    class _FakeStorage:
        async def list_tool_policies(self, server_name: str):
            assert server_name == "lambchat_internal"
            return {
                "image_generate": MCPToolPolicy(
                    server_name="lambchat_internal",
                    tool_name="image_generate",
                    allowed_roles=["admin"],
                )
            }

    monkeypatch.setattr(internal_registry, "MCPStorage", lambda: _FakeStorage())
    monkeypatch.setattr(
        internal_registry,
        "build_internal_tools",
        lambda: [_FakeTool(name="image_generate"), _FakeTool(name="env_var_list")],
    )

    tools = await internal_registry.get_internal_tools_for_user(
        user_id="user-1",
        user_roles=["user"],
        is_admin=False,
    )

    assert [tool.name for tool in tools] == ["env_var_list"]


@pytest.mark.asyncio
async def test_internal_image_generate_tool_infos_include_supported_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.infra.tool import internal_registry
    from src.infra.tool.image_generation_tool import get_image_generation_tool

    async def _empty_policies():
        return {}

    monkeypatch.setattr(internal_registry, "get_internal_tool_policies", _empty_policies)
    monkeypatch.setattr(
        internal_registry,
        "build_internal_tools",
        lambda: [get_image_generation_tool()],
    )

    infos = await internal_registry.get_internal_tool_infos(
        user_id="admin-1",
        user_roles=["admin"],
        is_admin=True,
    )

    params = {param["name"]: param for param in infos[0].parameters}

    for name in (
        "prompt",
        "input_images",
        "background",
        "input_fidelity",
        "size",
        "quality",
        "n",
        "output_format",
    ):
        assert name in params
        assert params[name]["description"]
    assert "runtime" not in params
    assert "mask_url" not in params


class _AsyncCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def __aiter__(self):
        for doc in self._docs:
            yield doc


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def find(self, query: dict[str, Any]):
        return _AsyncCursor(
            [
                doc
                for doc in self._docs
                if all(doc.get(key) == value for key, value in query.items())
            ]
        )


class _BulkPolicyStorage(StorageOperations):
    def __init__(self) -> None:
        self.bulk_calls: list[list[str]] = []

    async def _get_user_preferences(self, user_id: str) -> dict[str, bool]:
        return {}

    def _get_system_collection(self) -> _FakeCollection:
        return _FakeCollection(
            [
                {"name": "alpha", "transport": "sse", "enabled": True},
                {"name": "beta", "transport": "sse", "enabled": True},
            ]
        )

    def _get_user_collection(self) -> _FakeCollection:
        return _FakeCollection([])

    def _doc_to_config_dict(self, doc: dict[str, Any]) -> dict[str, Any]:
        return {"transport": doc["transport"]}

    async def list_tool_policies(self, server_name: str):
        raise AssertionError("get_effective_config should use bulk policy loading")

    async def list_tool_policies_for_servers(self, server_names: list[str]):
        self.bulk_calls.append(server_names)
        from src.kernel.schemas.mcp import MCPToolPolicy

        return {
            "alpha": {
                "search": MCPToolPolicy(
                    server_name="alpha",
                    tool_name="search",
                    disabled=True,
                )
            }
        }


@pytest.mark.asyncio
async def test_effective_config_loads_system_tool_policies_in_bulk() -> None:
    storage = _BulkPolicyStorage()

    config = await storage.get_effective_config("user-1")

    assert storage.bulk_calls == [["alpha", "beta"]]
    assert config["mcpServers"]["alpha"]["tool_policies"]["search"]["disabled"] is True
    assert "tool_policies" not in config["mcpServers"]["beta"]


class _LimitedEffectiveConfigStorage(StorageOperations):
    async def _get_user_preferences(self, user_id: str) -> dict[str, bool]:
        return {}

    def _get_system_collection(self) -> _FakeCollection:
        return _FakeCollection(
            [{"name": f"system-{index}", "transport": "sse", "enabled": True} for index in range(5)]
        )

    def _get_user_collection(self) -> _FakeCollection:
        return _FakeCollection([])

    def _doc_to_config_dict(self, doc: dict[str, Any]) -> dict[str, Any]:
        return {"transport": doc["transport"]}

    async def list_tool_policies_for_servers(self, server_names: list[str]):
        return {}


@pytest.mark.asyncio
async def test_effective_config_caps_loaded_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.kernel.config import settings

    monkeypatch.setattr(settings, "MCP_EFFECTIVE_CONFIG_MAX_SERVERS", 2)
    storage = _LimitedEffectiveConfigStorage()
    config = await storage.get_effective_config("user-1")

    assert list(config["mcpServers"]) == ["system-0", "system-1"]

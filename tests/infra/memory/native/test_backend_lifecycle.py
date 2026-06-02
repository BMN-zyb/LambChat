import pytest

from src.infra.memory.client.native import backend as backend_module
from src.infra.memory.client.native.backend import NativeMemoryBackend


@pytest.mark.asyncio
async def test_delete_removes_store_payload_for_long_memory():
    seen: dict[str, object] = {}

    class FakeCollection:
        async def find_one(self, query, _projection=None):
            seen["find_query"] = query
            return {
                "user_id": "u1",
                "memory_id": "m1",
                "content_storage_mode": "store",
                "content_store_key": "memory:m1",
            }

        async def delete_one(self, query):
            seen["delete_query"] = query

            class Result:
                deleted_count = 1

            return Result()

    class FakeStore:
        async def aput(self, namespace, key, value):
            seen["store_delete"] = {"namespace": namespace, "key": key, "value": value}

    backend = NativeMemoryBackend()
    backend._collection = FakeCollection()
    backend._store = FakeStore()

    async def fake_invalidate(_user_id):
        seen["invalidated"] = True

    backend._invalidate_cache = fake_invalidate  # type: ignore[method-assign]

    result = await backend.delete("u1", "m1")

    assert result["success"] is True
    assert seen["find_query"] == {"user_id": "u1", "memory_id": "m1"}
    assert seen["delete_query"] == {"user_id": "u1", "memory_id": "m1"}
    assert seen["store_delete"] == {
        "namespace": ("memories", "u1", "content"),
        "key": "memory:m1",
        "value": None,
    }
    assert seen["invalidated"] is True


@pytest.mark.asyncio
async def test_maybe_embed_offloads_sync_embedding_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    backend = NativeMemoryBackend()

    def sync_embedding(text: str) -> list[float]:
        assert text == "hello"
        return [1.0, 2.0]

    async def fake_run_blocking_io(func, *args, **kwargs):
        calls.append(func)
        return func(*args, **kwargs)

    backend._embedding_fn = sync_embedding
    monkeypatch.setattr(backend_module, "run_blocking_io", fake_run_blocking_io)

    result = await backend._maybe_embed("hello")

    assert result == [1.0, 2.0]
    assert calls == [sync_embedding]

"""Library-mode store tests against a FAKE Memory object (never imports
mem0ai — the default suite must stay SDK-free), plus one importorskip smoke
test for the real object shape and the rule-4 containment pin.
"""

import json
import sys
import types

import pytest

from mem0_bridge.client import Mem0ApiError
from mem0_bridge.stores.library import LibraryStore, _openai_v1_root

SETTINGS = {
    "mode": "library",
    "run_root": "/tmp/run-root",
    "llm_model": "deepseek-v4-flash",
    "llm_api_key": "roster-key",
    "llm_base_url": "https://api.deepseek.com",
    "embedding_model": "text-embedding-3-small",
    "embedding_api_key": "emb-key",
    "embedding_base_url": "https://emb.invalid",
    "embedding_dimensions": 1536,
}


class FakeMemory:
    """Records calls and mimics the engine's wire shapes."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.memories: dict[str, dict] = {}
        self.add_response = None  # override to script a raw return value

    def add(self, messages, **kwargs):
        self.calls.append(("add", {"messages": messages, **kwargs}))
        if self.add_response is not None:
            return self.add_response
        results = []
        for message in messages:
            memory_id = f"m{len(self.memories) + 1}"
            self.memories[memory_id] = {
                "id": memory_id,
                "memory": f"fact: {message['content'][:40]}",
                "user_id": kwargs.get("user_id"),
                "run_id": kwargs.get("run_id"),
                "score": 0.8,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
            results.append({"id": memory_id, "memory": self.memories[memory_id]["memory"], "event": "ADD"})
        return {"results": results}

    def search(self, *, query, filters, top_k, threshold):
        self.calls.append(("search", {"query": query, "filters": filters, "top_k": top_k, "threshold": threshold}))
        hits = [dict(m) for m in self.memories.values() if m["user_id"] == filters.get("user_id")]
        return {"results": hits[:top_k]}

    def get(self, memory_id):
        self.calls.append(("get", {"memory_id": memory_id}))
        memory = self.memories.get(memory_id)
        return dict(memory) if memory is not None else None

    def get_all(self, *, filters, top_k):
        self.calls.append(("get_all", {"filters": filters, "top_k": top_k}))
        hits = [dict(m) for m in self.memories.values() if m["user_id"] == filters.get("user_id")]
        return {"results": hits[:top_k]}

    def update(self, memory_id, text=None, metadata=None):
        self.calls.append(("update", {"memory_id": memory_id, "text": text, "metadata": metadata}))
        memory = self.memories.get(memory_id)
        if memory is None:
            raise ValueError(f"Memory with id {memory_id} not found")
        if text is not None:
            memory["memory"] = text
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id):
        self.calls.append(("delete", {"memory_id": memory_id}))
        if memory_id not in self.memories:
            raise ValueError(f"Memory with id {memory_id} not found")
        del self.memories[memory_id]
        return {"message": "Memory deleted successfully!"}


@pytest.fixture
def store():
    return LibraryStore(SETTINGS, memory=FakeMemory())


def test_openai_v1_root_normalization():
    assert _openai_v1_root("https://api.deepseek.com") == "https://api.deepseek.com/v1"
    assert _openai_v1_root("https://api.deepseek.com/") == "https://api.deepseek.com/v1"
    assert _openai_v1_root("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"


def test_config_dict_shape(monkeypatch, tmp_path):
    """The engine config: roster LLM + embedding quartet (dims threaded to the
    vector store too) + store paths under the run root, both upstream URLs
    normalized to the /v1 root."""
    captured = {}

    class FakeMemoryClass:
        @staticmethod
        def from_config(config):
            captured["config"] = config
            return FakeMemory()

    monkeypatch.setitem(sys.modules, "mem0", types.SimpleNamespace(Memory=FakeMemoryClass))
    LibraryStore({**SETTINGS, "run_root": str(tmp_path)})
    config = captured["config"]
    assert config["llm"] == {
        "provider": "openai",
        "config": {
            "model": "deepseek-v4-flash",
            "api_key": "roster-key",
            "openai_base_url": "https://api.deepseek.com/v1",
            "max_tokens": 32000,
        },
    }
    assert config["embedder"] == {
        "provider": "openai",
        "config": {
            "model": "text-embedding-3-small",
            "api_key": "emb-key",
            "openai_base_url": "https://emb.invalid/v1",
            "embedding_dims": 1536,
        },
    }
    assert config["vector_store"] == {
        "provider": "qdrant",
        "config": {"collection_name": "mem0", "path": str(tmp_path / "mem0" / "qdrant"), "embedding_model_dims": 1536},
    }
    assert config["history_db_path"] == str(tmp_path / "mem0" / "history.db")
    assert (tmp_path / "mem0").is_dir()


def test_add_is_sync_and_conveys_guidelines_via_prompt(store):
    results = store.add(
        messages=[{"role": "user", "content": "hello"}],
        user_id="alice",
        run_id="run-1",
        infer=True,
        guidelines="  prefer operational facts  ",
    )
    assert results == [{"id": "m1", "memory": "fact: hello", "event": "ADD"}]
    _, call = store._memory.calls[0]
    assert call["prompt"] == "prefer operational facts"
    assert call["user_id"] == "alice" and call["run_id"] == "run-1" and call["infer"] is True

    store.add(messages=[{"role": "user", "content": "again"}], user_id="alice", guidelines="   ")
    assert store._memory.calls[1][1]["prompt"] is None


def test_add_tolerates_a_bare_list_response(store):
    store._memory.add_response = [{"id": "m9", "memory": "flat", "event": "ADD"}]
    assert store.add(messages=[{"role": "user", "content": "x"}], user_id="alice") == [
        {"id": "m9", "memory": "flat", "event": "ADD"}
    ]


def test_search_sends_entity_filter_and_explicit_threshold(store):
    store.add(messages=[{"role": "user", "content": "tea facts"}], user_id="alice")
    hits = store.search(query="facts", user_id="alice", top_k=5, threshold=0.0, timeout=3.0)
    assert hits[0]["id"] == "m1"
    _, call = store._memory.calls[-1]
    assert call == {"query": "facts", "filters": {"user_id": "alice"}, "top_k": 5, "threshold": 0.0}


def test_get_all_sends_entity_filter_and_explicit_top_k(store):
    store.add(messages=[{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], user_id="alice")
    rows = store.get_all(user_id="alice", limit=10000)
    assert len(rows) == 2
    _, call = store._memory.calls[-1]
    assert call == {"filters": {"user_id": "alice"}, "top_k": 10000}


def test_get_missing_id_raises_404(store):
    with pytest.raises(Mem0ApiError) as excinfo:
        store.get("missing")
    assert excinfo.value.status_code == 404


def test_update_echoes_via_get_and_maps_missing_404(store):
    store.add(messages=[{"role": "user", "content": "old"}], user_id="alice")
    memory = store.update("m1", text="new text")
    assert memory["memory"] == "new text"
    assert [name for name, _ in store._memory.calls[-2:]] == ["update", "get"]
    with pytest.raises(Mem0ApiError) as excinfo:
        store.update("missing", text="x")
    assert excinfo.value.status_code == 404


def test_delete_maps_missing_404(store):
    with pytest.raises(Mem0ApiError) as excinfo:
        store.delete("missing")
    assert excinfo.value.status_code == 404


def test_health_reports_the_engine_version(store):
    assert store.health()["status"] == "ok"


def test_library_settings_carry_no_key_material_into_artifacts(make_backend, monkeypatch, tmp_path):
    """Rule 4 for library mode: the resolved settings carry real roster
    LLM/embedder keys — memory.json and stats must not (tested, not assumed)."""
    for name in ("MEM0_API_KEY", "MEM0_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_API_KEY", "emb-secret-key")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://emb.invalid")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1536")
    monkeypatch.setenv("MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("API_KEY", "roster-secret-key")
    monkeypatch.setenv("BASE_URL", "https://api.deepseek.com")
    backend = make_backend(mode="library", run_root=str(tmp_path))
    backend.start()
    assert backend._available is True
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend.maybe_extract(10)
    backend.finalize()
    artifact = (tmp_path / "test-instance" / "memory.json").read_text()
    assert "roster-secret-key" not in artifact
    assert "emb-secret-key" not in artifact
    assert "roster-secret-key" not in json.dumps(backend.stats())


def test_real_memory_constructs_offline(tmp_path):
    """Smoke: the real mem0ai object shape (group-installed runs only). The
    skip must guard the NAME import, not `import mem0`: with integration/ on
    sys.path (the tencentdb tests package's pytest insertion), a bare `import
    mem0` resolves to a stray namespace package even when the SDK is absent."""
    try:
        from mem0 import Memory  # noqa: F401
    except ImportError:
        pytest.skip("mem0ai not installed (opt-in mem0-library group)")
    store = LibraryStore({**SETTINGS, "run_root": str(tmp_path)})
    assert store.health()["engine"] == "2.0.19"
    store.close()

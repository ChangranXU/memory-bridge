"""Store-layer tests: the PlatformStore adapter, the open_store factory, and
the backend/endpoint parity pins (both retrieval surfaces over one store)."""

import json

import httpx
import pytest

from mem0_bridge.client import Mem0ApiError
from mem0_bridge.stores import open_store
from mem0_bridge.stores.platform import PlatformStore


def make_store(handler) -> tuple[PlatformStore, list[httpx.Request]]:
    """A PlatformStore over a mock transport (the client constructor's
    transport seam, reached through the store's one client)."""
    requests: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    store = PlatformStore(api_key="m0-test-key", base_url="https://api.mem0.ai", poll_budget=5.0, poll_interval=0.0)
    store._client._client = httpx.Client(
        base_url="https://api.mem0.ai",
        headers={"Authorization": "Token m0-test-key"},
        transport=httpx.MockTransport(transport_handler),
    )
    return store, requests


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


def test_factory_dispatches_platform():
    store = open_store(
        "platform",
        {"mode": "platform", "api_key": "k", "base_url": "https://api.mem0.ai", "poll_budget": 1.0, "poll_interval": 0.1},
    )
    assert isinstance(store, PlatformStore)
    store.close()


def test_factory_unknown_mode_rejected():
    with pytest.raises(ValueError, match="unknown mem0 mode"):
        open_store("bogus", {})


def test_platform_add_maps_guidelines_to_custom_instructions():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": "id-1", "memory": "fact", "event": "ADD"}]})

    store, requests = make_store(handler)
    store.add(messages=[{"role": "user", "content": "x"}], user_id="alice", guidelines="prefer operational facts")
    assert body_of(requests[0])["custom_instructions"] == "prefer operational facts"
    store.add(messages=[{"role": "user", "content": "y"}], user_id="alice")
    assert "custom_instructions" not in body_of(requests[1])


def test_platform_get_all_paginates_until_next_is_null():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page < 3:
            return httpx.Response(
                200,
                json={
                    "count": 5,
                    "next": f"https://api.mem0.ai/v3/memories/?page={page + 1}",
                    "previous": None,
                    "results": [{"id": f"p{page}a"}, {"id": f"p{page}b"}],
                },
            )
        return httpx.Response(
            200, json={"count": 5, "next": None, "previous": "...", "results": [{"id": "p3a"}]}
        )

    store, requests = make_store(handler)
    rows = store.get_all(user_id="alice", limit=100)
    assert [row["id"] for row in rows] == ["p1a", "p1b", "p2a", "p2b", "p3a"]
    assert [r.url.params["page"] for r in requests] == ["1", "2", "3"]


def test_platform_get_all_limit_caps_and_an_empty_page_ends_the_walk():
    def handler(request: httpx.Request) -> httpx.Response:
        # A drifting ``next`` with empty pages must not spin the loop forever.
        return httpx.Response(200, json={"count": 0, "next": "https://api.mem0.ai/v3/memories/?page=2", "results": []})

    store, requests = make_store(handler)
    assert store.get_all(user_id="alice", limit=100) == []
    assert len(requests) == 1


def test_platform_get_all_keeps_a_constant_page_size_across_the_walk():
    """DRF computes a page's offset as (page-1)*page_size: a page_size that
    shrinks on the final page re-picks earlier rows into the dump."""
    total = [{"id": f"r{i}"} for i in range(120)]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        page_size = int(request.url.params["page_size"])
        start = (page - 1) * page_size
        return httpx.Response(
            200,
            json={
                "count": len(total),
                "next": None if start + page_size >= len(total) else f"https://api.mem0.ai/v3/memories/?page={page + 1}",
                "previous": None,
                "results": total[start : start + page_size],
            },
        )

    store, requests = make_store(handler)
    rows = store.get_all(user_id="alice", limit=150)  # not a multiple of 100
    assert [row["id"] for row in rows] == [f"r{i}" for i in range(120)]
    assert [r.url.params["page_size"] for r in requests] == ["100", "100"]


def test_platform_get_all_fails_closed_on_a_drifted_envelope():
    """Same rule as add/search: a page without the results list is drift, not
    an empty page — coercing it would silently truncate the final dump with
    no counter moving (a null ``results`` previously crashed the walk with a
    bare TypeError)."""
    store, _ = make_store(lambda r: httpx.Response(200, json={"count": 1, "next": None, "results": None}))
    with pytest.raises(Mem0ApiError) as excinfo:
        store.get_all(user_id="alice", limit=100)
    assert excinfo.value.status_code == 502
    assert "results" in excinfo.value.reason  # the drifted body's keys, never its content


def test_platform_get_all_does_not_end_the_walk_on_drifted_page_items():
    """The walk ends on the envelope's own null ``next`` or a genuinely EMPTY
    page — never on the filtered batch: a page whose items are all non-dict
    drift with ``next`` still set must not read as end-of-stream and silently
    truncate the dump."""
    pages = {
        1: httpx.Response(
            200, json={"count": 2, "next": "https://api.mem0.ai/v3/memories/?page=2", "results": ["drift", 7]}
        ),
        2: httpx.Response(200, json={"count": 2, "next": None, "results": [{"id": "p2a"}]}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return pages[int(request.url.params["page"])]

    store, requests = make_store(handler)
    assert [row["id"] for row in store.get_all(user_id="alice", limit=100)] == ["p2a"]
    assert [r.url.params["page"] for r in requests] == ["1", "2"]


def test_platform_store_searches_with_explicit_threshold_and_timeout():
    store, requests = make_store(lambda r: httpx.Response(200, json={"results": []}))
    assert store.search(query="q", user_id="alice", top_k=7, threshold=0.25, timeout=3.0) == []
    assert body_of(requests[0]) == {"query": "q", "filters": {"user_id": "alice"}, "top_k": 7, "threshold": 0.25}
    assert requests[0].extensions["timeout"] == {"connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0}


# ---------------------------------------------------------------------------
# P2-T5 parity pins: the arm's _search and the endpoint's search issue the
# same native call shape given the same inputs, with threshold/timeout sourced
# from the same config.
# ---------------------------------------------------------------------------
def test_backend_and_endpoint_search_parity(make_backend, monkeypatch):
    from mem0_bridge.endpoint import Mem0Endpoint
    from shared_bridge.endpoint import SearchRequest

    seen: list[dict] = []

    class RecordingStore:
        def health(self):
            return {}

        def search(self, **kwargs):
            seen.append(kwargs)
            return []

        def close(self):
            pass

    monkeypatch.setattr("mem0_bridge.backend.Mem0Backend._open_store", lambda self, settings: RecordingStore())
    backend = make_backend(search_threshold=0.15, search_timeout=4.0, max_memories=6)
    backend.start()
    backend.set_task("the task query")
    backend._search()
    backend.finalize()

    endpoint = Mem0Endpoint(RecordingStore(), search_threshold=0.15, search_timeout=4.0)
    endpoint.search(SearchRequest(query="the task query", user_id="minisweagent", top_k=6))

    assert seen[0] == seen[1] == {
        "query": "the task query",
        "user_id": "minisweagent",
        "top_k": 6,
        "threshold": 0.15,
        "timeout": 4.0,
    }


def test_backend_search_widens_top_k_only_with_a_floor(make_backend, monkeypatch):
    """The backend's wider-pool policy is arm-internal: the endpoint keeps the
    contract's exact top_k. Both still share threshold/timeout semantics."""
    from mem0_bridge.endpoint import Mem0Endpoint
    from shared_bridge.endpoint import SearchRequest

    seen: list[dict] = []

    class RecordingStore:
        def health(self):
            return {}

        def search(self, **kwargs):
            seen.append(kwargs)
            return []

        def close(self):
            pass

    monkeypatch.setattr("mem0_bridge.backend.Mem0Backend._open_store", lambda self, settings: RecordingStore())
    backend = make_backend(search_threshold=0.0, recall_min_score=0.1, max_memories=10)
    backend.start()
    backend.set_task("q")
    backend._search()
    backend.finalize()
    assert seen[0]["top_k"] == 50  # max(50, max_memories) while the floor is set

    endpoint = Mem0Endpoint(RecordingStore(), search_threshold=0.0)
    endpoint.search(SearchRequest(query="q", user_id="minisweagent", top_k=10))
    assert seen[1]["top_k"] == 10
    assert seen[0]["threshold"] == seen[1]["threshold"] == 0.0


def test_mode_and_version_recorded_in_settings(make_backend):
    from pathlib import Path

    backend = make_backend()
    backend.start()
    backend.finalize()
    data = json.loads((Path(backend.config.output_dir) / "memory.json").read_text())
    assert data["settings"]["mode"] == "platform"
    assert data["settings"]["bridge_version"]
    assert data["events"][0]["kind"] == "start"
    assert data["events"][0]["mode"] == "platform"

"""Server-mode store wire-format tests against httpx.MockTransport (no network).

Pins the OSS server wire shape (verified against the vendored server's
main.py): no /v1 prefix, no trailing slashes (redirect_slashes=False makes a
trailing slash a 404), sync adds, the prompt guidelines field, the explicit
threshold, query-param get-all clamped at the server's 1000 cap, and the
null→missing mapping on GET.
"""

import json

import httpx
import pytest

from mem0_bridge.client import Mem0ApiError
from mem0_bridge.stores.server import ServerStore


def make_store(handler, **kwargs) -> tuple[ServerStore, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    store = ServerStore(
        server_url="http://127.0.0.1:8890/",
        transport=httpx.MockTransport(transport_handler),
        **kwargs,
    )
    return store, requests


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


def test_health_probes_setup_status_not_health():
    """The API server has no /health (that path is the dashboard's); readiness
    is /auth/setup-status."""
    store, requests = make_store(lambda r: httpx.Response(200, json={"needs_setup": False}))
    assert store.health() == {"needs_setup": False}
    assert str(requests[0].url) == "http://127.0.0.1:8890/auth/setup-status"


def test_no_auth_header_when_server_api_key_is_empty():
    """AUTH_DISABLED containers get NO credential header: a presented token
    would reach the JWT path and 500 without a configured JWT_SECRET."""
    store, requests = make_store(lambda r: httpx.Response(200, json={}))
    store.health()
    assert "X-API-Key" not in requests[0].headers
    assert "Authorization" not in requests[0].headers


def test_x_api_key_header_when_configured():
    store, requests = make_store(lambda r: httpx.Response(200, json={}), server_api_key="m0sk_test")
    store.health()
    assert requests[0].headers["X-API-Key"] == "m0sk_test"


def test_add_is_sync_and_carries_prompt_as_the_guidelines_slot():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/memories"  # no /v1 prefix, no trailing slash
        return httpx.Response(
            200,
            json={"results": [{"id": "id-1", "memory": "fact one", "event": "ADD"}]},
        )

    store, requests = make_store(handler)
    results = store.add(
        messages=[{"role": "user", "content": "hello"}],
        user_id="alice",
        run_id="run-1",
        infer=True,
        metadata={"k": "v"},
        guidelines="  prefer operational facts  ",
    )
    assert results == [{"id": "id-1", "memory": "fact one", "event": "ADD"}]
    assert body_of(requests[0]) == {
        "messages": [{"role": "user", "content": "hello"}],
        "user_id": "alice",
        "infer": True,
        "run_id": "run-1",
        "metadata": {"k": "v"},
        "prompt": "prefer operational facts",
    }
    assert len(requests) == 1  # sync add — no event polling


def test_add_omits_prompt_when_guidelines_empty():
    store, requests = make_store(lambda r: httpx.Response(200, json={"results": []}))
    store.add(messages=[{"role": "user", "content": "x"}], user_id="alice", guidelines="   ")
    assert "prompt" not in body_of(requests[0])


def test_add_uses_the_extended_add_timeout():
    """An infer=true add is one extraction LLM round-trip inside the request —
    far past the 30 s client default for reasoning-style models."""
    store, requests = make_store(lambda r: httpx.Response(200, json={"results": []}))
    store.add(messages=[{"role": "user", "content": "x"}], user_id="alice")
    assert requests[0].extensions["timeout"] == {"connect": 300.0, "read": 300.0, "write": 300.0, "pool": 300.0}


def test_search_posts_filters_with_explicit_threshold():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        return httpx.Response(
            200,
            json={"results": [{"id": "r1", "memory": "likes cricket", "score": 0.42, "user_id": "alice", "run_id": "run-1"}]},
        )

    store, requests = make_store(handler)
    hits = store.search(query="hobbies", user_id="alice", top_k=5, threshold=0.0, timeout=3.0)
    assert hits[0]["id"] == "r1"
    assert body_of(requests[0]) == {"query": "hobbies", "filters": {"user_id": "alice"}, "top_k": 5, "threshold": 0.0}
    assert requests[0].extensions["timeout"] == {"connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0}


def test_get_maps_200_null_to_missing_404():
    """The server's GET answers 200 null for unknown ids (PUT/DELETE already
    404) — the store maps it to the protocol's missing-id convention."""
    store, _ = make_store(lambda r: httpx.Response(200, content=b"null"))
    with pytest.raises(Mem0ApiError) as excinfo:
        store.get("missing-id")
    assert excinfo.value.status_code == 404


def test_get_all_uses_query_params_and_clamps_to_the_server_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/memories"
        return httpx.Response(200, json={"results": [{"id": "r1", "memory": "fact", "user_id": "alice", "run_id": "run-1"}]})

    store, requests = make_store(handler)
    rows = store.get_all(user_id="alice", limit=10000)
    assert [row["id"] for row in rows] == ["r1"]
    # The entity filter is hard-required by the engine; the server caps top_k
    # at ALL_MEMORIES_LIMIT=1000 — the store clamps explicitly.
    assert dict(requests[0].url.params) == {"user_id": "alice", "top_k": "1000"}


def test_update_sends_set_fields_then_echoes_via_get():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            # The OSS PUT answers a bare message — never the updated memory.
            return httpx.Response(200, json={"message": "Memory updated successfully!"})
        return httpx.Response(200, json={"id": "r1", "memory": "new text", "user_id": "alice"})

    store, requests = make_store(handler)
    memory = store.update("r1", text="new text")
    assert memory["memory"] == "new text"
    assert body_of(requests[0]) == {"text": "new text"}
    assert [r.method for r in requests] == ["PUT", "GET"]


def test_update_delete_missing_id_404_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Memory with id x not found"})

    store, _ = make_store(handler)
    with pytest.raises(Mem0ApiError) as excinfo:
        store.update("x", text="t")
    assert excinfo.value.status_code == 404
    with pytest.raises(Mem0ApiError) as excinfo:
        store.delete("x")
    assert excinfo.value.status_code == 404


def test_error_reason_extraction():
    store, _ = make_store(lambda r: httpx.Response(500, json={"detail": "upstream boom"}))
    with pytest.raises(Mem0ApiError, match="upstream boom"):
        store.health()

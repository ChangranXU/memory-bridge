"""Client wire-format tests against httpx.MockTransport (no network)."""

import json

import httpx
import pytest

from mem0_bridge.client import Mem0ApiError, Mem0PlatformClient


def make_client(handler) -> tuple[Mem0PlatformClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = Mem0PlatformClient(
        api_key="m0-test-key",
        base_url="https://api.mem0.ai",
        transport=httpx.MockTransport(transport_handler),
    )
    return client, requests


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


def test_ping_sends_token_auth():
    client, requests = make_client(lambda r: httpx.Response(200, json={"status": "ok"}))
    assert client.ping() == {"status": "ok"}
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == "https://api.mem0.ai/v1/ping/"
    assert request.headers["Authorization"] == "Token m0-test-key"


def test_add_sync_results_normalized_from_data_memory():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/memories/add/"
        return httpx.Response(
            200,
            json={
                "message": "Memories stored successfully",
                "status": "SUCCEEDED",
                "results": [
                    {"id": "id-1", "data": {"memory": "fact one"}, "event": "ADD"},
                    {"id": "id-2", "memory": "flat v1 shape", "event": "NONE"},
                ],
            },
        )

    client, requests = make_client(handler)
    results = client.add(
        messages=[{"role": "user", "content": "hello"}],
        user_id="alice",
        run_id="run-1",
        infer=False,
        metadata={"k": "v"},
    )
    assert results == [
        {"id": "id-1", "memory": "fact one", "event": "ADD"},
        {"id": "id-2", "memory": "flat v1 shape", "event": "NONE"},
    ]
    sent = body_of(requests[0])
    assert sent == {
        "messages": [{"role": "user", "content": "hello"}],
        "user_id": "alice",
        "infer": False,
        "run_id": "run-1",
        "metadata": {"k": "v"},
    }
    assert len(requests) == 1  # no polling when results came back synchronously


def test_add_async_polls_event_until_succeeded():
    polls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/memories/add/":
            return httpx.Response(200, json={"event_id": "evt-1", "status": "PENDING"})
        polls["count"] += 1
        if polls["count"] == 1:
            return httpx.Response(200, json={"id": "evt-1", "status": "RUNNING"})
        return httpx.Response(
            200,
            json={
                "id": "evt-1",
                "status": "SUCCEEDED",
                "payload": {"results": [{"id": "id-9", "data": {"memory": "polled fact"}, "event": "ADD"}]},
            },
        )

    client, requests = make_client(handler)
    results = client.add(messages=[{"role": "user", "content": "x"}], user_id="alice", poll_interval=0.0)
    assert results == [{"id": "id-9", "memory": "polled fact", "event": "ADD"}]
    assert [str(r.url) for r in requests] == [
        "https://api.mem0.ai/v3/memories/add/",
        "https://api.mem0.ai/v1/event/evt-1/",
        "https://api.mem0.ai/v1/event/evt-1/",
    ]


def test_add_async_failed_event_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/memories/add/":
            return httpx.Response(200, json={"event_id": "evt-2", "status": "PENDING"})
        return httpx.Response(200, json={"id": "evt-2", "status": "FAILED", "error": "boom"})

    client, _ = make_client(handler)
    with pytest.raises(Mem0ApiError, match="boom"):
        client.add(messages=[{"role": "user", "content": "x"}], user_id="alice", poll_interval=0.0)


def test_add_async_poll_budget_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/memories/add/":
            return httpx.Response(200, json={"event_id": "evt-3", "status": "PENDING"})
        return httpx.Response(200, json={"id": "evt-3", "status": "RUNNING"})

    client, _ = make_client(handler)
    with pytest.raises(Mem0ApiError, match="still RUNNING after"):
        client.add(messages=[{"role": "user", "content": "x"}], user_id="alice", poll_budget=0.05, poll_interval=0.01)


def test_add_without_results_or_event_id_raises():
    client, _ = make_client(lambda r: httpx.Response(200, json={"message": "queued"}))
    with pytest.raises(Mem0ApiError, match="neither results nor event_id"):
        client.add(messages=[{"role": "user", "content": "x"}], user_id="alice")


def test_add_sync_empty_results_is_a_legitimate_no_op():
    """The store convention: a present-but-empty results list is a real no-op
    extraction, not drift — platform parity with the server/library stores
    (only a MISSING or non-list results raises)."""
    client, requests = make_client(lambda r: httpx.Response(200, json={"results": []}))
    assert client.add(messages=[{"role": "user", "content": "x"}], user_id="alice") == []
    assert len(requests) == 1  # no event to poll


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"results": "not-a-list"}, "a bare string"],
    ids=["null-payload", "payload-without-results", "non-list-results", "non-dict-payload"],
)
def test_add_async_succeeded_with_drifted_payload_fails_closed(payload):
    """A SUCCEEDED event whose payload is not the results envelope is drift,
    never "stored nothing": coercing it to [] would clear the backend's
    retained batch and silently lose the messages — the sync path fails
    closed for the same drift, and so must the poll terminal where every
    async add actually finishes."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/memories/add/":
            return httpx.Response(200, json={"event_id": "evt-4", "status": "PENDING"})
        return httpx.Response(200, json={"id": "evt-4", "status": "SUCCEEDED", "payload": payload})

    client, _ = make_client(handler)
    with pytest.raises(Mem0ApiError) as excinfo:
        client.add(messages=[{"role": "user", "content": "x"}], user_id="alice", poll_interval=0.0)
    assert excinfo.value.status_code == 502
    assert "unrecognizable" in excinfo.value.reason


def test_add_async_succeeded_with_empty_results_returns_no_op():
    """An empty results LIST on a SUCCEEDED event is a legitimate no-op
    extraction — [] must flow through, not raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/memories/add/":
            return httpx.Response(200, json={"event_id": "evt-5", "status": "PENDING"})
        return httpx.Response(200, json={"id": "evt-5", "status": "SUCCEEDED", "payload": {"results": []}})

    client, _ = make_client(handler)
    assert client.add(messages=[{"role": "user", "content": "x"}], user_id="alice", poll_interval=0.0) == []


def test_add_async_with_event_id_still_polls_despite_empty_sync_results():
    """An empty results list WITH an event_id keeps polling: the queued
    event's receipts are the authoritative answer there, and answering []
    from the inline list would lose every async extraction."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/memories/add/":
            return httpx.Response(200, json={"results": [], "event_id": "evt-6"})
        return httpx.Response(
            200, json={"id": "evt-6", "status": "SUCCEEDED", "payload": {"results": [{"id": "id-6", "event": "ADD"}]}}
        )

    client, requests = make_client(handler)
    results = client.add(messages=[{"role": "user", "content": "x"}], user_id="alice", poll_interval=0.0)
    assert [item["id"] for item in results] == ["id-6"]
    assert [str(r.url) for r in requests] == [
        "https://api.mem0.ai/v3/memories/add/",
        "https://api.mem0.ai/v1/event/evt-6/",
    ]


def test_search_nests_user_in_filters_and_maps_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/memories/search/"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "r1",
                        "memory": "likes cricket",
                        "user_id": "alice",
                        "score": 0.82,
                        "metadata": {"category": "hobbies"},
                        "created_at": "2024-07-26T10:29:36-07:00",
                        "updated_at": None,
                    }
                ]
            },
        )

    client, requests = make_client(handler)
    hits = client.search(query="hobbies", user_id="alice", top_k=5, threshold=0.0)
    assert hits[0]["id"] == "r1"
    assert hits[0]["memory"] == "likes cricket"
    sent = body_of(requests[0])
    assert sent == {"query": "hobbies", "filters": {"user_id": "alice"}, "top_k": 5, "threshold": 0.0}


def test_search_always_sends_threshold_explicitly():
    """The platform's server-side threshold default drifts across API versions
    (0.3 on v2, 0.1 on v3), so an omitted threshold is a silent relevance
    cutoff: the client always sends it (0.0 disables the cutoff)."""
    client, requests = make_client(lambda r: httpx.Response(200, json={"results": []}))
    assert client.search(query="q", user_id="alice", top_k=3) == []
    assert body_of(requests[0])["threshold"] == 0.0


def test_search_fails_closed_on_a_shapeless_200():
    """A 200 that is not the results envelope is drift, not "no memories":
    coercing it to [] would let the recall path cache a fabricated empty
    answer as authoritative (silent blindness, no counter moving)."""
    client, _ = make_client(lambda r: httpx.Response(200, json={"message": "ok"}))
    with pytest.raises(Mem0ApiError) as excinfo:
        client.search(query="q", user_id="alice", top_k=3)
    assert excinfo.value.status_code == 502
    assert "message" in excinfo.value.reason  # the drifted body's keys, never its content

    client, _ = make_client(lambda r: httpx.Response(200, content=b"null"))
    with pytest.raises(Mem0ApiError) as excinfo:
        client.search(query="q", user_id="alice", top_k=3)
    assert excinfo.value.status_code == 502


def test_get_all_posts_filters_with_pagination_params():
    client, requests = make_client(lambda r: httpx.Response(200, json={"count": 0, "results": []}))
    client.get_all(user_id="alice", page_size=50)
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v3/memories/"
    assert dict(request.url.params) == {"page": "1", "page_size": "50"}
    assert body_of(request) == {"filters": {"user_id": "alice"}}

    client.get_all(user_id="alice", page_size=50, page=3)
    assert dict(requests[1].url.params) == {"page": "3", "page_size": "50"}


def test_update_and_delete_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, json={"id": "r1", "memory": "new text", "user_id": "alice"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"message": "Memory deleted successfully!"})
        return httpx.Response(404, json={"error": "Memory not found!"})

    client, requests = make_client(handler)
    assert client.update("r1", text="new text")["memory"] == "new text"
    assert client.delete("r1") == {"message": "Memory deleted successfully!"}
    assert [str(r.url) for r in requests] == [
        "https://api.mem0.ai/v1/memories/r1/",
        "https://api.mem0.ai/v1/memories/r1/",
    ]


def test_get_fails_closed_on_a_drifted_200():
    """A drifted 200 must not read as an empty row: the endpoint adapter's
    ownership check would misreport the drift as a plain 404."""
    client, _ = make_client(lambda r: httpx.Response(200, json=["not", "a", "row"]))
    with pytest.raises(Mem0ApiError) as excinfo:
        client.get("r1")
    assert excinfo.value.status_code == 502


@pytest.mark.parametrize(
    "status, body",
    [
        (401, {"error": "Invalid API key"}),
        (404, {"error": "Memory not found!"}),
        (400, ["One of the filters: app_id, user_id, agent_id, run_id is required!"]),
        (500, "not-json"),
    ],
)
def test_error_reason_extraction(status, body):
    client, _ = make_client(lambda r: httpx.Response(status, json=body))
    with pytest.raises(Mem0ApiError) as excinfo:
        client.ping()
    assert excinfo.value.status_code == status
    if status == 400:
        assert "filters" in excinfo.value.reason
    elif status == 500:
        assert "HTTP 500" in excinfo.value.reason
    else:
        assert excinfo.value.reason


def test_add_custom_instructions_sent_only_when_non_empty():
    """The advisory guidelines field rides the add request exactly when it
    carries text (stripped); whitespace-only and absent values keep the
    request body byte-identical to the pre-field form."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/memories/add/"
        return httpx.Response(200, json={"results": [{"id": "id-1", "memory": "fact", "event": "ADD"}]})

    client, requests = make_client(handler)
    client.add(
        messages=[{"role": "user", "content": "hello"}],
        user_id="alice",
        custom_instructions="  prefer operational facts  ",
    )
    assert body_of(requests[0])["custom_instructions"] == "prefer operational facts"
    client.add(messages=[{"role": "user", "content": "again"}], user_id="alice", custom_instructions="   ")
    assert "custom_instructions" not in body_of(requests[1])
    client.add(messages=[{"role": "user", "content": "third"}], user_id="alice")
    assert "custom_instructions" not in body_of(requests[2])

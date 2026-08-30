"""Standardized memory endpoint contract: model validation plus a full HTTP
round-trip through shared_bridge.serve against a fake in-memory endpoint
(real local HTTP, stdlib only, fully offline)."""

import json
import threading
import urllib.error
import urllib.request

import pytest
from pydantic import ValidationError

from shared_bridge.endpoint import (
    AddRequest,
    AddResponse,
    DeleteResponse,
    MemoryEndpoint,
    MemoryEndpointError,
    MemoryRecord,
    SearchRequest,
    SearchResponse,
    UpdateRequest,
    UpdateResponse,
)
from shared_bridge.serve import make_server, serve_in_thread


class FakeEndpoint(MemoryEndpoint):
    """In-memory MemoryEndpoint honoring the contract rules (sync writes,
    user_id isolation, 404 on unknown ids)."""

    def __init__(self):
        self.store: dict[str, MemoryRecord] = {}
        self._next = 0

    def add(self, request: AddRequest) -> AddResponse:
        memory_ids = []
        for message in request.messages:
            self._next += 1
            record_id = str(self._next)
            self.store[record_id] = MemoryRecord(id=record_id, content=message.content, user_id=request.user_id)
            memory_ids.append(record_id)
        return AddResponse(
            success=True,
            request_id=request.request_id,
            user_id=request.user_id,
            session_id=request.session_id,
            memory_ids=memory_ids,
        )

    def search(self, request: SearchRequest) -> SearchResponse:
        hits = [
            record
            for record in self.store.values()
            if record.user_id == request.user_id and request.query.lower() in record.content.lower()
        ]
        return SearchResponse(data=hits[: request.top_k])

    def update(self, memory_id: str, request: UpdateRequest, *, user_id: str | None = None) -> UpdateResponse:
        record = self.store.get(memory_id)
        if record is None:
            raise MemoryEndpointError(404, f"memory not found: {memory_id}")
        if request.text is not None:
            record.content = request.text
        return UpdateResponse(success=True, memory=record)

    def delete(self, memory_id: str, *, user_id: str | None = None) -> DeleteResponse:
        if self.store.pop(memory_id, None) is None:
            raise MemoryEndpointError(404, f"memory not found: {memory_id}")
        return DeleteResponse(success=True, memory_id=memory_id)


@pytest.fixture
def base_url():
    server = make_server(FakeEndpoint(), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _request(base: str, method: str, path: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# Contract model validation
# ---------------------------------------------------------------------------
def test_add_request_requires_messages_and_user_id():
    with pytest.raises(ValidationError):
        AddRequest(user_id="u")
    with pytest.raises(ValidationError):
        AddRequest(messages=[{"role": "user", "content": "x"}])
    with pytest.raises(ValidationError):  # empty message content
        AddRequest(messages=[{"role": "user", "content": ""}], user_id="u")
    # request_id defaults to a unique id when omitted
    assert AddRequest(messages=[{"role": "user", "content": "x"}], user_id="u").request_id


def test_search_request_requires_query_and_caps_top_k():
    with pytest.raises(ValidationError):
        SearchRequest(user_id="u")
    with pytest.raises(ValidationError):
        SearchRequest(query="", user_id="u")
    with pytest.raises(ValidationError):
        SearchRequest(query="q", user_id="u", top_k=0)
    assert SearchRequest(query="q", user_id="u").top_k == 10


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------
def test_http_add_search_update_delete_round_trip(base_url):
    status, body = _request(base_url, "GET", "/health")
    assert status == 200 and body == {"status": "ok"}

    add_payload = {
        "request_id": "req-1",
        "messages": [{"role": "user", "content": "remember the rolling fix", "timestamp": 1704067200000}],
        "user_id": "alice",
        "session_id": "s1",
    }
    status, body = _request(base_url, "POST", "/v1/memories/", add_payload)
    assert status == 200
    # Synchronous contract: ids echoed byte-for-byte, success only after persist.
    assert body["success"] is True
    assert body["request_id"] == "req-1" and body["user_id"] == "alice" and body["session_id"] == "s1"
    (memory_id,) = body["memory_ids"]

    status, body = _request(base_url, "POST", "/v1/memories/search/", {"query": "rolling", "user_id": "alice", "top_k": 10})
    assert status == 200
    assert [hit["id"] for hit in body["data"]] == [memory_id]
    assert body["data"][0]["content"] == "remember the rolling fix"

    # user_id is the sole retrieval-isolation boundary.
    status, body = _request(base_url, "POST", "/v1/memories/search/", {"query": "rolling", "user_id": "bob"})
    assert status == 200 and body["data"] == []

    status, body = _request(base_url, "PUT", f"/v1/memories/{memory_id}", {"text": "remember the sliding fix"})
    assert status == 200 and body["success"] is True and body["memory"]["content"] == "remember the sliding fix"

    status, body = _request(base_url, "DELETE", f"/v1/memories/{memory_id}")
    assert status == 200 and body == {"success": True, "memory_id": memory_id}
    status, body = _request(base_url, "DELETE", f"/v1/memories/{memory_id}")
    assert status == 404 and "reason" in body["detail"]


def test_http_error_shapes(base_url):
    # Malformed JSON body -> 400 {"detail": {"reason": ...}}
    request = urllib.request.Request(base_url + "/v1/memories/search/", data=b"<not json", method="POST")
    try:
        urllib.request.urlopen(request)
        raise AssertionError("expected HTTPError")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert "reason" in json.loads(e.read())["detail"]

    # Schema-valid JSON failing validation -> 400
    status, _ = _request(base_url, "POST", "/v1/memories/search/", {"user_id": "u"})
    assert status == 400
    # Unknown id -> 404; unknown route -> 404
    status, _ = _request(base_url, "PUT", "/v1/memories/999", {"text": "x"})
    assert status == 404
    status, _ = _request(base_url, "POST", "/v1/nope", {})
    assert status == 404


def test_http_rejects_negative_content_length(base_url):
    """Content-Length: -1 means "read to EOF" for rfile.read — a held-open
    socket would block the single serving thread forever. Rejected with 400
    before any read."""
    import http.client

    port = int(base_url.rsplit(":", 1)[1])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/v1/memories/search/")
    conn.putheader("Content-Length", "-1")
    conn.endheaders(b"{}")
    response = conn.getresponse()
    assert response.status == 400
    response.read()
    conn.close()


def test_http_memory_id_is_percent_decoded(base_url):
    """The {id} segment is URL-decoded before dispatch: %31 is "1", and an
    encoded slash decodes to a path separator — not an id segment (404)."""
    status, body = _request(
        base_url,
        "POST",
        "/v1/memories/",
        {"messages": [{"role": "user", "content": "encoded id test"}], "user_id": "alice"},
    )
    assert status == 200
    (memory_id,) = body["memory_ids"]
    assert memory_id == "1"
    status, body = _request(base_url, "PUT", "/v1/memories/%31", {"text": "encoded id updated"})
    assert status == 200 and body["memory"]["content"] == "encoded id updated"
    status, _ = _request(base_url, "DELETE", "/v1/memories/1%2F2")
    assert status == 404


def test_serve_in_thread_reraises_the_real_startup_error(base_url):
    # The port is already bound by the base_url fixture server: the underlying
    # OSError must surface immediately, not a 5s wait and a generic message.
    port = int(base_url.rsplit(":", 1)[1])
    with pytest.raises(OSError):
        serve_in_thread(FakeEndpoint, "127.0.0.1", port)

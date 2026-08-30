"""CureMemoryEndpoint: the standardized add/search/update/delete contract over
a real CUREMemorySystem on a temp SQLite store (fully offline)."""

import json
import urllib.error
import urllib.request

import pytest

from conftest import ScriptedDecisionClient, approved_candidate

from shared_bridge.endpoint import AddRequest, MemoryEndpointError, SearchRequest, UpdateRequest
from shared_bridge.serve import serve_in_thread

from cure_memory.system import CUREMemorySystem
from cure_memory_bridge.endpoint import CureMemoryEndpoint


@pytest.fixture
def endpoint_and_client(tmp_path):
    client = ScriptedDecisionClient()
    system = CUREMemorySystem(str(tmp_path / "endpoint.sqlite3"), llm_client=client)
    yield CureMemoryEndpoint(system), client
    system.close()


def _add(endpoint, contents, user_id="alice", session_id="s1", request_id="req-1", infer=False):
    return endpoint.add(
        AddRequest(
            request_id=request_id,
            messages=[{"role": "user", "content": content} for content in contents],
            user_id=user_id,
            session_id=session_id,
            infer=infer,
        )
    )


def test_add_verbatim_echoes_ids_and_is_immediately_searchable(endpoint_and_client):
    endpoint, _ = endpoint_and_client
    response = _add(endpoint, ["remember the rolling fix"])
    # Synchronous contract: success only after persistence, ids echoed byte-for-byte.
    assert response.success is True
    assert (response.request_id, response.user_id, response.session_id) == ("req-1", "alice", "s1")
    assert len(response.memory_ids) == 1

    hits = endpoint.search(SearchRequest(query="rolling", user_id="alice")).data
    assert [hit.id for hit in hits] == response.memory_ids
    assert "remember the rolling fix" in hits[0].content
    assert hits[0].created_at

    # user_id is the sole retrieval-isolation boundary.
    assert endpoint.search(SearchRequest(query="rolling", user_id="bob")).data == []


def test_verbatim_add_retry_reports_the_existing_row(endpoint_and_client):
    """A retried infer=false add (same request_id, same content) dedupes
    against the row the first attempt already wrote and reports that row's
    id — never an empty id list for a persisted memory."""
    endpoint, _ = endpoint_and_client
    first = _add(endpoint, ["remember the rolling fix"])
    retry = _add(endpoint, ["remember the rolling fix"])
    assert retry.memory_ids == first.memory_ids
    rows = endpoint._system.store.list_memories("alice", review_status=None)
    assert len(rows) == 1  # the retry wrote nothing new


def test_add_with_infer_runs_the_extraction_pipeline(endpoint_and_client):
    endpoint, client = endpoint_and_client
    client.queue.append({"candidates": [approved_candidate(1, "k1", "extracted fact")], "deletions": [], "rejections": []})
    response = _add(endpoint, ["please remember the extracted fact"], infer=True)
    assert response.success is True and response.memory_ids
    hits = endpoint.search(SearchRequest(query="extracted", user_id="alice")).data
    assert [hit.id for hit in hits] == response.memory_ids
    assert "extracted fact" in hits[0].content


def test_add_with_infer_extraction_failure_is_a_500(endpoint_and_client):
    """Synchronous-writes contract: a failed extraction (e.g. the decision
    model returned empty content) persisted no memory, so the add cannot
    succeed — a 500, never a 200 with zero ids (which would read as a
    legitimate "nothing worth memorizing")."""
    endpoint, client = endpoint_and_client
    client.queue.append("empty_content")  # scripted last_error on the decision client
    with pytest.raises(MemoryEndpointError) as excinfo:
        _add(endpoint, ["remember the rolling fix"], infer=True)
    assert excinfo.value.status_code == 500
    assert endpoint.search(SearchRequest(query="rolling", user_id="alice")).data == []


def test_add_with_infer_dedupe_reports_the_existing_row(endpoint_and_client):
    """An infer=true add whose candidate dedupes (identical-content no-op)
    writes nothing new — and reports the EXISTING row's id, never an empty id
    list for a persisted memory (the verbatim retry path's convention)."""
    endpoint, client = endpoint_and_client
    decision = {"candidates": [approved_candidate(1, "k1", "extracted fact")], "deletions": [], "rejections": []}
    client.queue.append(decision)
    first = _add(endpoint, ["please remember the extracted fact"], infer=True)
    client.queue.append(decision)
    retry = _add(endpoint, ["please remember the extracted fact"], infer=True)
    assert first.memory_ids and retry.memory_ids == first.memory_ids
    rows = endpoint._system.store.list_memories("alice", review_status=None)
    assert len(rows) == 1  # the retry wrote nothing new
    hits = endpoint.search(SearchRequest(query="extracted", user_id="alice")).data
    assert [hit.id for hit in hits] == retry.memory_ids


def test_shared_session_id_still_isolates_extraction_per_user(endpoint_and_client):
    """user_id stays the sole isolation boundary even when two users reuse one
    session id: B's extraction must not ingest A's un-extracted messages (the
    message listing is user-scoped), and B's extraction must not advance the
    checkpoint past A's tail (the checkpoint is keyed per (user_id, session_id)),
    so A's messages stay extractable for A afterwards."""
    endpoint, client = endpoint_and_client
    # Alice leaves an un-extracted tail under session "s": her extraction
    # fails (the 500 path), so the recorded messages stay past the checkpoint.
    client.queue.append("empty_content")  # scripted last_error on the decision client
    with pytest.raises(MemoryEndpointError):
        _add(endpoint, ["ALICE PRIVATE CONTEXT note"], user_id="alice", session_id="s", infer=True)
    # Bob adds under the SAME session id with infer=true: only his own message
    # may reach the extraction request.
    client.queue.append({"candidates": [], "deletions": [], "rejections": []})
    _add(endpoint, ["bob message"], user_id="bob", session_id="s", infer=True)
    (request,) = client.requests[1:]
    assert [message["content"] for message in request["messages"]] == ["bob message"]
    # Alice's tail was not swallowed by bob's checkpoint: her next extraction
    # still sees it, and sees none of bob's.
    client.queue.append({"candidates": [], "deletions": [], "rejections": []})
    _add(endpoint, ["alice follow-up"], user_id="alice", session_id="s", infer=True)
    contents = [message["content"] for message in client.requests[2]["messages"]]
    assert contents == ["ALICE PRIVATE CONTEXT note", "alice follow-up"]


def test_verbatim_add_with_metadata_answers_400(endpoint_and_client):
    """Verbatim rows carry no arbitrary metadata: silently dropping the
    request's metadata would claim a write not fully made (the update path's
    ground). infer=true honors metadata via the recorded messages."""
    endpoint, _ = endpoint_and_client
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.add(
            AddRequest(
                messages=[{"role": "user", "content": "remember the rolling fix"}],
                user_id="alice",
                session_id="s1",
                infer=False,
                metadata={"source": "pytest"},
            )
        )
    assert excinfo.value.status_code == 400
    assert endpoint._system.store.list_memories("alice", review_status=None) == []


def test_search_top_k_caps_the_results(endpoint_and_client):
    endpoint, _ = endpoint_and_client
    _add(endpoint, ["fix alpha", "fix beta", "fix gamma"])
    hits = endpoint.search(SearchRequest(query="fix", user_id="alice", top_k=2)).data
    assert len(hits) == 2


def test_update_replaces_text_and_supersedes_the_old_row(endpoint_and_client):
    endpoint, _ = endpoint_and_client
    (memory_id,) = _add(endpoint, ["old text"]).memory_ids
    response = endpoint.update(memory_id, UpdateRequest(text="new text"), user_id="alice")
    assert response.success is True and "new text" in response.memory.content

    # CURE replaces by supersede-and-insert: the new text lives on a new row id.
    new_id = response.memory.id
    assert new_id != memory_id
    assert endpoint.search(SearchRequest(query="new", user_id="alice")).data[0].id == new_id
    assert endpoint.search(SearchRequest(query="old", user_id="alice")).data == []
    rows = endpoint._system.store.list_memories("alice", review_status=None)
    assert sorted(row.review_status for row in rows) == ["approved", "superseded"]


def test_update_and_delete_unknown_or_invalid_ids(endpoint_and_client):
    endpoint, _ = endpoint_and_client
    (memory_id,) = _add(endpoint, ["some text"]).memory_ids
    with pytest.raises(MemoryEndpointError) as missing:
        endpoint.update("999", UpdateRequest(text="x"), user_id="alice")
    assert missing.value.status_code == 404
    with pytest.raises(MemoryEndpointError) as invalid:
        endpoint.delete("not-an-id", user_id="alice")
    assert invalid.value.status_code == 400
    with pytest.raises(MemoryEndpointError) as no_text:
        endpoint.update(memory_id, UpdateRequest(), user_id="alice")
    assert no_text.value.status_code == 400


def test_update_with_metadata_answers_400(endpoint_and_client):
    """CURE rows carry no arbitrary metadata: a text+metadata update applied
    partially would silently drop the metadata half — 400 like the
    metadata-only case, and nothing is applied."""
    endpoint, _ = endpoint_and_client
    (memory_id,) = _add(endpoint, ["old text"]).memory_ids
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update(memory_id, UpdateRequest(text="new text", metadata={"k": "v"}), user_id="alice")
    assert excinfo.value.status_code == 400
    assert endpoint.search(SearchRequest(query="new", user_id="alice")).data == []
    assert endpoint.search(SearchRequest(query="old", user_id="alice")).data[0].id == memory_id


def test_delete_removes_the_row_from_search(endpoint_and_client):
    endpoint, _ = endpoint_and_client
    (memory_id,) = _add(endpoint, ["doomed fact"]).memory_ids
    response = endpoint.delete(memory_id, user_id="alice")
    assert response.success is True and response.memory_id == memory_id
    assert endpoint.search(SearchRequest(query="doomed", user_id="alice")).data == []
    with pytest.raises(MemoryEndpointError) as gone:
        endpoint.delete(memory_id, user_id="alice")
    assert gone.value.status_code == 404


def test_terminal_rows_are_never_rematched(endpoint_and_client):
    endpoint, _ = endpoint_and_client
    (memory_id,) = _add(endpoint, ["doomed fact"]).memory_ids
    endpoint.delete(memory_id, user_id="alice")
    # An update against the deleted row is a 404 — not a resurrection of the
    # deleted content as a fresh approved row, and the marker survives.
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update(memory_id, UpdateRequest(text="zombie"), user_id="alice")
    assert excinfo.value.status_code == 404
    rows = endpoint._system.store.list_memories("alice", review_status=None)
    assert [row.review_status for row in rows] == ["deleted"]
    assert endpoint.search(SearchRequest(query="doomed", user_id="alice")).data == []

    # A superseded row is terminal too: delete must not rewrite its marker.
    (old_id,) = _add(endpoint, ["old text"], request_id="req-2").memory_ids
    new_id = endpoint.update(old_id, UpdateRequest(text="new text"), user_id="alice").memory.id
    with pytest.raises(MemoryEndpointError) as superseded:
        endpoint.delete(old_id, user_id="alice")
    assert superseded.value.status_code == 404
    by_id = {row.id: row.review_status for row in endpoint._system.store.list_memories("alice", review_status=None)}
    assert by_id[int(old_id)] == "superseded"
    assert by_id[int(new_id)] == "approved"


def test_http_round_trip_via_serve_in_thread(tmp_path):
    """The sqlite store is thread-affine: serve_in_thread both constructs and
    uses the endpoint on the one serving thread."""
    db_path = str(tmp_path / "http.sqlite3")
    server = serve_in_thread(
        lambda: CureMemoryEndpoint(CUREMemorySystem(db_path), default_user_id="alice"), "127.0.0.1", 0
    )
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        def call(method, path, payload=None):
            data = None if payload is None else json.dumps(payload).encode()
            request = urllib.request.Request(base + path, data=data, method=method)
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        assert call("GET", "/health")[0] == 200
        status, body = call(
            "POST",
            "/v1/memories/",
            {
                "request_id": "req-http",
                "messages": [{"role": "user", "content": "remember the rolling fix"}],
                "user_id": "alice",
                "session_id": "s1",
                "infer": False,
            },
        )
        assert status == 200 and body["success"] is True and body["request_id"] == "req-http"
        (memory_id,) = body["memory_ids"]

        status, body = call("POST", "/v1/memories/search/", {"query": "rolling", "user_id": "alice"})
        assert status == 200 and [hit["id"] for hit in body["data"]] == [memory_id]

        status, body = call("PUT", f"/v1/memories/{memory_id}", {"text": "remember the sliding fix"})
        assert status == 200 and body["success"] is True
        new_id = body["memory"]["id"]

        status, body = call("DELETE", f"/v1/memories/{new_id}")
        assert status == 200 and body["success"] is True
        status, body = call("POST", "/v1/memories/search/", {"query": "sliding", "user_id": "alice"})
        assert status == 200 and body["data"] == []
    finally:
        server.shutdown()

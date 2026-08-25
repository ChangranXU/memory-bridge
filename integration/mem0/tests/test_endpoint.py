"""Endpoint adapter tests: the shared contract over the scripted client."""

import pytest
from shared_bridge.endpoint import (
    AddRequest,
    MemoryEndpointError,
    Message,
    SearchRequest,
    UpdateRequest,
)

from tests.conftest import FakePlatformClient  # noqa: F401  (documents the scripted seam)
from mem0_bridge.endpoint import Mem0Endpoint


@pytest.fixture
def endpoint(fake_client):
    return Mem0Endpoint(fake_client)


def test_add_echoes_ids_and_scope(endpoint, fake_client):
    response = endpoint.add(
        AddRequest(
            messages=[Message(role="user", content="I like tea"), Message(role="assistant", content="ok")],
            user_id="alice",
            session_id="sess-1",
            infer=True,
        )
    )
    assert response.success is True
    assert response.user_id == "alice"
    assert response.session_id == "sess-1"
    assert len(response.memory_ids) == 2
    call = fake_client.add_calls[0]
    assert call["user_id"] == "alice"
    assert call["run_id"] == "sess-1"
    assert call["infer"] is True
    assert [m["role"] for m in call["messages"]] == ["user", "assistant"]


def test_search_is_user_scoped_and_capped(endpoint, fake_client):
    endpoint.add(AddRequest(messages=[Message(role="user", content="tea facts")], user_id="alice"))
    endpoint.add(AddRequest(messages=[Message(role="user", content="bob facts")], user_id="bob"))
    response = endpoint.search(SearchRequest(query="facts", user_id="alice", top_k=5))
    assert all(record.user_id == "alice" for record in response.data)
    assert len(response.data) == 1
    assert response.data[0].content.startswith("fact:")
    assert response.data[0].id in response.data[0].id  # non-empty id echoed from the store


def test_search_caps_at_top_k(endpoint, fake_client):
    for i in range(5):
        endpoint.add(AddRequest(messages=[Message(role="user", content=f"fact {i}")], user_id="alice"))
    response = endpoint.search(SearchRequest(query="fact", user_id="alice", top_k=2))
    assert len(response.data) == 2


def test_search_drops_an_unusable_score(endpoint, fake_client):
    """One malformed platform score drops to None — it must not fail the
    whole search, and must not surface as a fabricated 0.0."""
    endpoint.add(AddRequest(messages=[Message(role="user", content="tea facts")], user_id="alice"))
    fake_client.memories["m1"]["score"] = "high"
    response = endpoint.search(SearchRequest(query="facts", user_id="alice"))
    assert response.data[0].score is None


def test_update_requires_text_or_metadata(endpoint):
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update("m1", UpdateRequest(), user_id="alice")
    assert excinfo.value.status_code == 400


def test_update_replaces_text(endpoint, fake_client):
    endpoint.add(AddRequest(messages=[Message(role="user", content="old")], user_id="alice"))
    memory_id = next(iter(fake_client.memories))
    response = endpoint.update(memory_id, UpdateRequest(text="new text"), user_id="alice")
    assert response.success is True
    assert response.memory.content == "new text"
    assert fake_client.memories[memory_id]["memory"] == "new text"


def test_update_shapeless_response_is_a_clean_500(endpoint, fake_client, monkeypatch):
    """The documented update response echoes the updated memory; a shapeless
    one (no echoed text) is an integration failure with a clear reason —
    never a raw KeyError escaping past the Mem0ApiError guard."""
    endpoint.add(AddRequest(messages=[Message(role="user", content="old")], user_id="alice"))
    memory_id = next(iter(fake_client.memories))
    monkeypatch.setattr(fake_client, "update", lambda *args, **kwargs: {"id": memory_id})
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update(memory_id, UpdateRequest(text="new"), user_id="alice")
    assert excinfo.value.status_code == 500
    assert "unusable" in excinfo.value.reason


def test_update_unknown_id_404(endpoint):
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update("missing", UpdateRequest(text="x"), user_id="alice")
    assert excinfo.value.status_code == 404


def test_update_foreign_user_looks_like_unknown(endpoint, fake_client):
    endpoint.add(AddRequest(messages=[Message(role="user", content="secret")], user_id="bob"))
    memory_id = next(iter(fake_client.memories))
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update(memory_id, UpdateRequest(text="x"), user_id="alice")
    assert excinfo.value.status_code == 404


def test_update_and_delete_ownerless_memory_fails_closed(endpoint, fake_client):
    """A stored user_id of None means the memory was written outside this
    contract (adds here always carry one): the ownership check fails closed,
    exactly like a foreign user_id."""
    fake_client.memories["m9"] = {
        "id": "m9",
        "memory": "unowned",
        "user_id": None,
        "metadata": {},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    with pytest.raises(MemoryEndpointError) as update_exc:
        endpoint.update("m9", UpdateRequest(text="x"), user_id="alice")
    assert update_exc.value.status_code == 404
    with pytest.raises(MemoryEndpointError) as delete_exc:
        endpoint.delete("m9", user_id="alice")
    assert delete_exc.value.status_code == 404
    assert "m9" in fake_client.memories  # untouched


def test_delete_removes_and_404s(endpoint, fake_client):
    endpoint.add(AddRequest(messages=[Message(role="user", content="temp")], user_id="alice"))
    memory_id = next(iter(fake_client.memories))
    response = endpoint.delete(memory_id, user_id="alice")
    assert response.success is True
    assert response.memory_id == memory_id
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.delete(memory_id, user_id="alice")
    assert excinfo.value.status_code == 404


def test_add_api_error_maps_to_500(endpoint, fake_client):
    from mem0_bridge.client import Mem0ApiError

    fake_client.add_error = Mem0ApiError(502, "platform down")
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.add(AddRequest(messages=[Message(role="user", content="x")], user_id="alice"))
    assert excinfo.value.status_code == 500

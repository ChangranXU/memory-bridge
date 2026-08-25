"""Standardized-endpoint adapter tests (fake gateway client)."""

import pytest

from shared_bridge.endpoint import (
    AddRequest,
    MemoryEndpointError,
    Message,
    SearchRequest,
    UpdateRequest,
)
from tencentdb.tests.fake_gateway import FakeGatewayClient
from tencentdb_bridge.client import TencentDBApiError
from tencentdb_bridge.endpoint import TencentDBEndpoint



@pytest.fixture
def client():
    return FakeGatewayClient()


@pytest.fixture
def endpoint(client):
    return TencentDBEndpoint(client, "minisweagent")


def _add(messages=None, user_id="user-1", infer=True):
    return AddRequest(
        messages=messages or [Message(role="user", content="pytest x fails on merge")], user_id=user_id, infer=infer
    )


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------
def test_add_infer_flow_fresh_session_and_memory_ids(endpoint, client):
    response = endpoint.add(_add())
    assert response.success is True
    assert response.user_id == "user-1"
    assert response.session_id  # fresh uuid4 per add, not the caller's
    assert response.memory_ids == [row["id"] for row in client.rows]
    add_call = client.add_calls[0]
    assert add_call["user_id"] == "user-1"
    assert add_call["session_id"] == response.session_id
    # The minted session rides as the add's task tag, and the resolve query
    # filters on it: memory_ids names exactly this add's rows.
    assert add_call["task_id"] == response.session_id
    assert client.query_calls[0]["task_id"] == response.session_id
    assert client.drain_calls == 1  # one cycle covers a small add: a single drain, no idle wait


def test_add_mints_a_fresh_session_over_the_callers(endpoint, client):
    # The one deliberate deviation from the contract's echo rule (the module
    # docstring): the response carries the MINTED session id, not the
    # request's — a reused session could fall sub-threshold and leave the
    # add's memories unsearchable behind an armed idle timer.
    from uuid import UUID

    request = AddRequest(
        messages=[Message(role="user", content="pytest x fails on merge")], user_id="user-1", session_id="caller-s1"
    )
    response = endpoint.add(request)
    assert response.session_id != "caller-s1"
    UUID(response.session_id)  # a real uuid4, not a derived string
    assert client.add_calls[0]["session_id"] == response.session_id


def test_add_over_one_cycle_waits_out_the_idle_timer(endpoint, client, monkeypatch):
    # >10 messages exceed one L1 cycle (L1_BATCH_PROCESS): a 1-9-row tail's
    # only landing mechanism is the status-invisible idle timer, so the drain
    # waits it out like the arm's finalize — a bare wait_l1_idle would return
    # memory_ids missing the tail ("success before searchable").
    sleeps: list[float] = []
    monkeypatch.setattr(TencentDBEndpoint, "_sleep", lambda self, seconds: sleeps.append(seconds))
    messages = [Message(role="user", content=f"fact number {i}") for i in range(11)]
    response = endpoint.add(_add(messages=messages))
    assert response.success is True
    assert len(response.memory_ids) == 11
    assert client.drain_calls == 2  # the threshold cycles, then the timer-fired tail
    assert client.drain_budgets == [300.0, 300.0]  # the tail gets a fresh per-wait budget
    assert sleeps == [35.0]  # l1_idle_timeout (30) + margin (5)


def test_add_over_one_cycle_tail_settle_failure_maps_500(endpoint, client, monkeypatch):
    monkeypatch.setattr(TencentDBEndpoint, "_sleep", lambda self, seconds: None)
    client.idle_answers = [True, False]  # cycles settle, the timer-fired tail never does
    messages = [Message(role="user", content=f"fact number {i}") for i in range(11)]
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.add(_add(messages=messages))
    assert excinfo.value.status_code == 500
    assert "tail" in excinfo.value.reason


def test_add_l1_settle_failure_maps_500(endpoint, client):
    # The drain reporting not-idle inside the budget must surface as a 500 —
    # never as a success with unsearchable memories.
    client.idle_answers = [False]
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.add(_add())
    assert excinfo.value.status_code == 500
    assert "did not settle" in excinfo.value.reason


def test_add_rejects_non_wire_roles(endpoint, client):
    # The conversation schema's role enum is user/assistant: anything else is
    # a caller bug answered with the contract's 400, never relayed as a 500 —
    # and rejected BEFORE any gateway write.
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.add(
            AddRequest(
                messages=[Message(role="system", content="policy"), Message(role="user", content="x")], user_id="user-1"
            )
        )
    assert excinfo.value.status_code == 400
    assert "system" in excinfo.value.reason
    assert client.add_calls == []


def test_add_without_a_user_round_answers_400(endpoint, client):
    # The gateway notifies the pipeline only on role=="user" rounds and the
    # per-add session is unique: an all-assistant add would persist L0 yet
    # NEVER become searchable — not the honest-empty case.
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.add(AddRequest(messages=[Message(role="assistant", content="monologue")], user_id="user-1"))
    assert excinfo.value.status_code == 400
    assert "user round" in excinfo.value.reason
    assert client.add_calls == []


def test_add_rejects_content_over_the_wire_cap(endpoint, client):
    # The gateway's per-message content cap (8192, zod) is the role
    # pre-validation's class: a contract-legal request the gateway must
    # reject is a caller bug answered 400 — before any write, never a 500.
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.add(_add(messages=[Message(role="user", content="x" * 8193)]))
    assert excinfo.value.status_code == 400
    assert "8192" in excinfo.value.reason
    assert client.add_calls == []


def test_add_cap_counts_utf16_units(endpoint, client):
    # The zod cap counts JavaScript's String.length — UTF-16 code units:
    # 5000 astral chars are 5000 Python code points but 10000 wire units, so
    # a code-point check would let the gateway's 400 through (relayed as a
    # 500 after a partial write) — the pre-validation measures the same unit.
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.add(_add(messages=[Message(role="user", content="😀" * 5000)]))
    assert excinfo.value.status_code == 400
    assert client.add_calls == []


def test_add_does_not_claim_concurrent_adds_rows(endpoint, client):
    """Two overlapping adds for one user_id (the contract's only isolation
    boundary, which does not serialize callers) must not claim each other's
    rows: the watermark window is user-wide, so only the per-add task tag
    keeps request B's memory_ids free of request A's rows."""
    first = endpoint.add(_add(messages=[Message(role="user", content="first fact")]))
    second = endpoint.add(_add(messages=[Message(role="user", content="second fact")]))
    first_ids = {row["id"] for row in client.rows if row.get("task_id") == first.session_id}
    second_ids = {row["id"] for row in client.rows if row.get("task_id") == second.session_id}
    assert first_ids and second_ids
    assert set(first.memory_ids) == first_ids
    assert set(second.memory_ids) == second_ids
    assert not set(first.memory_ids) & set(second.memory_ids)


def test_add_does_not_claim_untagged_rows(endpoint, client):
    # Rows written outside the endpoint (no task tag) never match the add's
    # task-scoped resolve, even when they land inside its watermark window
    # (fresh updated_at below).
    from tencentdb_bridge.client import utc_now_iso

    client.rows.append({"id": "foreign", "content": "other caller's row", "updated_at": utc_now_iso()})
    response = endpoint.add(_add())
    assert "foreign" not in response.memory_ids


def test_add_with_empty_extraction_returns_empty_memory_ids(endpoint, client):
    client.auto_produce = False
    response = endpoint.add(_add())
    assert response.memory_ids == []  # honest: the extractor produced nothing


def test_add_infer_false_answers_400(endpoint):
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.add(_add(infer=False))
    assert excinfo.value.status_code == 400
    assert "verbatim" in excinfo.value.reason


def test_add_gateway_error_maps_500(endpoint, client):
    client.add_error = TencentDBApiError(4291, "quota exceeded")
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.add(_add())
    assert excinfo.value.status_code == 500


def test_add_uses_dedicated_add_timeout(client):
    # The add's own timeout mirrors TencentDBConfig.add_timeout, not the
    # drain budget: a slow embedding provider can stretch one add past the
    # drain budget while the gateway still completes the write.
    endpoint = TencentDBEndpoint(client, "minisweagent", drain_budget=111.0, add_timeout=222.0)
    endpoint.add(_add())
    assert client.add_calls[0]["timeout"] == 222.0


def test_add_wraps_transport_errors(client):
    # Transport-level failures (wrapped by the client) map to the contract's
    # 500 like any gateway error — never a raw httpx exception over the wire.
    client.add_error = TencentDBApiError(503, "gateway transport error: timed out")
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint_transport = TencentDBEndpoint(client, "minisweagent")
        endpoint_transport.add(_add())
    assert excinfo.value.status_code == 500
    assert "transport" in excinfo.value.reason


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def test_search_maps_rows_with_scores(endpoint, client):
    client.search_hits = [
        {"id": "a1", "content": "fact one", "score": 0.03, "user_id": "user-1", "created_at": "2026-01-01T00:00:00Z"},
        {"id": "a2", "content": "fact two", "score": "bad"},
        {"id": "", "content": "dropped"},
    ]
    response = endpoint.search(SearchRequest(query="pytest", user_id="user-1", top_k=5))
    assert len(response.data) == 2
    assert response.data[0].id == "a1"
    assert response.data[0].score == 0.03
    assert response.data[0].created_at == "2026-01-01T00:00:00Z"
    assert response.data[1].score is None
    assert client.search_calls[0]["task_id"] is None  # user-wide, no repo narrowing


def test_search_caps_at_top_k(endpoint, client):
    client.search_hits = [{"id": f"a{i}", "content": "c"} for i in range(10)]
    response = endpoint.search(SearchRequest(query="q", user_id="u", top_k=3))
    assert len(response.data) == 3


def test_search_clamps_top_k_to_the_wire_cap(endpoint, client):
    # atomic/search's schema max limit is 100: a larger top_k fetches at the
    # cap (the response slice still honors top_k below).
    endpoint.search(SearchRequest(query="q", user_id="u", top_k=500))
    assert client.search_calls[0]["limit"] == 100


def test_search_gateway_error_maps_500(endpoint, client):
    client.search_error = TencentDBApiError(500, "boom")
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.search(SearchRequest(query="q", user_id="u"))
    assert excinfo.value.status_code == 500


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------
def test_update_replaces_text(endpoint, client):
    client.rows.append({"id": "a1", "content": "old", "version": 1})
    response = endpoint.update("a1", UpdateRequest(text="new text"), user_id="user-1")
    assert response.success is True
    assert response.memory.content == "new text"
    assert client.rows[0]["content"] == "new text"
    assert client.update_calls[0]["user_id"] == "user-1"  # the write scopes to the caller


def test_update_rejects_text_over_the_wire_cap(endpoint, client):
    # The update text rides the same 8192 zod cap as add messages: a
    # contract-legal request the gateway must reject is a 400, never a 500.
    client.rows.append({"id": "a1", "content": "old", "version": 1})
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update("a1", UpdateRequest(text="x" * 8193), user_id="user-1")
    assert excinfo.value.status_code == 400
    assert client.rows[0]["content"] == "old"  # untouched


def test_update_cap_counts_utf16_units(endpoint, client):
    # Same cap in the wire's unit: 5000 astral chars = 10000 UTF-16 code
    # units — over the cap even though Python's len() says 5000.
    client.rows.append({"id": "a1", "content": "old", "version": 1})
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update("a1", UpdateRequest(text="😀" * 5000), user_id="user-1")
    assert excinfo.value.status_code == 400
    assert client.rows[0]["content"] == "old"  # untouched


def test_update_metadata_only_answers_400(endpoint):
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update("a1", UpdateRequest(metadata={"k": "v"}), user_id="user-1")
    assert excinfo.value.status_code == 400


def test_update_text_and_metadata_answers_400(endpoint, client):
    # L1 rows carry no metadata: applying the text half would silently drop
    # the metadata half — 400 rather than a partial write claimed whole.
    client.rows.append({"id": "a1", "content": "old", "version": 1})
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update("a1", UpdateRequest(text="new", metadata={"k": "v"}), user_id="user-1")
    assert excinfo.value.status_code == 400
    assert client.rows[0]["content"] == "old"  # untouched


def test_update_unknown_id_native_404_maps_contract_404(endpoint, client):
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update("missing", UpdateRequest(text="x"), user_id="user-1")
    assert excinfo.value.status_code == 404


def test_update_native_403_maps_contract_404(endpoint, client):
    client.rows.append({"id": "a1", "content": "old", "version": 1})
    client.update_error = TencentDBApiError(403, "ownership mismatch")
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.update("a1", UpdateRequest(text="x"), user_id="user-1")
    assert excinfo.value.status_code == 404  # isolation must look like absence


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
def test_delete_single_element_batch(endpoint, client):
    client.rows.append({"id": "a1", "content": "old", "version": 1})
    response = endpoint.delete("a1", user_id="user-1")
    assert response.success is True
    assert client.deleted == ["a1"]
    assert client.delete_calls[0]["user_id"] == "user-1"  # the write scopes to the caller


def test_update_and_delete_default_owner(endpoint, client):
    # Without an explicit user_id the write scopes to the endpoint's default
    # owner — a silent "default" upstream would split the isolation tier.
    client.rows.append({"id": "a1", "content": "old", "version": 1})
    endpoint.update("a1", UpdateRequest(text="new"))
    endpoint.delete("a1")
    assert client.update_calls[0]["user_id"] == "minisweagent"
    assert client.delete_calls[0]["user_id"] == "minisweagent"


def test_delete_no_match_maps_404(endpoint, client):
    with pytest.raises(MemoryEndpointError) as excinfo:
        endpoint.delete("missing", user_id="user-1")
    assert excinfo.value.status_code == 404


def test_delete_gateway_error_maps_500(endpoint, client):
    client.rows.append({"id": "a1", "content": "old", "version": 1})
    original = client.atomic_delete
    client.atomic_delete = lambda *a, **k: (_ for _ in ()).throw(TencentDBApiError(500, "boom"))
    try:
        with pytest.raises(MemoryEndpointError) as excinfo:
            endpoint.delete("a1", user_id="user-1")
        assert excinfo.value.status_code == 500
    finally:
        client.atomic_delete = original

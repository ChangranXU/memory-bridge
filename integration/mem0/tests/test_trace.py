"""The mem0 arm's schema-v6 trace surface: platform receipts become generation
changes (native_receipt evidence, partial completeness), cross-checked against
the before/after state snapshot — which is also the exception path's only
evidence — and recalls become search + delivery, all against the real capture
server with the scripted platform client. The protocol machinery itself is
pinned generically in shared-bridge/tests/test_trace.py; here only the mem0
adapter shapes.
"""

import hashlib

import httpx

from mem0_bridge.backend import Mem0Backend
from mem0_bridge.client import Mem0ApiError
from mem0_bridge.config import Mem0Config

from shared_bridge.annotate import text_sha256


def _changes(server):
    return [event["payload"] for event in server.events("memory_change")]


def _ends(server):
    return [event["payload"] for event in server.events("memory_generate_end")]


def _seed_memory(fake_client, memory_id="m1", text="user prefers pytest"):
    fake_client.memories[memory_id] = {
        "id": memory_id,
        "memory": text,
        "user_id": "minisweagent",
        "run_id": "run-x",
        "score": 0.9,
        "metadata": {},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def test_session_and_binds_with_mem0_adapter(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("fix the bug")
    (session,) = capture_server.events("memory_session")
    payload = session["payload"]
    assert payload["adapter"]["name"] == "mem0"
    assert payload["adapter"]["version"] == backend._adapter_meta()["version"]
    assert payload["extensions"]["mem0"] == {"session_id": backend._session_id, "user_id": "minisweagent"}
    # The namespace is the hashed effective user id (the store scope).
    assert backend._namespace == hashlib.sha256(b"minisweagent").hexdigest()
    binds = capture_server.events("memory_role_bind")
    assert [b["payload"]["logical_role"] for b in binds] == ["main", "memory"]
    main_posts = [r for r in capture_server.requests if "/MAIN/" in r["path"]]
    memory_posts = [r for r in capture_server.requests if "/MEMORY/" in r["path"]]
    assert len(main_posts) == 1 and len(main_posts[0]["events"]) == 2
    assert len(memory_posts) == 1 and len(memory_posts[0]["events"]) == 1
    backend.finalize()


def test_memory_lane_resolves_from_env(tmp_path, capture_server, fake_client, monkeypatch):
    """The memory lane carries no model URL: its endpoint comes from the
    MEMORY_ANNOTATE_MEMORY_URL env (the generalized resolution) when the
    config field is blank."""
    monkeypatch.setenv("MEMORY_ANNOTATE_MEMORY_URL", capture_server.annotate_url("MEMORY"))
    config = Mem0Config(enabled=True, output_dir=str(tmp_path / "inst"), api_key="test-key")
    backend = Mem0Backend(config, "test-instance", model_base_url=capture_server.lane_url("MAIN"))
    backend.start()
    assert backend._trace is not None
    backend.set_task("task")
    assert len(capture_server.events("memory_session")) == 1
    backend.finalize()


# ---------------------------------------------------------------------------
# Generation (platform receipts -> changes)
# ---------------------------------------------------------------------------
def test_add_receipts_become_create_changes(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    (start,) = capture_server.events("memory_generate_start")
    payload = start["payload"]
    # No native message id: the base numbered the pending input synthetically.
    assert [i["input_id"] for i in payload["inputs"]] == ["1"]
    assert payload["inputs"][0]["content"]["text"] == "hello"
    assert payload["extensions"]["mem0"]["extraction_step"] == "2"
    assert "binding" not in start
    (change,) = _changes(capture_server)
    assert change["action"] == "create" and change["before"] == []
    (ref,) = change["after"]
    assert ref["item_id"] == "m1"
    assert ref["version_id"] == f"m1:{text_sha256('fact: hello')}"
    assert ref["identity_scheme"] == "mem0-platform-memory-v1"
    assert ref["identity_strength"] == "native_stable"
    assert ref["namespace"] == backend._namespace
    assert ref["content"]["text"] == "fact: hello"  # the receipt text, not the input
    assert change["evidence"] == "native_receipt"
    assert change["completeness"] == "partial"  # the receipt payload carries no before-image
    assert change["extensions"]["mem0"] == {"event": "ADD"}
    assert (change["change_index"], change["change_count"]) == (0, 1)
    (end,) = _ends(capture_server)
    assert end["status"] == "completed"
    assert [r["version_id"] for r in end["produced"]] == [ref["version_id"]]
    assert end["change_count"] == 1 and end["error_codes"] == []
    assert end["state_evidence"] == "complete"  # the receipts and the observed diff agree
    mem0 = end["extensions"]["mem0"]
    assert (mem0["added"], mem0["updated"], mem0["deleted"], mem0["none"]) == (1, 0, 0, 0)
    assert mem0["user_id"] == "minisweagent"
    # Change and end bind at the start cursor (capture server reports 0).
    events = capture_server.events("memory_change") + capture_server.events("memory_generate_end")
    assert all(e["binding"] == {"after_role_call_index": 0} for e in events)
    backend.finalize()


def test_update_and_delete_receipts_map_to_their_actions(traced_backend, capture_server, fake_client):
    _seed_memory(fake_client)  # m1 — the row the scripted UPDATE/DELETE acts on
    fake_client.add_event = "UPDATE"
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    (update,) = [c for c in _changes(capture_server) if c["action"] == "update"]
    assert update["before"] == []  # the receipt payload carries no before-image
    assert update["after"][0]["item_id"] == "m1"
    (end,) = _ends(capture_server)
    assert end["extensions"]["mem0"]["updated"] == 1
    assert end["state_evidence"] == "complete"  # the diff observed the same rewrite

    fake_client.add_event = "DELETE"
    backend.record([{"role": "user", "content": "again"}], step=3)
    backend._extract(4)
    (delete,) = [c for c in _changes(capture_server) if c["action"] == "delete"]
    assert delete["after"] == []
    assert delete["before"][0]["item_id"] == "m1"
    assert delete["evidence"] == "native_receipt" and delete["completeness"] == "partial"
    (end,) = [e for e in _ends(capture_server) if e["operation_id"] == delete["operation_id"]]
    assert end["extensions"]["mem0"]["deleted"] == 1
    assert end["produced"] == []  # a delete produces nothing
    assert end["state_evidence"] == "complete"
    backend.finalize()


def test_none_receipts_emit_a_zero_change_generation(traced_backend, capture_server, fake_client):
    fake_client.add_event = "NONE"  # the platform deduped every fact
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    assert _changes(capture_server) == []
    (end,) = _ends(capture_server)
    assert end["status"] == "completed" and end["change_count"] == 0
    assert end["extensions"]["mem0"]["none"] == 1
    backend.finalize()


def test_id_less_receipt_emits_no_change(traced_backend, capture_server, fake_client, monkeypatch):
    """A receipt without a platform id is uncitable: no change and no produced
    ref is fabricated for it (a fabricated identity would collapse every such
    row into one item). The native counters still count the platform's event."""
    monkeypatch.setattr(
        fake_client,
        "add",
        lambda **kwargs: [{"memory": "well-formed but id-less", "event": "ADD"}],
    )
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    assert _changes(capture_server) == []
    (end,) = _ends(capture_server)
    assert end["status"] == "completed" and end["change_count"] == 0 and end["produced"] == []
    assert end["extensions"]["mem0"]["added"] == 1  # the platform's own count stands
    assert backend._counts["memories_added"] == 1
    backend.finalize()


def test_unrecognized_receipt_event_warns_but_succeeds(traced_backend, capture_server, fake_client, monkeypatch, caplog):
    """A receipt whose event name the bridge does not know is not an
    extraction failure (the platform answered fine) — but it is never
    silent: a warning is logged, no change is fabricated for it, and the
    batch is still consumed."""
    monkeypatch.setattr(
        fake_client,
        "add",
        lambda **kwargs: [{"id": "m9", "memory": "mystery", "event": "CREATE"}],
    )
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    with caplog.at_level("WARNING", logger="mem0_bridge.backend"):
        backend._extract(2)
    assert "unrecognized event" in caplog.text
    assert _changes(capture_server) == []
    (end,) = _ends(capture_server)
    assert end["status"] == "completed" and end["change_count"] == 0
    assert backend._counts["memories_added"] == 0 and backend._counts["extraction_errors"] == 0
    assert backend._pending == []  # the batch is consumed, not retried forever
    backend.finalize()


def test_failed_add_takes_the_exception_path(traced_backend, capture_server, fake_client):
    """The add raises BEFORE anything persists: the observed before/after
    diff is empty, so the end stays failed — but with both snapshots
    available the evidence is now partial, not unknown."""
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    fake_client.add_error = Mem0ApiError(504, "poll timeout")
    backend._extract(2)  # contained by the base shell
    assert _changes(capture_server) == []
    (end,) = _ends(capture_server)
    assert end["status"] == "failed"
    assert end["error_codes"] == ["Mem0ApiError"]
    assert end["state_evidence"] == "partial" and end["produced"] == []  # the diff saw no writes
    assert end["extensions"]["mem0"]["user_id"] == "minisweagent"
    assert "added" not in end["extensions"]["mem0"]  # no receipts on this path
    # The buffer survived for the next tick, which then traces normally.
    fake_client.add_error = None
    backend._extract(4)
    assert len(capture_server.events("memory_generate_start")) == 2
    assert _ends(capture_server)[1]["status"] == "completed"
    backend.finalize()


def test_persisted_timed_out_add_reconciles_via_the_state_diff(traced_backend, capture_server, fake_client):
    """The campaign failure shape: the platform persists the extraction
    server-side, then the answer dies on the wire. The observed diff must
    attribute the landed rows — partial observed_diff changes under a
    partial generation — instead of a failed end that denies them."""
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    storing_add = fake_client.add

    def write_then_timeout(**kwargs):
        storing_add(**kwargs)  # the write lands server-side...
        raise httpx.RemoteProtocolError("Server disconnected")  # ...then the response dies

    fake_client.add = write_then_timeout
    backend._extract(2)  # contained by the base shell
    (change,) = _changes(capture_server)
    assert change["action"] == "create" and change["before"] == []
    (ref,) = change["after"]
    assert ref["item_id"] == "m1"
    assert ref["version_id"] == f"m1:{text_sha256('fact: hello')}"
    assert ref["identity_scheme"] == "mem0-platform-memory-v1"
    assert ref["namespace"] == backend._namespace
    assert change["evidence"] == "observed_diff"
    assert change["completeness"] == "partial"
    assert (change["change_index"], change["change_count"]) == (0, 1)
    (end,) = _ends(capture_server)
    assert end["status"] == "partial"
    assert end["error_codes"] == ["RemoteProtocolError"]
    assert end["change_count"] == 1 and end["state_evidence"] == "partial"
    assert "added" not in end["extensions"]["mem0"]  # no receipts on this path
    backend.finalize()


def test_retried_add_after_a_persisted_timeout_never_double_reports(traced_backend, capture_server, fake_client):
    """The buffer is retained across the failed add, so the next tick
    re-sends the same messages and the engine dedupes them (NONE receipts).
    The failed tick's diff already attributed the landed rows: the retry
    must post NO further change for them and audit clean."""
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    storing_add = fake_client.add

    def write_then_timeout(**kwargs):
        storing_add(**kwargs)
        raise Mem0ApiError(504, "poll timeout")

    fake_client.add = write_then_timeout
    backend._extract(2)
    fake_client.add = storing_add
    fake_client.add_event = "NONE"  # the retry's re-sent facts are deduped
    backend._extract(4)
    (change,) = _changes(capture_server)  # exactly the failed tick's recovered create
    assert change["after"][0]["item_id"] == "m1"
    ends = _ends(capture_server)
    assert [e["status"] for e in ends] == ["partial", "completed"]
    assert ends[1]["change_count"] == 0
    assert ends[1]["state_evidence"] == "complete"  # NONE claims match the quiet diff
    assert ends[1]["extensions"]["mem0"]["none"] == 1
    backend.finalize()


def test_snapshot_failure_degrades_to_unknown_evidence(traced_backend, capture_server, fake_client, monkeypatch):
    """A snapshot that hits the same flaky endpoint degrades to the
    pre-snapshot behavior: no diff evidence, unknown state, never a raise
    into the native path."""
    def failing_get_all(**kwargs):
        raise Mem0ApiError(503, "get-all boom")

    monkeypatch.setattr(fake_client, "get_all", failing_get_all)
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)  # clean native add; the audit is blind
    (end,) = _ends(capture_server)
    assert end["status"] == "completed" and end["change_count"] == 1  # receipts still trace
    assert end["state_evidence"] == "unknown"
    assert backend._counts["memories_added"] == 1
    # Same degrade on the exception path: failed, unknown, no diff changes.
    fake_client.add_error = Mem0ApiError(504, "poll timeout")
    backend.record([{"role": "user", "content": "again"}], step=3)
    backend._extract(4)
    ends = _ends(capture_server)
    assert ends[1]["status"] == "failed" and ends[1]["state_evidence"] == "unknown"
    assert len(_changes(capture_server)) == 1  # only the first tick's receipt change
    assert backend._counts["extraction_errors"] == 1
    backend.finalize()


def test_receipts_the_state_does_not_show_are_flagged_drift(traced_backend, capture_server, fake_client, monkeypatch):
    """The silent-insert class (documented for the OSS surfaces: ADD
    receipts over an empty write): a completed generation whose receipts
    the observed diff cannot confirm ends partial, not complete."""
    monkeypatch.setattr(
        fake_client,
        "add",
        lambda **kwargs: [{"id": "m9", "memory": "ghost receipt", "event": "ADD"}],  # claims, persists nothing
    )
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    (change,) = _changes(capture_server)
    assert change["evidence"] == "native_receipt" and change["completeness"] == "partial"
    (end,) = _ends(capture_server)
    assert end["status"] == "completed" and end["change_count"] == 1
    assert end["state_evidence"] == "partial"  # the claim has no state behind it
    assert end["extensions"]["mem0"]["unexplained"] == ["receipt create on m9 is not reflected in the after snapshot"]
    backend.finalize()


def test_create_then_delete_receipt_pair_nets_to_absent_cleanly(traced_backend, capture_server, fake_client, monkeypatch):
    """A same-id ADD+DELETE receipt pair in one batch nets to absent: the
    engine mints ids, so a delete receipt on an id the same batch created is
    necessarily the later action — the receipts are faithfully reflected and
    the audit must stay clean (no false drift line, no partial downgrade)."""
    monkeypatch.setattr(
        fake_client,
        "add",
        lambda **kwargs: [
            {"id": "m9", "memory": "contradicted fact", "event": "ADD"},
            {"id": "m9", "memory": None, "event": "DELETE"},
        ],
    )
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    assert [change["action"] for change in _changes(capture_server)] == ["create", "delete"]
    (end,) = _ends(capture_server)
    assert end["status"] == "completed" and end["state_evidence"] == "complete"
    assert "unexplained" not in end["extensions"]["mem0"]
    backend.finalize()


def test_state_changes_without_receipts_are_flagged_drift(traced_backend, capture_server, fake_client, monkeypatch):
    """The reverse direction: the store mutated but the engine reported no
    receipts, so no change can be cited — the generation posts none and the
    observed diff downgrades the end's evidence to partial."""
    storing_add = fake_client.add

    def silent_persist(**kwargs):
        storing_add(**kwargs)
        return []

    monkeypatch.setattr(fake_client, "add", silent_persist)
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    assert _changes(capture_server) == []
    (end,) = _ends(capture_server)
    assert end["status"] == "completed" and end["change_count"] == 0
    assert end["state_evidence"] == "partial"
    assert end["extensions"]["mem0"]["unexplained"] == ["observed create on m1 with no matching receipt"]
    backend.finalize()


def test_batched_receipts_for_one_row_do_not_false_flag(traced_backend, capture_server, fake_client, monkeypatch):
    """One add can report several receipts for the same row (created, then
    rewritten within the batch). The audit keys claims per id with all its
    actions: the observed create matches the ADD claim, and the row's
    presence confirms the UPDATE — no drift."""
    storing_add = fake_client.add

    def create_then_rewrite(**kwargs):
        receipts = storing_add(**kwargs)
        return receipts + [{"id": receipts[0]["id"], "memory": receipts[0]["memory"], "event": "UPDATE"}]

    monkeypatch.setattr(fake_client, "add", create_then_rewrite)
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    assert [c["action"] for c in _changes(capture_server)] == ["create", "update"]
    (end,) = _ends(capture_server)
    assert end["status"] == "completed" and end["change_count"] == 2
    assert end["state_evidence"] == "complete"
    assert "unexplained" not in end["extensions"]["mem0"]
    backend.finalize()


def test_start_409_disables_memory_lane_but_not_native_work(traced_backend, capture_server, fake_client):
    def responder(path, events):
        if any(e["type"] == "memory_generate_start" for e in events):
            return 409, {"error": "annotation_conflict"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_recovery_conflict"]
    assert record["op"] == "generate_start"
    assert backend._trace.memory_lane_enabled is False
    assert len(fake_client.add_calls) == 1  # the platform add ran untraced
    posts_before = len(capture_server.requests)
    backend.record([{"role": "user", "content": "again"}], step=3)
    backend._extract(4)
    assert len(capture_server.requests) == posts_before  # the lane stayed off
    assert len(fake_client.add_calls) == 2
    backend.finalize()


# ---------------------------------------------------------------------------
# Search and delivery
# ---------------------------------------------------------------------------
def test_search_end_returns_platform_hit_refs(traced_backend, capture_server, fake_client):
    _seed_memory(fake_client)
    backend = traced_backend()
    backend.set_task("fix the bug")
    recall = backend.recall_context(planned_step=3)
    assert recall is not None and recall["n_memories"] == 1
    (start,) = capture_server.events("memory_search_start")
    assert start["payload"]["query"]["text"] == "fix the bug"
    assert start["payload"]["extensions"]["mem0"]["planned_step"] == "3"
    (end,) = capture_server.events("memory_search_end")
    end_payload = end["payload"]
    assert end_payload["status"] == "completed"
    (item,) = end_payload["returned"]
    assert item["ordinal"] == 0 and "score" not in item
    ref = item["memory"]
    assert ref["item_id"] == "m1"
    assert ref["version_id"] == f"m1:{text_sha256('user prefers pytest')}"
    assert ref["extensions"]["mem0"]["score"] == 0.9
    assert ref["extensions"]["mem0"]["user_id"] == "minisweagent"
    mem0 = end_payload["extensions"]["mem0"]
    assert (mem0["matched"], mem0["selected"], mem0["rendered"], mem0["budget_dropped"]) == (1, 1, 1, 0)
    # The hosted platform returns a top-k pool, so the raw hit count is only
    # a floor on the true match total — the default precision.
    assert end_payload["matched_count"] == {"value": 1, "precision": "lower_bound"}
    backend.finalize()


def test_id_less_hit_is_never_traced(traced_backend, capture_server, fake_client, monkeypatch):
    """An id-less search hit is dropped at the intake: the search end returns
    only the citable hits and the delivery cites nothing fabricated."""
    _seed_memory(fake_client)
    real_search = fake_client.search

    def with_id_less_row(**kwargs):
        return [{"memory": "id-less row", "score": 0.1}] + real_search(**kwargs)

    monkeypatch.setattr(fake_client, "search", with_id_less_row)
    backend = traced_backend()
    backend.set_task("fix the bug")
    recall = backend.recall_context(planned_step=1)
    assert recall is not None and recall["n_memories"] == 1
    (end,) = capture_server.events("memory_search_end")
    (item,) = end["payload"]["returned"]
    assert item["memory"]["item_id"] == "m1"
    assert item["memory"]["extensions"]["mem0"]["score"] == 0.9
    backend.finalize()


def test_string_and_unusable_scores_never_fabricate_zero(traced_backend, capture_server, fake_client):
    """Score fidelity: a string score is parsed, but a non-numeric or
    non-finite score is dropped from the ref — coercing it to 0.0 would
    fabricate ranking evidence."""
    _seed_memory(fake_client, memory_id="m1", text="fact one")
    fake_client.memories["m1"]["score"] = "0.87"
    _seed_memory(fake_client, memory_id="m2", text="fact two")
    fake_client.memories["m2"]["score"] = "high"
    _seed_memory(fake_client, memory_id="m3", text="fact three")
    fake_client.memories["m3"]["score"] = float("nan")
    backend = traced_backend()
    backend.set_task("fix the bug")
    recall = backend.recall_context(planned_step=1)
    assert recall is not None and recall["n_memories"] == 3
    (end,) = capture_server.events("memory_search_end")
    refs = {item["memory"]["item_id"]: item["memory"] for item in end["payload"]["returned"]}
    assert refs["m1"]["extensions"]["mem0"]["score"] == 0.87
    assert "score" not in refs["m2"]["extensions"]["mem0"]
    assert "score" not in refs["m3"]["extensions"]["mem0"]
    backend.finalize()


def test_delivery_re_derives_the_same_refs(traced_backend, capture_server, fake_client):
    _seed_memory(fake_client)
    backend = traced_backend()
    backend.set_task("fix the bug")
    recall = backend.recall_context(planned_step=1)
    cursor = backend.main_lane_cursor()
    backend.deliver_recall(recall, step=2, msg_index=4, cursor=cursor)
    (delivery,) = capture_server.events("memory_delivery")
    payload = delivery["payload"]
    assert payload["status"] == "placed"
    assert payload["extensions"]["mem0"] == {
        "session_id": backend._session_id,
        "delivered": 1,
        "msg_index": 4,
    }
    block = recall["content"]
    assert delivery["binding"] == {
        "after_role_call_index": 0,
        "proofs": [{"kind": "canonical_message", "msg_index": 4, "content_sha256": text_sha256(block)}],
    }
    # The delivery cites exactly the search's returned identities.
    returned = capture_server.events("memory_search_end")[0]["payload"]["returned"]
    assert [item["memory"]["version_id"] for item in returned] == [payload["memories"][0]["version_id"]]
    backend.finalize()

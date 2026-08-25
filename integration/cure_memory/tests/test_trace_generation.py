"""Step 4 (PLAN §6.5): the extraction→memory_change audit mapping.

Real SQLite stores, the scripted decision client, and the real capture server:
every test drives native CURE writes and asserts the exact change/end events
the backend reconstructs — create/update/noop/delete trichotomy, CURE's
first-active-row no-op rule, error/exception paths, chunking, and cursor
binding."""

from types import SimpleNamespace

import re

from conftest import approved_candidate

from shared_bridge.annotate import text_sha256
from shared_bridge.backend import _sanitize_error_code


def _extract_with(backend, fake_client, step, content, decision):
    """Record one user message, queue one decision, run one extraction."""
    backend.record([{"role": "user", "content": content}], step=step - 1)
    fake_client.queue.append(decision)
    backend._extract(step)


def _decision(*candidates):
    return {"candidates": list(candidates), "deletions": [], "rejections": []}


def _changes(server):
    return [event["payload"] for event in server.events("memory_change")]


def _ends(server):
    return [event["payload"] for event in server.events("memory_generate_end")]


def _rows(backend):
    return backend._system.memory_search("minisweagent", query=None, review_status=None)


def test_vacuous_extraction_posts_no_generation_op(capture_server, fake_client, traced_backend):
    """A tick with no unextracted messages is not a traced operation either:
    no memory_generate_start/end with empty inputs enters the trajectory."""
    backend = traced_backend()
    backend.set_task("task")
    backend._extract(2)
    backend.finalize()
    assert capture_server.events("memory_generate_start") == []
    assert capture_server.events("memory_generate_end") == []
    assert backend._counts["extraction_calls"] == 0


def test_create_change_mapping(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("task")
    _extract_with(backend, fake_client, 2, "remember the rolling rule", _decision(approved_candidate(1, "rolling-rule", "always roll the mean")))
    (change,) = _changes(capture_server)
    (row,) = _rows(backend)
    assert change["action"] == "create"
    assert change["before"] == []
    (ref,) = change["after"]
    assert ref["version_id"].startswith(f"{row.id}:")
    assert ref["identity_strength"] == "native_stable"
    assert ref["identity_scheme"] == "cure-sqlite-row-version-v1"
    assert ref["namespace"] == backend._namespace
    assert ref["content"]["text"] == "always roll the mean"
    assert ref["content"]["sha256"] == text_sha256("always roll the mean")
    assert ref["extensions"]["cure"]["store_id"] == row.id
    assert change["extensions"]["cure"] == {"before_store_ids": [], "after_store_ids": [row.id]}
    assert change["evidence"] == "observed_diff" and change["completeness"] == "complete"
    assert (change["change_index"], change["change_count"]) == (0, 1)
    (end,) = _ends(capture_server)
    assert end["status"] == "completed"
    assert [r["version_id"] for r in end["produced"]] == [ref["version_id"]]
    assert end["change_count"] == 1 and end["state_evidence"] == "complete" and end["error_codes"] == []
    cure = end["extensions"]["cure"]
    assert cure["checkpoint"] == "advanced" and cure["mutation_audit"] == "clean"
    assert cure["candidates"] == 1 and cure["approved"] == 1
    # Same operation across start/change/end; the start itself is unbound.
    (start,) = [e for e in capture_server.events("memory_generate_start")]
    assert start["payload"]["operation_id"] == change["operation_id"] == end["operation_id"]
    assert "binding" not in start
    backend.finalize()


def test_update_change_mapping_with_supersedes(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("task")
    _extract_with(backend, fake_client, 2, "remember the rolling rule", _decision(approved_candidate(1, "rolling-rule", "version one")))
    _extract_with(backend, fake_client, 4, "actually update the rolling rule", _decision(approved_candidate(2, "rolling-rule", "version two")))
    (update,) = [c for c in _changes(capture_server) if c["action"] == "update"]
    old_row, new_row = sorted(_rows(backend), key=lambda row: row.id)
    assert update["extensions"]["cure"]["before_store_ids"] == [old_row.id]
    assert update["extensions"]["cure"]["after_store_ids"] == [new_row.id]
    (relationship,) = update["relationships"]
    assert relationship["type"] == "supersedes"
    assert relationship["from_version_id"] == update["before"][0]["version_id"]
    assert relationship["to_version_id"] == update["after"][0]["version_id"]
    assert old_row.review_status == "superseded" and old_row.superseded_by == new_row.id
    assert update["after"][0]["extensions"]["cure"]["supersedes"] == [old_row.id]
    (end,) = [e for e in _ends(capture_server) if e["operation_id"] == update["operation_id"]]
    assert end["status"] == "completed"
    assert [r["version_id"] for r in end["produced"]] == [update["after"][0]["version_id"]]
    backend.finalize()


def test_first_active_row_noop_produces_existing_row_ref(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("task")
    _extract_with(backend, fake_client, 2, "remember the rolling rule", _decision(approved_candidate(1, "rolling-rule", "same value")))
    # Identical key/value/review-status: CURE's first-active-row rule makes
    # this a no-op, so the candidate keeps id=None and the audit must point at
    # the already-stored row version, never at a new one.
    _extract_with(backend, fake_client, 4, "remember the rolling rule again", _decision(approved_candidate(2, "rolling-rule", "same value")))
    (noop,) = [c for c in _changes(capture_server) if c["action"] == "noop"]
    (row,) = _rows(backend)
    assert noop["before"] == noop["after"]
    assert noop["after"][0]["version_id"].startswith(f"{row.id}:")
    (end,) = [e for e in _ends(capture_server) if e["operation_id"] == noop["operation_id"]]
    assert [r["version_id"] for r in end["produced"]] == [noop["after"][0]["version_id"]]
    assert len(_rows(backend)) == 1  # the no-op wrote nothing
    backend.finalize()


def test_general_candidate_creates_alongside_a_repo_bound_row(capture_server, fake_client, traced_backend):
    """The layer guard end to end: a general candidate carrying the same
    type+key+value as a repo-bound row is NOT a dedup no-op against it — the
    native upsert creates a separate general row and the audit replays a
    create (the mirror guard in _first_active_row keeps the replay on the same
    predicate), so both mutation audits stay clean."""
    backend = traced_backend()
    backend.set_task("task")
    _extract_with(
        backend, fake_client, 2, "remember the shared rule", _decision(approved_candidate(1, "shared-rule", "same value", scope="project"))
    )
    _extract_with(
        backend, fake_client, 4, "remember the shared rule again", _decision(approved_candidate(2, "shared-rule", "same value"))
    )
    rows = _rows(backend)
    assert len(rows) == 2
    by_scope = {row.scope: row for row in rows}
    assert by_scope["project"].project_id == "test-instance"
    assert by_scope["user"].project_id is None
    creates = [c for c in _changes(capture_server) if c["action"] == "create"]
    assert len(creates) == 2  # no noop bound to the repo-bound row
    for end in _ends(capture_server):
        assert end["extensions"]["cure"]["mutation_audit"] == "clean"
    backend.finalize()


def test_repo_candidate_dedupes_into_a_general_row(capture_server, fake_client, traced_backend):
    """The cross-layer no-op, traced — the audit-consistency argument's other
    direction (test_identical_value_noop_spans_layers pins it untraced): a
    repo-bound candidate whose value already lives in a general row of the
    same type+key stores nothing new, and the audit's _first_active_row mirror
    (a repo candidate matches its own repo's rows plus the general rows)
    attributes the no-op to the general row itself — never an unexplained
    drift, never a fabricated create."""
    backend = traced_backend()
    backend.set_task("task")
    _extract_with(
        backend, fake_client, 2, "remember the shared rule", _decision(approved_candidate(1, "shared-rule", "same value"))
    )
    _extract_with(
        backend, fake_client, 4, "remember the shared rule again",
        _decision(approved_candidate(2, "shared-rule", "same value", scope="project")),
    )
    (row,) = _rows(backend)
    assert row.project_id is None  # the general row stands alone; nothing new stored
    (noop,) = [c for c in _changes(capture_server) if c["action"] == "noop"]
    assert noop["before"] == noop["after"]
    assert noop["after"][0]["version_id"].startswith(f"{row.id}:")
    (end,) = [e for e in _ends(capture_server) if e["operation_id"] == noop["operation_id"]]
    assert [r["version_id"] for r in end["produced"]] == [noop["after"][0]["version_id"]]
    assert end["extensions"]["cure"]["mutation_audit"] == "clean"
    backend.finalize()


def test_delete_change_mapping(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("task")
    _extract_with(backend, fake_client, 2, "remember the rolling rule", _decision(approved_candidate(1, "rolling-rule", "always roll the mean")))
    # The seeded row is general (approved_candidate's scope="user"), so the
    # deletion names that layer — a layer-less target stays in the session's
    # own layer and would not match it.
    _extract_with(
        backend, fake_client, 4, "forget the rolling rule",
        {"candidates": [], "deletions": [{"target": "rolling-rule", "scope": "user"}], "rejections": []},
    )
    (delete,) = [c for c in _changes(capture_server) if c["action"] == "delete"]
    (row,) = _rows(backend)
    assert delete["after"] == []
    assert delete["before"][0]["version_id"].startswith(f"{row.id}:")
    assert delete["extensions"]["cure"] == {"before_store_ids": [row.id], "after_store_ids": []}
    assert row.review_status == "deleted"
    (end,) = [e for e in _ends(capture_server) if e["operation_id"] == delete["operation_id"]]
    assert end["status"] == "completed" and end["extensions"]["cure"]["deleted"] == 1
    backend.finalize()


def test_repeated_deletion_matches_nothing(capture_server, fake_client, traced_backend):
    """Deletion matching skips terminal rows, so a repeated delete re-matches
    nothing: no second delete change, no inflated count, and the audit stays
    clean (the silence is the explanation — not an unexplained drift)."""
    backend = traced_backend()
    backend.set_task("task")
    _extract_with(backend, fake_client, 2, "remember the rolling rule", _decision(approved_candidate(1, "rolling-rule", "always roll the mean")))
    deletion = {"candidates": [], "deletions": [{"target": "rolling-rule", "scope": "user"}], "rejections": []}
    _extract_with(backend, fake_client, 4, "forget the rolling rule", deletion)
    _extract_with(backend, fake_client, 6, "forget the rolling rule again", deletion)
    (delete,) = [c for c in _changes(capture_server) if c["action"] == "delete"]
    (row,) = _rows(backend)
    assert row.review_status == "deleted"
    second_end = [e for e in _ends(capture_server) if e["extensions"]["cure"]["extraction_step"] == "6"]
    (second_end,) = second_end
    assert second_end["status"] == "completed" and second_end["extensions"]["cure"]["deleted"] == 0
    assert second_end["extensions"]["cure"]["mutation_audit"] == "clean"
    backend.finalize()


def test_one_pass_create_update_noop_chain(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("task")
    _extract_with(backend, fake_client, 2, "remember the rolling rule", _decision(approved_candidate(1, "rolling-rule", "version one")))
    decision = {
        "candidates": [
            approved_candidate(2, "brand-new", "fresh fact"),
            approved_candidate(2, "rolling-rule", "version two"),
            approved_candidate(2, "rolling-rule", "version two"),
        ],
        "deletions": [],
        "rejections": [],
    }
    _extract_with(backend, fake_client, 4, "revise everything", decision)
    starts = capture_server.events("memory_generate_start")
    operation_id = starts[1]["payload"]["operation_id"]
    changes = [c for c in _changes(capture_server) if c["operation_id"] == operation_id]
    assert [c["action"] for c in changes] == ["create", "update", "noop"]
    assert [c["change_index"] for c in changes] == [0, 1, 2]
    assert {c["change_count"] for c in changes} == {3}
    # The in-pass no-op binds to the row version created by the in-pass update.
    assert changes[2]["after"][0]["version_id"] == changes[1]["after"][0]["version_id"]
    (end,) = [e for e in _ends(capture_server) if e["operation_id"] == operation_id]
    assert end["change_count"] == 3 and end["extensions"]["cure"]["candidates"] == 3
    backend.finalize()


def test_errors_hold_writes_and_checkpoint(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "remember the rolling rule"}], step=1)
    fake_client.queue.append("http_500")
    backend._extract(2)
    (end,) = _ends(capture_server)
    assert end["status"] == "failed"
    assert end["error_codes"] == ["llm_decision_failed_http_500"]  # ':' is not annotation-safe
    assert end["extensions"]["cure"]["checkpoint"] == "held"
    assert _changes(capture_server) == [] and end["produced"] == []
    assert _rows(backend) == []  # CURE wrote nothing
    # The held checkpoint re-offers the same input on the next start.
    _extract_with(backend, fake_client, 4, "another fact", {"candidates": [], "deletions": [], "rejections": []})
    starts = capture_server.events("memory_generate_start")
    assert [i["input_id"] for i in starts[1]["payload"]["inputs"]] == ["1", "2"]
    assert _ends(capture_server)[1]["extensions"]["cure"]["checkpoint"] == "advanced"
    backend.finalize()


def test_free_form_error_text_is_sanitized_onto_the_recorder_charset(capture_server, fake_client, traced_backend):
    """CURE errors are free-form text, but the recorder requires each code to
    fullmatch [A-Za-z0-9][A-Za-z0-9._-]{0,127}: every character outside the
    charset folds, not just ':' (a rejected end would orphan the operation
    and 409-disable the lane on the next start)."""
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "remember the rolling rule"}], step=1)
    fake_client.queue.append("[Errno 111] Connection refused")
    backend._extract(2)
    (end,) = _ends(capture_server)
    assert end["status"] == "failed"
    assert end["error_codes"] == ["llm_decision_failed__Errno_111__Connection_refused"]
    for code in end["error_codes"]:
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", code)
    backend.finalize()


def test_sanitize_error_code_edge_cases():
    assert _sanitize_error_code("llm_decision_failed:http_500") == "llm_decision_failed_http_500"
    assert _sanitize_error_code("") == "error"
    assert _sanitize_error_code("!!!") == "error"
    assert _sanitize_error_code("[Errno 111] Connection refused") == "Errno_111__Connection_refused"
    assert _sanitize_error_code("café") == "caf_"
    assert len(_sanitize_error_code("x" * 300)) == 128


def test_error_result_produced_refs_are_operation_local(fake_client, traced_backend):
    backend = traced_backend()
    candidate = SimpleNamespace(
        id=None,
        value="never stored",
        user_id="minisweagent",
        project_id="test-instance",
        scope="project",
        memory_type="fact",
        key="lost-candidate",
        confidence=0.9,
        review_status="approved",
    )
    result = SimpleNamespace(errors=["llm_decision_failed:timeout"], candidates=[candidate], deleted=[])
    changes, produced, unexplained = backend._classify(SimpleNamespace(before={}), result, None)
    assert changes == [] and unexplained == []
    (ref,) = produced
    assert ref["identity_strength"] == "operation_local"
    assert ref["identity_scheme"] == "cure-decision-candidate-v1"
    assert ref["version_id"] == "candidate-0"
    assert ref["content"]["text"] == "never stored"
    backend.finalize()


def test_unavailable_before_snapshot_never_claims_drift(capture_server, fake_client, traced_backend, monkeypatch):
    """A failed begin snapshot makes attribution impossible: the end reports
    unknown state evidence with no changes — replaying onto an empty view
    would fabricate drift for every pre-existing row."""
    backend = traced_backend()
    backend.set_task("task")
    real_snapshot = backend._snapshot_rows
    calls = {"n": 0}

    def snapshot_unavailable_once():
        calls["n"] += 1
        return None if calls["n"] == 1 else real_snapshot()

    monkeypatch.setattr(backend, "_snapshot_rows", snapshot_unavailable_once)
    _extract_with(
        backend, fake_client, 2, "remember the rolling rule", _decision(approved_candidate(1, "rolling-rule", "always roll the mean"))
    )
    assert calls["n"] == 2  # begin failed, finish succeeded
    assert _changes(capture_server) == []
    (end,) = _ends(capture_server)
    assert end["status"] == "completed"
    assert end["state_evidence"] == "unknown"
    assert end["change_count"] == 0
    cure = end["extensions"]["cure"]
    assert "unexplained" not in cure and cure["mutation_audit"] == "clean"
    (row,) = _rows(backend)
    (ref,) = end["produced"]
    assert ref["version_id"].startswith(f"{row.id}:")
    backend.finalize()


def test_unavailable_before_snapshot_still_attributes_noop_candidates(
    capture_server, fake_client, traced_backend, monkeypatch
):
    """The begin snapshot failure forfeits the working view, but a dedup no-op
    candidate's matched row is untouched by the extraction: attribute the
    no-op against the after snapshot (noop change + existing row ref in
    produced) instead of silently dropping the candidate."""
    backend = traced_backend()
    backend.set_task("task")
    _extract_with(backend, fake_client, 2, "remember the rolling rule", _decision(approved_candidate(1, "rolling-rule", "same value")))
    real_snapshot = backend._snapshot_rows
    calls = {"n": 0}

    def snapshot_unavailable_once():
        calls["n"] += 1
        return None if calls["n"] == 1 else real_snapshot()

    monkeypatch.setattr(backend, "_snapshot_rows", snapshot_unavailable_once)
    # Identical key/value/review-status as the stored row: CURE dedupes it
    # (id=None no-op branch) while the begin snapshot is unavailable.
    _extract_with(backend, fake_client, 4, "remember the rolling rule again", _decision(approved_candidate(2, "rolling-rule", "same value")))
    assert calls["n"] == 2  # begin failed, finish succeeded
    (noop,) = [c for c in _changes(capture_server) if c["action"] == "noop"]
    (row,) = _rows(backend)
    assert noop["before"] == noop["after"]
    assert noop["after"][0]["version_id"].startswith(f"{row.id}:")
    (end,) = [e for e in _ends(capture_server) if e["operation_id"] == noop["operation_id"]]
    assert end["status"] == "completed"
    assert end["state_evidence"] == "unknown"  # the before snapshot was unavailable
    assert [r["version_id"] for r in end["produced"]] == [noop["after"][0]["version_id"]]
    assert end["extensions"]["cure"]["mutation_audit"] == "clean"
    assert len(_rows(backend)) == 1  # the no-op wrote nothing
    backend.finalize()


def test_exception_mid_write_posts_partial_diff(capture_server, fake_client, traced_backend, monkeypatch):
    backend = traced_backend()
    backend.set_task("task")
    real_extract = backend._system.extract_runtime_memories

    def extract_then_raise():
        real_extract()
        raise RuntimeError("simulated mid-write crash")

    monkeypatch.setattr(backend._system, "extract_runtime_memories", extract_then_raise)
    _extract_with(backend, fake_client, 2, "remember the rolling rule", _decision(approved_candidate(1, "rolling-rule", "always roll the mean")))
    # The native error path ran (no raise, strict=false), and the annotation
    # fallback still records the observable row-level diff as partial evidence.
    assert backend._consecutive_errors == 1
    (change,) = _changes(capture_server)
    assert change["action"] == "create" and change["completeness"] == "partial"
    (end,) = _ends(capture_server)
    assert end["status"] == "partial"
    assert end["error_codes"] == ["RuntimeError"]
    assert end["state_evidence"] == "partial" and end["produced"] == []
    assert end["extensions"]["cure"]["checkpoint"] == "held"
    assert [row.key for row in _rows(backend)] == ["rolling-rule"]  # native write survived
    backend.finalize()


def test_change_events_chunk_at_256(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("task")
    decision = {
        "candidates": [approved_candidate(1, f"key-{index}", f"value {index}") for index in range(300)],
        "deletions": [],
        "rejections": [],
    }
    _extract_with(backend, fake_client, 2, "remember many things", decision)
    change_posts = [r for r in capture_server.requests if any(e["type"] == "memory_change" for e in r["events"])]
    assert [len(r["events"]) for r in change_posts] == [256, 44]
    changes = _changes(capture_server)
    assert [c["change_index"] for c in changes] == list(range(300))
    assert {c["change_count"] for c in changes} == {300}
    assert len({c["operation_id"] for c in changes}) == 1
    (end,) = _ends(capture_server)
    assert end["change_count"] == 300 and len(end["produced"]) == 300
    assert len(_rows(backend)) == 300
    backend.finalize()


def test_start_cursor_binds_every_later_event_of_the_operation(capture_server, fake_client, traced_backend):
    capture_server.cursor = 7
    backend = traced_backend()
    backend.set_task("task")
    _extract_with(backend, fake_client, 2, "remember the rolling rule", _decision(approved_candidate(1, "rolling-rule", "always roll the mean")))
    events = capture_server.events("memory_change") + capture_server.events("memory_generate_end")
    assert events and all(e["binding"] == {"after_role_call_index": 7} for e in events)
    # Only events of this operation bind at its start cursor; the start itself never binds.
    (start,) = capture_server.events("memory_generate_start")
    assert "binding" not in start
    backend.finalize()

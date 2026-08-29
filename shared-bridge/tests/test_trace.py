"""The schema-v6 memory-protocol surface of ``BaseMemoryBackend``, pinned
generically through the conftest FakeBackend against the real capture server:
session/binds, the generation operation (generic observed diff), pending-input
lifecycle, search start/end, the delivery proof, the anchor gate, and the
413/409/ambiguous start classification plus the delivery failure semantics.
"""

import json

from fake_integration import FakeBackend, _config, _started

from shared_bridge.annotate import Annotator, text_sha256
from shared_bridge.backend import BaseMemoryBackend

def _changes(server):
    return [event["payload"] for event in server.events("memory_change")]


def _ends(server):
    return [event["payload"] for event in server.events("memory_generate_end")]


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def test_session_and_binds_posted_with_exact_task(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("fix the rolling regression")
    (session,) = capture_server.events("memory_session")
    payload = session["payload"]
    assert payload["task"]["text"] == "fix the rolling regression"
    assert payload["task"]["sha256"] == text_sha256("fix the rolling regression")
    assert payload["adapter"] == {"name": "fake", "version": "0.0.1"}
    assert payload["extensions"] == {"fake": {}}  # the default _trace_context
    binds = capture_server.events("memory_role_bind")
    assert [(b["payload"]["logical_role"], b["payload"]["trace_session_id"]) for b in binds] == [
        ("main", payload["trace_session_id"]),
        ("memory", payload["trace_session_id"]),
    ]
    # Session + main bind went to the main endpoint, the memory bind to its own.
    main_posts = [r for r in capture_server.requests if "/MAIN/" in r["path"]]
    memory_posts = [r for r in capture_server.requests if "/MEMORY/" in r["path"]]
    assert len(main_posts) == 1 and len(main_posts[0]["events"]) == 2
    assert len(memory_posts) == 1 and len(memory_posts[0]["events"]) == 1
    backend.finalize()


def test_tracing_disabled_for_provider_urls(tmp_path):
    backend = _started(tmp_path)  # FakeBackend default: no lane URLs at all
    assert backend._trace is None
    backend.set_task("task")  # no endpoint, no crash, no posts
    backend.finalize()


def test_untraced_annotation_surface_is_inert(tmp_path):
    """The real tracing implementations are inert when untraced: no I/O, no
    raise, no state."""
    backend = _started(tmp_path)
    assert backend.main_lane_cursor() is None
    assert backend.consume_annotation_duration() == 0.0
    backend.deliver_recall({"content": "x", "memories": []}, step=1, msg_index=0, cursor=None)
    backend.set_task("t")
    backend.system.hits = ["one"]
    recall = backend.recall_context(planned_step=1)
    assert recall is not None and backend._trace is None
    backend.finalize()


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def test_generation_events_from_the_generic_observed_diff(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "first fact"}], step=1)
    backend._extract(2)
    (start,) = capture_server.events("memory_generate_start")
    payload = start["payload"]
    # No native message id: the base numbered the pending input synthetically.
    assert [i["input_id"] for i in payload["inputs"]] == ["1"]
    (item,) = payload["inputs"]
    assert item["kind"] == "message" and item["role"] == "user" and item["source_step"] == 1
    assert item["content"]["text"] == "first fact"
    assert item["content"]["sha256"] == text_sha256("first fact")
    assert payload["trigger"] == "step" and payload["main_step"] == 2
    assert payload["requested_by"] == "main" and payload["handled_by"] == "memory"
    assert payload["extensions"]["fake"] == {"session_id": backend._session_id, "extraction_step": "2"}
    assert "binding" not in start
    (change,) = _changes(capture_server)
    assert change["action"] == "create" and change["before"] == []
    (ref,) = change["after"]
    assert ref["content"]["text"] == "first fact"
    assert ref["identity_scheme"] == "fake-row-v1"
    assert ref["identity_strength"] == "derived_content"
    assert ref["namespace"] == backend._namespace
    assert change["evidence"] == "observed_diff" and change["completeness"] == "complete"
    assert (change["change_index"], change["change_count"]) == (0, 1)
    (end,) = _ends(capture_server)
    assert end["status"] == "completed"
    assert [r["version_id"] for r in end["produced"]] == [ref["version_id"]]
    assert end["change_count"] == 1 and end["state_evidence"] == "complete" and end["error_codes"] == []
    assert end["extensions"] == {"fake": {}}  # the default _generation_end_context
    assert end["operation_id"] == change["operation_id"] == payload["operation_id"]
    backend.finalize()


def test_narrow_change_payload_override_still_traces(traced_backend, capture_server, monkeypatch):
    """An integration-side _change_payload override may take the narrow
    positional form (the CURE adapter's shape): the base's generic observed
    diff calls the hook positionally, so it still traces through it."""
    base_payload = BaseMemoryBackend._change_payload

    def narrow(self, operation, action, before_rows, after_rows, supersede_new=None):
        return base_payload(self, operation, action, before_rows, after_rows, supersede_new=supersede_new)

    monkeypatch.setattr(FakeBackend, "_change_payload", narrow)
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "first fact"}], step=1)
    backend._extract(2)
    (change,) = _changes(capture_server)
    assert change["action"] == "create" and change["evidence"] == "observed_diff"
    backend.finalize()


def test_vacuous_extraction_posts_no_generation_op(traced_backend, capture_server):
    """A tick with no pending messages is not a traced operation either: no
    memory_generate_start/end with empty inputs enters the trajectory."""
    backend = traced_backend()
    backend.set_task("task")
    backend._extract(2)
    backend.finalize()
    assert capture_server.events("memory_generate_start") == []
    assert capture_server.events("memory_generate_end") == []
    assert backend._counts["extraction_calls"] == 0


def test_failed_extraction_with_a_failed_begin_snapshot_reports_unknown(traced_backend, capture_server, monkeypatch):
    """The exception path applies the same both-snapshots rule as the success
    path: a begin snapshot that failed while the end one succeeded leaves no
    observable diff — zero changes under an ``unknown`` label, never a
    zero-change ``partial`` claiming evidence this path does not have."""
    backend = traced_backend()
    real_snapshot = backend._snapshot_memory_state
    calls = {"n": 0}

    def flaky_begin_snapshot():
        calls["n"] += 1
        return None if calls["n"] == 1 else real_snapshot()

    monkeypatch.setattr(backend, "_snapshot_memory_state", flaky_begin_snapshot)
    backend.system.extract_error = RuntimeError("extraction boom")
    backend.set_task("task")
    backend.record([{"role": "user", "content": "first fact"}], step=1)
    backend._extract(2)  # contained by the base shell
    (end,) = _ends(capture_server)
    assert end["status"] == "failed" and end["change_count"] == 0
    assert end["state_evidence"] == "unknown"
    backend.finalize()


def test_pending_inputs_follow_checkpoint_rules(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "first fact"}], step=1)
    backend._extract(2)
    starts = capture_server.events("memory_generate_start")
    assert [i["input_id"] for i in starts[0]["payload"]["inputs"]] == ["1"]
    # Success cleared the pending refs: the next start carries only newer input.
    backend.record([{"role": "user", "content": "second fact"}], step=3)
    backend._extract(4)
    starts = capture_server.events("memory_generate_start")
    assert [i["input_id"] for i in starts[1]["payload"]["inputs"]] == ["2"]
    # A hard failure holds the inputs: the same input is offered again, and the
    # failed operation closed with the exception path (no diff -> failed).
    backend.record([{"role": "user", "content": "third fact"}], step=5)
    backend.system.extract_error = RuntimeError("extract boom")
    backend._extract(6)
    (failed_end,) = [e for e in _ends(capture_server) if e["status"] == "failed"]
    assert failed_end["error_codes"] == ["RuntimeError"]
    assert failed_end["change_count"] == 0 and failed_end["produced"] == []
    backend.system.extract_error = None
    backend._extract(8)
    starts = capture_server.events("memory_generate_start")
    assert [i["input_id"] for i in starts[3]["payload"]["inputs"]] == ["3"]
    backend.finalize()


def test_start_cursor_binds_every_later_event_of_the_operation(traced_backend, capture_server):
    capture_server.cursor = 7
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "first fact"}], step=1)
    backend._extract(2)
    events = capture_server.events("memory_change") + capture_server.events("memory_generate_end")
    assert events and all(e["binding"] == {"after_role_call_index": 7} for e in events)
    # Only events of this operation bind at its start cursor; the start itself never binds.
    (start,) = capture_server.events("memory_generate_start")
    assert "binding" not in start
    backend.finalize()


def test_start_413_runs_native_extraction_untraced(traced_backend, capture_server):
    def responder(path, events):
        if any(e["type"] == "memory_generate_start" for e in events):
            return 413, {"error": "too large"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "remember the fix"}], step=1)
    backend._extract(2)
    # Native extraction ran; the rejected start produced no orphan events.
    assert capture_server.events("memory_change") == []
    assert capture_server.events("memory_generate_end") == []
    assert backend.system.rows == ["remember the fix"]
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_start_oversize"]
    assert record["operation_id"] and record["step"] == 2
    backend.finalize()


def test_start_409_disables_memory_lane_but_not_native_work(traced_backend, capture_server):
    def responder(path, events):
        if any(e["type"] == "memory_generate_start" for e in events):
            return 409, {"error": "annotation_conflict"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "remember the fix"}], step=1)
    backend._extract(2)
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_recovery_conflict"]
    assert record["operation_id"] and record["step"] == 2
    assert backend._trace.memory_lane_enabled is False
    posts_before = len(capture_server.requests)
    # The lane stays disabled: later extractions post nothing, still extract natively.
    backend.record([{"role": "user", "content": "another fact"}], step=3)
    backend._extract(4)
    assert len(capture_server.requests) == posts_before
    assert backend.system.rows == ["remember the fix", "another fact"]
    backend.finalize()


def test_ambiguous_start_posts_changes_unbound(traced_backend, capture_server):
    def responder(path, events):
        if any(e["type"] == "memory_generate_start" for e in events):
            return 500, {"error": "flaky"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend(annotate_retries=0)
    backend.set_task("task")
    backend.record([{"role": "user", "content": "remember the fix"}], step=1)
    backend._extract(2)
    # The change/end still post (no cursor to guess at), carrying no binding.
    changes = capture_server.events("memory_change")
    ends = capture_server.events("memory_generate_end")
    assert len(changes) == 1 and len(ends) == 1
    assert "binding" not in changes[0] and "binding" not in ends[0]
    backend.finalize()


def _change_posts(server):
    return [r for r in server.requests if any(e["type"] == "memory_change" for e in r["events"])]


def test_changes_chunked_under_the_event_cap(traced_backend, capture_server):
    """One flush with more changes than the recorder's per-request event cap
    posts sequential chunks; the end still declares the full native count."""
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": f"fact {i}"} for i in range(300)], step=1)
    backend._extract(2)
    assert [len(r["events"]) for r in _change_posts(capture_server)] == [256, 44]
    changes = _changes(capture_server)
    assert (changes[0]["change_index"], changes[-1]["change_index"]) == (0, 299)
    assert all(c["change_count"] == 300 for c in changes)
    (end,) = _ends(capture_server)
    assert end["change_count"] == 300 and end["status"] == "completed"
    backend.finalize()


def test_changes_chunked_under_the_byte_budget(traced_backend, capture_server):
    """Chunks also respect the recorder's 1 MiB body cap: four ~400 KiB refs
    cannot share one body, so they split — and every posted body fits."""
    backend = traced_backend(max_message_chars=500_000)
    backend.set_task("task")
    big = "x" * 400_000
    backend.record([{"role": "user", "content": big} for _ in range(4)], step=1)
    backend._extract(2)
    posts = _change_posts(capture_server)
    assert [len(r["events"]) for r in posts] == [2, 2]
    for request in posts:
        assert len(json.dumps({"events": request["events"]})) <= 1024 * 1024
    (end,) = _ends(capture_server)
    assert end["change_count"] == 4
    backend.finalize()


def test_oversize_start_degrades_inputs_to_digests(traced_backend, capture_server):
    """A start event over the recorder's 1 MiB body cap must not take the
    whole extraction untraced: every input degrades to its digest-only ref
    (identity kept, bulk dropped), the start is accepted, and the operation
    traces through its end."""
    backend = traced_backend(max_message_chars=500_000)
    backend.set_task("task")
    big = "x" * 400_000
    backend.record([{"role": "user", "content": big} for _ in range(4)], step=1)
    backend._extract(2)
    (start,) = capture_server.events("memory_generate_start")
    contents = [item["content"] for item in start["payload"]["inputs"]]
    assert contents == [
        {"availability": "unavailable", "reason": "oversize", "sha256": text_sha256(big)}
    ] * 4
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_inputs_degraded"]
    assert record["op"] == "generate_start" and record["step"] == 2
    # The operation still traced to completion, native extraction included.
    assert len(capture_server.events("memory_change")) == 4
    (end,) = _ends(capture_server)
    assert end["status"] == "completed" and end["change_count"] == 4
    assert backend.system.rows == [big] * 4
    backend.finalize()


def test_rejected_change_post_abandons_the_operation(traced_backend, capture_server):
    """A definitive rejection of a change batch (the recorder's commit is
    atomic, so nothing of the batch landed) leaves an open interval that can
    never close honestly: no end event posts, the memory lane is disabled for
    the session, and the native extraction is untouched."""
    def responder(path, events):
        if any(e["type"] == "memory_change" for e in events):
            return 413, {"error": "too large"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "remember the fix"}], step=1)
    backend._extract(2)
    assert capture_server.events("memory_generate_end") == []
    assert backend.system.rows == ["remember the fix"]  # native work ran
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_change_rejected"]
    assert record["op"] == "change" and record["step"] == 2 and record["status"] == 413
    assert backend._trace.memory_lane_enabled is False
    assert backend._trace.pending_inputs == []
    posts_before = len(capture_server.requests)
    # The lane stays disabled: later extractions post nothing, still extract natively.
    backend.record([{"role": "user", "content": "another fact"}], step=3)
    backend._extract(4)
    assert len(capture_server.requests) == posts_before
    assert backend.system.rows == ["remember the fix", "another fact"]
    backend.finalize()


def test_ambiguous_change_post_still_posts_the_end(traced_backend, capture_server):
    """An ambiguous change-post failure (5xx/transport) may or may not have
    landed: the operation keeps posting and the recorder's change-series
    check owns any gap report."""
    def responder(path, events):
        if any(e["type"] == "memory_change" for e in events):
            return 500, {"error": "flaky"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend(annotate_retries=0)
    backend.set_task("task")
    backend.record([{"role": "user", "content": "remember the fix"}], step=1)
    backend._extract(2)
    (end,) = _ends(capture_server)
    assert end["change_count"] == 1 and end["status"] == "completed"
    assert backend._trace.memory_lane_enabled is True
    backend.finalize()


def test_main_lane_rejects_an_uncheckable_explicit_url(tmp_path, capture_server):
    """With no main-lane model URL to check against, an explicit (stale) main
    annotate URL resolves to nothing and tracing stays off — only the memory
    lane may resolve from an explicit URL alone."""
    backend = FakeBackend(
        _config(
            tmp_path / "inst",
            annotate_main_url=capture_server.annotate_url("MAIN"),
            annotate_memory_url=capture_server.annotate_url("MEMORY"),
        ),
        model_base_url="",
    )
    backend.start()
    assert backend._trace is None
    backend.set_task("task")
    assert capture_server.requests == []  # nothing posted anywhere
    backend.finalize()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def test_search_start_end_roundtrip(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]
    recall = backend.recall_context(planned_step=3)
    assert recall is not None and recall["n_memories"] == 1
    (start,) = capture_server.events("memory_search_start")
    payload = start["payload"]
    assert payload["query"]["text"] == "fix the bug"
    assert payload["query"]["sha256"] == text_sha256("fix the bug")
    assert payload["main_step"] == 3
    assert payload["requested_by"] == "main" and payload["handled_by"] == "memory"
    assert payload["extensions"]["fake"] == {
        "session_id": backend._session_id,
        "planned_step": "3",
        "query_source": "task",
    }
    assert "binding" not in start
    (end,) = capture_server.events("memory_search_end")
    end_payload = end["payload"]
    assert end_payload["operation_id"] == payload["operation_id"]
    assert end_payload["status"] == "completed" and end_payload["error_codes"] == []
    (item,) = end_payload["returned"]
    assert item["ordinal"] == 0 and "score" not in item
    assert item["memory"]["content"]["text"] == "alpha fact"
    fake = end_payload["extensions"]["fake"]
    assert (fake["matched"], fake["selected"], fake["rendered"], fake["budget_dropped"]) == (1, 1, 1, 0)
    # The portable match count: the raw hit count before floor/slice/budget,
    # with the conservative default precision (a top-k native search could
    # have truncated the pool — the fake does not override the hook).
    assert end_payload["matched_count"] == {"value": 1, "precision": "lower_bound"}
    # The end binds at the start cursor (capture server reports 0).
    assert end["binding"] == {"after_role_call_index": 0}
    backend.finalize()


def test_floor_dropped_hit_counts_in_matched_count(traced_backend, capture_server):
    """A hit the relevance floor drops still counts in matched_count.value
    while returned stays empty: the field decouples "what the search matched"
    from "what policy handed back" — the recall-loss-visibility shape."""
    backend = traced_backend(recall_min_score=0.5)
    backend.set_task("fix the bug")
    backend.system.hits = ["weak fact"]
    backend.system.scores = {"weak fact": 0.1}
    assert backend.recall_context(planned_step=1) is None
    (end,) = capture_server.events("memory_search_end")
    assert end["payload"]["returned"] == []
    assert end["payload"]["extensions"]["fake"]["matched"] == 1
    assert end["payload"]["matched_count"] == {"value": 1, "precision": "lower_bound"}
    backend.finalize()


def test_rewritten_query_is_traced_with_its_source(traced_backend, capture_server):
    """A successful rewrite replaces the recall query; the next search start
    posts the rewritten query with query_source "rewritten"."""
    def responder(path, events):
        if path == "/chat/completions":
            return 200, {"choices": [{"message": {"content": '{"query": "the current blocker"}'}, "finish_reason": "stop"}]}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend(
        rewrite_every_n_steps=1,
        rewrite_model="q-model",
        rewrite_base_url=capture_server.url,
        rewrite_api_key="k",
    )
    backend.set_task("the original task")
    backend.system.hits = ["alpha fact"]
    backend.maybe_rewrite(1)
    assert backend._current_query == "the current blocker"
    recall = backend.recall_context(planned_step=2)
    assert recall is not None
    (start,) = capture_server.events("memory_search_start")
    assert start["payload"]["query"]["text"] == "the current blocker"
    assert start["payload"]["extensions"]["fake"]["query_source"] == "rewritten"
    backend.finalize()


def test_empty_search_is_traced_with_empty_returned(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("fix the bug")
    assert backend.recall_context(planned_step=1) is None
    (end,) = capture_server.events("memory_search_end")
    assert end["payload"]["returned"] == []
    assert end["payload"]["status"] == "completed"
    assert end["payload"]["extensions"]["fake"]["matched"] == 0
    assert capture_server.events("memory_delivery") == []
    backend.finalize()


def test_failed_search_posts_failed_end(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("fix the bug")
    backend.system.search_error = RuntimeError("search boom")
    assert backend.recall_context(planned_step=1) is None  # contained
    assert backend._counts["backend_errors"] == 1
    (end,) = capture_server.events("memory_search_end")
    assert end["payload"]["status"] == "failed"
    assert end["payload"]["error_codes"] == ["RuntimeError"]
    assert end["payload"]["returned"] == []
    # A failed search has no real match count — the placeholder 0 must not be
    # fabricated into the portable field.
    assert "matched_count" not in end["payload"]
    assert backend._trace.pending_search is None  # a failed search anchors nothing
    backend.finalize()


# ---------------------------------------------------------------------------
# Anchor gate and delivery
# ---------------------------------------------------------------------------
def test_rejected_search_start_anchors_no_delivery(traced_backend, capture_server):
    """A search whose START the recorder definitively rejected must not anchor
    a delivery even when the end post lands — while a plain 4xx is not a
    recovery conflict, so the memory lane stays up. Native recall is unaffected."""
    def responder(path, events):
        if any(e["type"] == "memory_search_start" for e in events):
            return 400, {"error": "bad payload"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend()
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]
    recall = backend.recall_context(planned_step=1)
    assert recall is not None and recall["n_memories"] == 1
    assert backend._trace.pending_search is None
    assert backend._trace.memory_lane_enabled is True
    backend.deliver_recall(recall, step=1, msg_index=0, cursor=0)
    assert capture_server.events("memory_delivery") == []
    backend.finalize()


def test_rejected_search_end_anchors_no_delivery(traced_backend, capture_server):
    """A search whose end post was not accepted must not anchor a delivery:
    the delivery would cite a search_operation_id whose end never recorded."""
    def responder(path, events):
        if any(e["type"] == "memory_search_end" for e in events):
            return 500, {"error": "down"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend(annotate_retries=0)
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]
    recall = backend.recall_context(planned_step=1)
    assert recall is not None and recall["n_memories"] == 1
    assert backend._trace.pending_search is None
    backend.deliver_recall(recall, step=1, msg_index=0, cursor=0)
    assert capture_server.events("memory_delivery") == []
    backend.finalize()


def test_cached_delivery_cites_the_prior_search(traced_backend, capture_server):
    """A cache-hit step posts no search events; its delivery cites the prior
    accepted search's operation_id with a fresh delivery_id, carries
    cached: true, and its refs are the cited search's returned set."""
    backend = traced_backend()
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]
    recall = backend.recall_context(planned_step=1)
    cursor = backend.main_lane_cursor()
    backend.deliver_recall(recall, step=1, msg_index=4, cursor=cursor)
    search_id = capture_server.events("memory_search_start")[0]["payload"]["operation_id"]

    cached = backend.recall_context(planned_step=2)
    assert cached["cached"] is True
    # No search events posted for the cache hit (only the cursor read flows).
    assert len(capture_server.events("memory_search_start")) == 1
    assert len(capture_server.events("memory_search_end")) == 1
    cursor = backend.main_lane_cursor()
    backend.deliver_recall(cached, step=2, msg_index=4, cursor=cursor)
    deliveries = capture_server.events("memory_delivery")
    assert len(deliveries) == 2
    fresh, cached_delivery = (d["payload"] for d in deliveries)
    assert cached_delivery["delivery_id"] != fresh["delivery_id"]  # a fresh id per delivery
    assert cached_delivery["search_operation_id"] == search_id == fresh["search_operation_id"]
    assert cached_delivery["extensions"]["fake"]["cached"] is True
    assert "cached" not in fresh["extensions"]["fake"]
    # The subsequence rule holds: the memoized refs are exactly the cited
    # search's returned set.
    returned = capture_server.events("memory_search_end")[0]["payload"]["returned"]
    assert [m["version_id"] for m in cached_delivery["memories"]] == [
        item["memory"]["version_id"] for item in returned
    ]
    backend.finalize()


def test_unrecorded_search_payload_never_cites_an_older_search(traced_backend, capture_server):
    """A fresh search whose END post fails still renders and caches its
    payload — but with a None anchor, so the cache-hit delivery dangles
    instead of citing an older search whose returned set may differ."""
    def flaky_end(path, events):
        if any(e["type"] == "memory_search_end" for e in events):
            return 500, {"error": "down"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = flaky_end
    backend = traced_backend(annotate_retries=0)
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]
    recall = backend.recall_context(planned_step=1)
    assert recall is not None
    assert backend._trace.pending_search is None  # the unconfirmed end anchors nothing
    cursor = backend.main_lane_cursor()
    backend.deliver_recall(recall, step=1, msg_index=4, cursor=cursor)
    assert capture_server.events("memory_delivery") == []

    cached = backend.recall_context(planned_step=2)  # cache hit on that payload
    assert cached["cached"] is True
    backend.deliver_recall(cached, step=2, msg_index=4, cursor=backend.main_lane_cursor())
    assert capture_server.events("memory_delivery") == []  # no stale citation
    backend.finalize()


def test_delivery_posts_with_canonical_message_proof(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]
    recall = backend.recall_context(planned_step=1)
    assert backend._trace.pending_search is not None  # the anchor formed
    cursor = backend.main_lane_cursor()
    assert cursor == 0
    backend.deliver_recall(recall, step=2, msg_index=4, cursor=cursor)
    (delivery,) = capture_server.events("memory_delivery")
    payload = delivery["payload"]
    assert (payload["from_role"], payload["to_role"], payload["status"], payload["main_step"]) == (
        "memory",
        "main",
        "placed",
        2,
    )
    assert payload["search_operation_id"] == capture_server.events("memory_search_start")[0]["payload"]["operation_id"]
    placement = payload["placement"]
    assert placement["kind"] == "prompt_message"
    assert placement["proof_kind"] == "canonical_message"
    block = recall["content"]
    assert placement["content"]["text"] == block
    assert placement["content"]["sha256"] == text_sha256(block)
    assert payload["extensions"]["fake"] == {"session_id": backend._session_id, "delivered": 1, "msg_index": 4}
    assert delivery["binding"] == {
        "after_role_call_index": 0,
        "proofs": [{"kind": "canonical_message", "msg_index": 4, "content_sha256": text_sha256(block)}],
    }
    (mem_ref,) = payload["memories"]
    # Delivery memories are an ordered subsequence (here: exactly) the search's.
    returned = capture_server.events("memory_search_end")[0]["payload"]["returned"]
    assert [item["memory"]["version_id"] for item in returned] == [mem_ref["version_id"]]
    backend.finalize()


def test_missing_cursor_skips_delivery_with_log_record(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]
    recall = backend.recall_context(planned_step=1)
    backend.deliver_recall(recall, step=2, msg_index=None, cursor=None)
    assert capture_server.events("memory_delivery") == []
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_delivery_no_cursor"]
    assert record["op"] == "delivery" and record["step"] == 2 and record["search_operation_id"]
    assert backend._trace.delivery_enabled is True  # a per-delivery degradation
    backend.finalize()


def test_unconfirmed_delivery_disables_delivery_tracing_only(traced_backend, capture_server):
    def responder(path, events):
        if any(e["type"] == "memory_delivery" for e in events):
            return 500, {"error": "down"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend(annotate_retries=0)
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]
    recall = backend.recall_context(planned_step=1)
    backend.deliver_recall(recall, step=2, msg_index=4, cursor=0)
    assert len(capture_server.events("memory_delivery")) == 1  # no 5xx retry
    assert backend._trace.delivery_enabled is False
    assert backend._trace.memory_lane_enabled is True  # searches keep flowing
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_delivery_unconfirmed"]
    assert record["op"] == "delivery" and record["status"] == 500
    # Once disabled, no later delivery cursor read is attempted at all.
    assert backend.main_lane_cursor() is None
    backend.finalize()


def test_rejected_delivery_disables_with_distinct_reason(traced_backend, capture_server):
    def responder(path, events):
        if any(e["type"] == "memory_delivery" for e in events):
            return 409, {"error": "annotation_conflict"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend()
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]
    recall = backend.recall_context(planned_step=1)
    backend.deliver_recall(recall, step=2, msg_index=4, cursor=0)
    assert len(capture_server.events("memory_delivery")) == 1  # 4xx is never retried
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_delivery_rejected"]
    assert record["status"] == 409
    assert backend._trace.delivery_enabled is False
    backend.finalize()


# ---------------------------------------------------------------------------
# Containment on the real machinery
# ---------------------------------------------------------------------------
def test_search_post_failure_keeps_native_recall(traced_backend, capture_server):
    """A failing search-start post (ambiguous 5xx) leaves the native recall
    intact and uncounted — annotation failures never become backend errors."""
    def responder(path, events):
        if any(e["type"] == "memory_search_start" for e in events):
            return 500, {"error": "down"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend(annotate_retries=0)
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]
    recall = backend.recall_context(planned_step=1)
    assert recall is not None and recall["n_memories"] == 1
    assert backend._counts["backend_errors"] == 0
    backend.finalize()


def test_annotator_raise_is_contained(traced_backend, capture_server, monkeypatch):
    """A raising annotator (a client bug, not an HTTP failure) is contained at
    every recall exit: the computed recall is still delivered and nothing is
    counted."""
    backend = traced_backend()
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]

    def boom(url, events):
        raise RuntimeError("annotator boom")

    monkeypatch.setattr(Annotator, "post", boom)
    recall = backend.recall_context(planned_step=1)  # search-begin raise contained
    assert recall is not None and recall["n_memories"] == 1
    assert backend._counts["backend_errors"] == 0
    backend.finalize()


def test_search_end_raise_keeps_native_recall(traced_backend, capture_server, monkeypatch):
    """A raise inside the search-end path (here: a broken ref derivation) is
    contained on the spot: the computed recall is delivered, no backend error
    is counted, and no second corrupting search_end follows."""
    backend = traced_backend()
    backend.set_task("fix the bug")
    backend.system.hits = ["alpha fact"]

    def bad_ref(obj):
        raise RuntimeError("ref boom")

    monkeypatch.setattr(backend, "_memory_ref", bad_ref)
    recall = backend.recall_context(planned_step=1)
    assert recall is not None and recall["n_memories"] == 1
    assert backend._counts["backend_errors"] == 0
    assert capture_server.events("memory_search_end") == []
    backend.finalize()

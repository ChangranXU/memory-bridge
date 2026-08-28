"""Step 5 (PLAN §6.6): recall traced as search operations, and the transient
placement as a main-lane delivery with a canonical-message proof.

Backend-level tests drive recall_context against the real capture server;
agent-level tests run full offline episodes through a trajectory-scoped
OPENAI_BASE_URL and assert the delivery chain — plus the recovery rules and
the wall-time exclusion."""

import json

import pytest
from minisweagent.models.test_models import DeterministicToolcallModel

from conftest import (
    SUBMIT_COMMAND,
    CapturingToolcallModel,
    approved_candidate,
    make_crashing_model,
    on_tool,
)

from shared_bridge.annotate import text_sha256


def _traced_memory(tmp_path, server, **overrides):
    return {
        "enabled": True,
        "scope": "instance",
        "output_dir": str(tmp_path / "inst"),
        "extract_every_n_steps": 1,
        "extract_model": "fake-extract-model",
        "extract_base_url": server.lane_url("EXTRACT"),
        "extract_api_key": "k",
        **overrides,
    }


def _recall_script(make_bash_output, trigger="trigger_recall"):
    return [
        make_bash_output("s0", [f"echo {trigger}"]),
        make_bash_output("s1", ["echo step1"]),
        make_bash_output("s2", [SUBMIT_COMMAND]),
    ]


def _approve_recall_key(fake_client):
    fake_client.rules.append(
        on_tool(
            "trigger_recall",
            lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "recall_key", "remembered fact xyz")),
        )
    )


def _seed(backend, fake_client, *candidates):
    backend.record([{"role": "user", "content": "remember recall_key"}], step=1)
    fake_client.queue.append({"candidates": list(candidates), "deletions": [], "rejections": []})
    backend._extract(2)


def _transient(messages):
    return [m for m in messages if m.get("extra", {}).get("transient_recall")]


# ---------------------------------------------------------------------------
# Search operation shape (backend level)
# ---------------------------------------------------------------------------
def test_search_start_end_roundtrip(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("fix the recall_key bug")
    _seed(backend, fake_client, approved_candidate(1, "recall_key", "remembered fact xyz"))
    recall = backend.recall_context(planned_step=3)
    assert recall is not None and recall["n_memories"] == 1
    (start,) = capture_server.events("memory_search_start")
    payload = start["payload"]
    assert payload["query"]["text"] == "fix the recall_key bug"
    assert payload["query"]["sha256"] == text_sha256("fix the recall_key bug")
    assert payload["main_step"] == 3
    assert payload["requested_by"] == "main" and payload["handled_by"] == "memory"
    assert "binding" not in start
    (end,) = capture_server.events("memory_search_end")
    end_payload = end["payload"]
    assert end_payload["operation_id"] == payload["operation_id"]
    assert end_payload["status"] == "completed" and end_payload["error_codes"] == []
    (item,) = end_payload["returned"]
    assert item["ordinal"] == 0 and "score" not in item
    ref = item["memory"]
    (row,) = backend._system.memory_search("minisweagent", query=None, review_status=None)
    assert ref["version_id"].startswith(f"{row.id}:")
    assert ref["extensions"]["cure"]["key"] == "recall_key"
    cure = end_payload["extensions"]["cure"]
    assert (cure["matched"], cure["selected"], cure["rendered"], cure["budget_dropped"]) == (1, 1, 1, 0)
    # The native search is an unbounded full scan, so len(hits) is the true
    # match count — precision "exact", never a lower bound.
    assert end_payload["matched_count"] == {"value": 1, "precision": "exact"}
    # The end binds at the start cursor (capture server reports 0).
    assert end["binding"] == {"after_role_call_index": 0}
    # Native-stable join: the search returns the very version generation produced.
    generation_end = capture_server.events("memory_generate_end")[0]["payload"]
    assert [r["version_id"] for r in generation_end["produced"]] == [ref["version_id"]]
    backend.finalize()


def test_empty_search_is_traced_with_empty_returned(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("fix the recall_key bug")
    assert backend.recall_context(planned_step=1) is None
    (end,) = capture_server.events("memory_search_end")
    assert end["payload"]["returned"] == []
    assert end["payload"]["status"] == "completed"
    assert end["payload"]["extensions"]["cure"]["matched"] == 0
    assert capture_server.events("memory_delivery") == []
    backend.finalize()


def test_post_finalize_recall_posts_no_search_trace(capture_server, fake_client, traced_backend):
    """Post-finalize dormancy fires BEFORE the tracing hooks: a stray recall
    posts no fabricated search operation for a search that never runs."""
    backend = traced_backend()
    backend.set_task("fix the recall_key bug")
    backend.finalize()
    assert backend.recall_context(planned_step=3) is None
    assert capture_server.events("memory_search_start") == []
    assert capture_server.events("memory_search_end") == []


def test_budget_drop_is_counted_not_hidden(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("fix the recall_key bug")
    # CURE recall scores key/value text against the task terms, so both
    # candidates must literally contain a term of 'fix the recall_key bug'.
    _seed(
        backend,
        fake_client,
        approved_candidate(1, "recall_key", "remembered fact xyz"),
        approved_candidate(1, "recall_key_two", "second remembered fact"),
    )
    full = backend.recall_context(planned_step=3)
    assert full["n_memories"] == 2
    # A lines budget that fits exactly the first memory line drops the second
    # (the header is excluded from the budget).
    second_line_start = full["content"].index("\n- [", full["content"].index("\n- [") + 1) + 1
    first_line_len = second_line_start - (full["content"].index("\n- [") + 1) - 1
    backend.config.max_total_recall_chars = first_line_len
    backend._mark_store_changed()  # the dirty-flag cache would otherwise serve the old render
    limited = backend.recall_context(planned_step=4)
    assert limited["n_memories"] == 1
    assert limited["content"] == full["content"][: second_line_start - 1]
    ends = capture_server.events("memory_search_end")
    cure = ends[-1]["payload"]["extensions"]["cure"]
    assert (cure["matched"], cure["selected"], cure["rendered"], cure["budget_dropped"]) == (2, 2, 1, 1)
    assert len(ends[-1]["payload"]["returned"]) == 1
    # The seq-279 shape: matched 2, returned 1 — the portable field keeps the
    # true match count visible to consumers that read returned alone.
    assert ends[-1]["payload"]["matched_count"] == {"value": 2, "precision": "exact"}
    backend.finalize()


def test_floor_dropped_hit_counts_in_matched_count(capture_server, fake_client, traced_backend):
    """The seq-214 shape: the relevance floor drops the only hit, returned
    stays empty, and matched_count still records the match — the recall loss
    stays visible in the trace instead of reading as "nothing matched"."""
    backend = traced_backend(recall_min_score=2.0)
    backend.set_task("fix the recall_key bug")
    _seed(backend, fake_client, approved_candidate(1, "recall_key", "remembered fact xyz"))
    # The only term of the query the row matches is "recall_key" (score 1).
    assert backend.recall_context(planned_step=3) is None
    (end,) = capture_server.events("memory_search_end")
    assert end["payload"]["returned"] == []
    assert end["payload"]["extensions"]["cure"]["selected"] == 0
    assert end["payload"]["matched_count"] == {"value": 1, "precision": "exact"}
    backend.finalize()


def test_search_start_409_disables_lane_but_not_native_recall(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch, backend_spy
):
    def responder(path, events):
        if any(e["type"] == "memory_search_start" for e in events):
            return 409, {"error": "annotation_conflict"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(model, memory=_traced_memory(tmp_path, capture_server), cost_limit=100.0)
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"
    # Native recall kept working untraced: the marker still reached the model.
    assert len(_transient(model.captured[1])) == 1
    assert len(_transient(model.captured[2])) == 1
    # One rejected start, no ends, no deliveries; the lane stayed off.
    assert len(capture_server.events("memory_search_start")) == 1
    assert capture_server.events("memory_search_end") == []
    assert capture_server.events("memory_delivery") == []
    backend = backend_spy[0]
    assert backend._trace is not None and backend._trace.memory_lane_enabled is False
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_recovery_conflict"]
    assert record["op"] == "search_start"
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["counts"]["recall_injections"] == 2


def test_rejected_search_start_anchors_no_delivery(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch, backend_spy
):
    """A search whose START the recorder definitively rejected (a 4xx other
    than the recovery/oversize codes) must not anchor a delivery even when the
    end post lands: the citation needs the whole operation recorded. Native
    recall is unaffected either way."""
    def responder(path, events):
        if any(e["type"] == "memory_search_start" for e in events):
            return 400, {"error": "bad payload"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(
        model, memory=_traced_memory(tmp_path, capture_server, annotate_retries=0), cost_limit=100.0
    )
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"
    # Native recall kept working: both transient blocks reached the model.
    assert len(_transient(model.captured[1])) == 1
    assert len(_transient(model.captured[2])) == 1
    # No delivery was even attempted (the anchor never formed); a plain 4xx is
    # not a recovery conflict, so the memory lane stayed up for later posts.
    assert capture_server.events("memory_delivery") == []
    backend = backend_spy[0]
    assert backend._trace.pending_search is None
    assert backend._trace.memory_lane_enabled is True
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["counts"]["recall_injections"] == 2


def test_rejected_search_end_anchors_no_delivery(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch, backend_spy
):
    """A search whose end post was not accepted must not anchor a delivery:
    the delivery would cite a search_operation_id whose end never recorded.
    Native recall is unaffected either way."""
    def responder(path, events):
        if any(e["type"] == "memory_search_end" for e in events):
            return 500, {"error": "down"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(
        model, memory=_traced_memory(tmp_path, capture_server, annotate_retries=0), cost_limit=100.0
    )
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"
    # Native recall kept working: both transient blocks reached the model.
    assert len(_transient(model.captured[1])) == 1
    assert len(_transient(model.captured[2])) == 1
    # No delivery was even attempted (the anchor never formed) ...
    assert capture_server.events("memory_delivery") == []
    backend = backend_spy[0]
    assert backend._trace.pending_search is None
    # ... while search starts/ends kept flowing on the memory lane (the lane
    # stays up: a 5xx on the end post is ambiguous, not a recovery conflict).
    assert len(capture_server.events("memory_search_start")) == 3
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["counts"]["recall_injections"] == 2


# ---------------------------------------------------------------------------
# Delivery (agent level)
# ---------------------------------------------------------------------------
def _counting_cursor_responder(capture_server):
    """Each cursor-only (empty) read returns the next main-lane cursor."""
    cursors = iter(range(1, 1000))

    def responder(path, events):
        cursor = next(cursors) if not events else 0
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": cursor}

    capture_server.responder = responder


def test_query_posts_delivery_with_canonical_message_proof(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch
):
    _counting_cursor_responder(capture_server)
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(model, memory=_traced_memory(tmp_path, capture_server), cost_limit=100.0)
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"

    deliveries = capture_server.events("memory_delivery")
    assert len(deliveries) == 2  # queries 2 and 3; query 1 ran before any memory
    search_starts = capture_server.events("memory_search_start")
    search_ends = capture_server.events("memory_search_end")
    delivery = deliveries[0]
    payload = delivery["payload"]
    assert (payload["from_role"], payload["to_role"], payload["status"], payload["main_step"]) == (
        "memory",
        "main",
        "placed",
        2,
    )
    assert payload["search_operation_id"] == search_starts[1]["payload"]["operation_id"]
    placement = payload["placement"]
    assert placement["kind"] == "prompt_message"
    assert placement["proof_kind"] == "canonical_message"
    # The block sits at msg_index in the recorded request: 4 messages
    # (system, instance, assistant, tool) precede the transient marker.
    captured = model.captured[1]
    assert captured[4]["extra"]["transient_recall"] is True
    block = captured[4]["content"]
    assert placement["content"]["text"] == block
    assert placement["content"]["sha256"] == text_sha256(block)
    assert payload["extensions"]["cure"]["msg_index"] == 4
    assert delivery["binding"] == {
        "after_role_call_index": 1,
        "proofs": [{"kind": "canonical_message", "msg_index": 4, "content_sha256": text_sha256(block)}],
    }
    (mem_ref,) = payload["memories"]
    assert mem_ref["extensions"]["cure"]["key"] == "recall_key"
    # Delivery memories are an ordered subsequence (here: exactly) the search's.
    returned = search_ends[1]["payload"]["returned"]
    assert [item["memory"]["version_id"] for item in returned] == [mem_ref["version_id"]]
    # The second delivery binds at the next cursor read, for step 3.
    assert deliveries[1]["binding"]["after_role_call_index"] == 2
    assert deliveries[1]["payload"]["main_step"] == 3
    # Native behavior unchanged: transient never persisted.
    assert _transient(agent.messages) == []


def test_preflight_blocked_attempt_records_search_but_no_delivery(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch
):
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = DeterministicToolcallModel(outputs=_recall_script(make_bash_output))
    agent = make_agent(model, memory=_traced_memory(tmp_path, capture_server), step_limit=1)
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "LimitsExceeded"
    assert capture_server.events("memory_delivery") == []
    ends = capture_server.events("memory_search_end")
    assert len(ends) == 2  # query 1 (empty) + the blocked attempt (one ref)
    assert ends[0]["payload"]["returned"] == []
    (item,) = ends[1]["payload"]["returned"]
    assert item["memory"]["extensions"]["cure"]["key"] == "recall_key"


def test_model_crash_without_proxy_visible_call_posts_no_delivery(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch, backend_spy
):
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = make_crashing_model(_recall_script(make_bash_output), crash_after=1)
    agent = make_agent(model, memory=_traced_memory(tmp_path, capture_server), cost_limit=100.0)
    with pytest.raises(RuntimeError, match="model boom"):
        agent.run("fix the recall_key bug")
    # The query crashed client-side after n_calls incremented: the lane cursor
    # never advanced, so no delivery posts — a provable "placed" claim bound to
    # an empty interval would record no_call, a structural problem for
    # retrieval. The recall was still accounted, and delivery tracing stays on.
    assert capture_server.events("memory_delivery") == []
    backend = backend_spy[0]
    assert backend._trace.delivery_enabled is True
    assert backend._counts["recall_injections"] == 1


def test_model_crash_after_call_landed_still_posts_delivery(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch
):
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)

    def landed_crash():
        # The query's request folded before failing: the lane cursor advanced.
        capture_server.cursor = 1
        return RuntimeError("model boom")

    model = make_crashing_model(
        _recall_script(make_bash_output), crash_after=1, exception_factory=landed_crash
    )
    agent = make_agent(model, memory=_traced_memory(tmp_path, capture_server), cost_limit=100.0)
    with pytest.raises(RuntimeError, match="model boom"):
        agent.run("fix the recall_key bug")
    # The crash is server-side: the failed call is proxy-visible, so the
    # delivery posts bound at the pre-call cursor — exactly the §6.6 semantics.
    (delivery,) = capture_server.events("memory_delivery")
    assert delivery["payload"]["status"] == "placed"
    assert delivery["binding"]["after_role_call_index"] == 0
    assert delivery["payload"]["extensions"]["cure"]["msg_index"] == 4


def test_unconfirmed_delivery_disables_delivery_tracing_only(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch, backend_spy
):
    def responder(path, events):
        if any(e["type"] == "memory_delivery" for e in events):
            return 500, {"error": "down"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(
        model, memory=_traced_memory(tmp_path, capture_server, annotate_retries=0), cost_limit=100.0
    )
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"
    # Exactly one delivery post (no 5xx retry), then delivery tracing stopped;
    # searches kept flowing on the memory lane.
    assert len(capture_server.events("memory_delivery")) == 1
    assert len(capture_server.events("memory_search_end")) == 3
    backend = backend_spy[0]
    assert backend._trace.delivery_enabled is False
    assert backend._trace.memory_lane_enabled is True
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_delivery_unconfirmed"]
    assert record["op"] == "delivery" and record["status"] == 500
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["counts"]["recall_injections"] == 2


def test_unconfirmed_delivery_suppresses_later_cursor_reads(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch, backend_spy
):
    def responder(path, events):
        if any(e["type"] == "memory_delivery" for e in events):
            return 500, {"error": "down"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(
        model, memory=_traced_memory(tmp_path, capture_server, annotate_retries=0), cost_limit=100.0
    )
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"
    backend = backend_spy[0]
    assert backend._trace.delivery_enabled is False
    # PLAN §8.2: after an unconfirmed delivery no later delivery cursor read is
    # attempted. Two recalls placed (the sibling test counts 2 injections), but
    # only the first — before the disable — may post the empty-events cursor
    # read to the main lane.
    cursor_reads = [r for r in capture_server.requests if "/MAIN/" in r["path"] and r["events"] == []]
    assert len(cursor_reads) == 1


def test_rejected_delivery_disables_with_distinct_reason(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch, backend_spy
):
    def responder(path, events):
        if any(e["type"] == "memory_delivery" for e in events):
            return 409, {"error": "annotation_conflict"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(model, memory=_traced_memory(tmp_path, capture_server), cost_limit=100.0)
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"
    assert len(capture_server.events("memory_delivery")) == 1  # 4xx is never retried
    backend = backend_spy[0]
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_delivery_rejected"]
    assert record["status"] == 409
    assert backend._trace.delivery_enabled is False


def test_failed_cursor_read_skips_delivery_with_log_record(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch, backend_spy
):
    def responder(path, events):
        if "/MAIN/" in path and events == []:
            return 500, {"error": "down"}  # the cursor-only read
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(
        model, memory=_traced_memory(tmp_path, capture_server, annotate_retries=0), cost_limit=100.0
    )
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"
    # Native recall kept working: both transient blocks reached the model.
    assert len(_transient(model.captured[1])) == 1
    assert len(_transient(model.captured[2])) == 1
    # No delivery could bind, but the skip is a per-delivery degradation:
    # delivery tracing stays on and searches keep flowing on the memory lane.
    assert capture_server.events("memory_delivery") == []
    assert len(capture_server.events("memory_search_end")) == 3
    backend = backend_spy[0]
    assert backend._trace.delivery_enabled is True
    # The skip is visible in artifacts like every other degradation path.
    records = [e for e in backend._events if e.get("reason") == "annotation_delivery_no_cursor"]
    assert len(records) == 2  # queries 2 and 3, matching the two placed blocks
    assert all(r["op"] == "delivery" and r["search_operation_id"] for r in records)
    assert [r["step"] for r in records] == [2, 3]
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    persisted = [e for e in data["events"] if e.get("reason") == "annotation_delivery_no_cursor"]
    assert len(persisted) == 2
    assert data["counts"]["recall_injections"] == 2


def test_annotation_io_time_is_excluded_from_wall_clock(
    tmp_path, make_agent, make_bash_output, capture_server, fake_client, monkeypatch
):
    monkeypatch.setenv("OPENAI_BASE_URL", capture_server.lane_url("MAIN"))
    _approve_recall_key(fake_client)
    model = DeterministicToolcallModel(outputs=_recall_script(make_bash_output))
    agent = make_agent(model, memory=_traced_memory(tmp_path, capture_server), cost_limit=100.0)
    start_time_before = agent._start_time
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"
    # Every query consumed the annotation seconds accumulated since the
    # previous one and added them to _start_time: real HTTP annotation I/O
    # happened, so the inherited wall clock moved forward by that amount.
    assert agent._start_time > start_time_before

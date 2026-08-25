"""Step 4 (PLAN §6.2/§6.3): annotation URL resolution, the stdlib annotator,
session/bind posting, and URL sanitization — all against a real local HTTP
capture server, never transport mocks."""

import hashlib
import json
import logging

from conftest import TEST_TRAJECTORY_ID, approved_candidate

from shared_bridge.annotate import (
    Annotator,
    canonical_json_sha256,
    derive_annotate_url,
    endpoints_compatible,
    resolve_lane_url,
    sanitize_url,
    text_sha256,
)


def _event(event_type="memory_session", annotation_id="11111111-2222-4333-8444-555555555555"):
    return {"type": event_type, "annotation_id": annotation_id, "payload": {}}


# ---------------------------------------------------------------------------
# URL resolution and sanitization (pure)
# ---------------------------------------------------------------------------
def test_derive_annotate_url_keeps_reverse_proxy_and_role_segments():
    derived = derive_annotate_url("http://h:1/rev-proxy/MAIN/trajectories/abc123/v1")
    assert derived == "http://h:1/rev-proxy/MAIN/trajectories/abc123/annotate"
    assert derive_annotate_url("http://h:1/trajectories/abc123") == "http://h:1/trajectories/abc123/annotate"
    # Normal provider URLs never match and never receive annotations.
    assert derive_annotate_url("https://api.deepseek.com/v1") is None
    assert derive_annotate_url("") is None


def test_resolve_lane_url_precedence_and_mismatch():
    model_url = "http://h:1/MAIN/trajectories/abc/v1"
    derived = derive_annotate_url(model_url)
    assert resolve_lane_url("", "", model_url) == derived
    assert resolve_lane_url("", derived, model_url) == derived  # env override matching the derivation
    explicit = "http://h:1/MAIN/trajectories/abc/annotate"
    assert resolve_lane_url(explicit, "", model_url) == explicit
    # Explicit must name the same endpoint the model URL derives — an explicit
    # URL for another trajectory, lane, or host never binds.
    assert resolve_lane_url("http://h:1/MAIN/trajectories/OTHER/annotate", "", model_url) is None
    assert resolve_lane_url("http://h:1/EXTRACT/trajectories/abc/annotate", "", model_url) is None
    assert resolve_lane_url("http://evil:9/MAIN/trajectories/abc/annotate", "", model_url) is None
    # An explicit URL with no trajectory-scoped model URL can never be validated.
    assert resolve_lane_url(explicit, "", "https://api.deepseek.com/v1") is None


def test_endpoints_compatible():
    main = "http://h:1/MAIN/trajectories/abc/annotate"
    memory = "http://h:1/EXTRACT/trajectories/abc/annotate"
    assert endpoints_compatible(main, memory) is None
    assert endpoints_compatible(main, main) is not None  # one lane path cannot carry both roles
    other = "http://h:1/EXTRACT/trajectories/xyz/annotate"
    assert "different trajectories" in endpoints_compatible(main, other)
    assert endpoints_compatible(None, memory) is None  # per-lane disablement handled upstream


def test_canonical_json_sha256_handles_lone_surrogates_like_the_recorder():
    """Pinned to the recorder's canonical form (PLAN §4.5): lone-surrogate
    strings (valid JSON, invalid strict UTF-8) digest via surrogatepass
    instead of raising UnicodeEncodeError."""
    value = {"key": "lone\ud800surrogate", "items": ["\udcff", 1.5, None]}
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    assert canonical_json_sha256(value) == hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def test_sanitize_url_hashes_the_bearer_id_and_strips_extras():
    raw = "http://user:pw@h:1/MAIN/trajectories/abc123/v1?x=1#frag"
    hashed = hashlib.sha256(b"abc123").hexdigest()[:16]
    assert sanitize_url(raw) == f"http://h:1/MAIN/trajectories/{hashed}/v1"
    assert sanitize_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"


# ---------------------------------------------------------------------------
# Annotator over real HTTP
# ---------------------------------------------------------------------------
def test_annotator_posts_batch_and_returns_cursor(capture_server):
    capture_server.cursor = 5
    annotator = Annotator(timeout=2.0, retries=0, max_consecutive_errors=3)
    result = annotator.post(capture_server.annotate_url("MAIN"), [_event()])
    assert result.ok and result.cursor == 5
    assert capture_server.requests[0]["events"] == [_event()]
    assert annotator.duration > 0
    assert annotator.consume_duration() > 0
    assert annotator.consume_duration() == 0.0  # consumed


def test_annotator_retries_5xx_reusing_annotation_ids(capture_server):
    calls = []

    def responder(path, events):
        calls.append(events)
        return (500, {"error": "boom"}) if len(calls) == 1 else (202, {"recorded": 1, "duplicates": 0, "role_call_cursor": 0})

    capture_server.responder = responder
    annotator = Annotator(timeout=2.0, retries=1, max_consecutive_errors=3)
    result = annotator.post(capture_server.annotate_url("MAIN"), [_event()])
    assert result.ok
    assert len(capture_server.requests) == 2
    assert capture_server.requests[0]["events"] == capture_server.requests[1]["events"]


def test_annotator_never_retries_validation_4xx(capture_server):
    capture_server.responder = lambda path, events: (400, {"error": "invalid_annotation"})
    annotator = Annotator(timeout=2.0, retries=3, max_consecutive_errors=3)
    result = annotator.post(capture_server.annotate_url("MAIN"), [_event()])
    assert not result.ok and result.status == 400
    assert len(capture_server.requests) == 1


def test_annotator_breaker_trips_and_skips(capture_server):
    capture_server.responder = lambda path, events: (500, {"error": "down"})
    annotator = Annotator(timeout=2.0, retries=0, max_consecutive_errors=2)
    url = capture_server.annotate_url("MAIN")
    assert not annotator.post(url, [_event()]).ok
    assert not annotator.breaker_open
    assert not annotator.post(url, [_event()]).ok
    assert annotator.breaker_open
    skipped = annotator.post(url, [_event()])
    assert skipped.skipped and len(capture_server.requests) == 2  # no further I/O


def test_annotator_dead_endpoint_fails_without_raising():
    annotator = Annotator(timeout=0.2, retries=0, max_consecutive_errors=3)
    result = annotator.post("http://127.0.0.1:9/MAIN/trajectories/abc/annotate", [_event()])
    assert not result.ok and result.status is None


def test_annotator_never_retries_a_malformed_success_body(capture_server):
    # A syntactically successful response with a non-JSON body is not a retry
    # class (connection/5xx only, PLAN §6.3): one attempt, then definitive.
    capture_server.responder = lambda path, events: (202, b"<not json")
    annotator = Annotator(timeout=2.0, retries=3, max_consecutive_errors=3)
    result = annotator.post(capture_server.annotate_url("MAIN"), [_event()])
    assert not result.ok and result.status is None and result.definitive
    assert len(capture_server.requests) == 1


def test_unexpected_failure_logs_one_sanitized_line_without_traceback(caplog):
    # A space in the path makes http.client raise InvalidURL, whose message
    # embeds the raw URL — bearer trajectory ID included. The catch-all must
    # log the sanitized URL and the exception type only, never the traceback.
    url = "http://127.0.0.1:9/MAIN/trajectories/bad id/annotate"
    annotator = Annotator(timeout=0.2, retries=0, max_consecutive_errors=3)
    with caplog.at_level(logging.WARNING, logger="shared_bridge.annotate"):
        result = annotator.post(url, [_event()])
    assert not result.ok
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    (record,) = [r for r in records if "unexpectedly" in r.getMessage()]
    assert record.exc_info is None
    assert "InvalidURL" in record.getMessage()
    for r in records:
        assert "bad id" not in r.getMessage()


# ---------------------------------------------------------------------------
# Backend wiring: session, bindings, pending inputs, sanitization
# ---------------------------------------------------------------------------
def test_session_and_binds_posted_with_exact_task(tmp_path, capture_server, traced_backend):
    backend = traced_backend()
    backend.set_task("fix the rolling regression")
    (session,) = capture_server.events("memory_session")
    payload = session["payload"]
    assert payload["task"]["text"] == "fix the rolling regression"
    assert payload["task"]["sha256"] == text_sha256("fix the rolling regression")
    assert payload["adapter"]["name"] == "cure"
    assert payload["extensions"]["cure"]["session_id"] == backend._session_id
    assert payload["extensions"]["cure"]["user_id"] == "minisweagent"
    binds = capture_server.events("memory_role_bind")
    assert [(b["payload"]["logical_role"], b["payload"]["trace_session_id"]) for b in binds] == [
        ("main", payload["trace_session_id"]),
        ("memory", payload["trace_session_id"]),
    ]
    # Session + main bind went to the main endpoint, the memory bind to its own.
    main_posts = [r for r in capture_server.requests if "/MAIN/" in r["path"]]
    memory_posts = [r for r in capture_server.requests if "/EXTRACT/" in r["path"]]
    assert len(main_posts) == 1 and len(main_posts[0]["events"]) == 2
    assert len(memory_posts) == 1 and len(memory_posts[0]["events"]) == 1
    backend.finalize()


def test_memory_json_sanitizes_urls(tmp_path, capture_server, traced_backend):
    backend = traced_backend()
    backend.set_task("task")
    backend.finalize()
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    hashed = hashlib.sha256(TEST_TRAJECTORY_ID.encode()).hexdigest()[:16]
    assert data["settings"]["extract_base_url"] == f"{capture_server.url}/EXTRACT/trajectories/{hashed}/v1"
    assert TEST_TRAJECTORY_ID not in json.dumps(data)


def test_tracing_disabled_for_provider_urls(tmp_path, extract_env, fake_client):
    from cure_memory_bridge.backend import CureMemoryBackend
    from cure_memory_bridge.config import CureMemoryConfig

    config = CureMemoryConfig(enabled=True, output_dir=str(tmp_path / "inst"))
    backend = CureMemoryBackend(config, "test-instance", model_base_url="https://api.deepseek.com/v1")
    backend.start()
    assert backend._available and backend._trace is None
    backend.set_task("task")  # no endpoint, no crash, no posts
    backend.finalize()


def test_pending_inputs_follow_checkpoint_rules(capture_server, fake_client, traced_backend):
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "first fact"}], step=1)
    fake_client.queue.append({"candidates": [], "deletions": [], "rejections": []})
    backend._extract(2)
    starts = capture_server.events("memory_generate_start")
    assert [i["input_id"] for i in starts[0]["payload"]["inputs"]] == ["1"]
    assert starts[0]["payload"]["inputs"][0]["content"]["text"] == "first fact"
    # Success cleared the pending refs: the next start carries only newer input.
    backend.record([{"role": "user", "content": "second fact"}], step=3)
    fake_client.queue.append({"candidates": [], "deletions": [], "rejections": []})
    backend._extract(4)
    starts = capture_server.events("memory_generate_start")
    assert [i["input_id"] for i in starts[1]["payload"]["inputs"]] == ["2"]
    # A failed result holds the checkpoint: the same input is offered again.
    # (CURE only calls the decision client when uncheckpointed messages exist,
    # so the failing pass needs a freshly recorded message.)
    backend.record([{"role": "user", "content": "third fact"}], step=5)
    fake_client.queue.append("http_500")
    backend._extract(6)
    fake_client.queue.append({"candidates": [], "deletions": [], "rejections": []})
    backend._extract(8)
    starts = capture_server.events("memory_generate_start")
    assert [i["input_id"] for i in starts[3]["payload"]["inputs"]] == ["3"]
    backend.finalize()


def test_start_413_runs_native_extraction_untraced(capture_server, fake_client, traced_backend):
    def responder(path, events):
        if any(e["type"] == "memory_generate_start" for e in events):
            return 413, {"error": "too large"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "remember the rolling fix"}], step=1)
    fake_client.queue.append({"candidates": [approved_candidate(1, "k1", "fact one")], "deletions": [], "rejections": []})
    backend._extract(2)
    # Native extraction ran; the rejected start produced no orphan events.
    assert capture_server.events("memory_change") == []
    assert capture_server.events("memory_generate_end") == []
    rows = backend._system.memory_search("minisweagent", query=None, review_status=None)
    assert [row.key for row in rows] == ["k1"]
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_start_oversize"]
    assert record["operation_id"] and record["step"] == 2
    backend.finalize()


def test_start_409_disables_memory_lane_but_not_native_work(capture_server, fake_client, traced_backend):
    def responder(path, events):
        if any(e["type"] == "memory_generate_start" for e in events):
            return 409, {"error": "annotation_conflict"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "remember the rolling fix"}], step=1)
    fake_client.queue.append({"candidates": [approved_candidate(1, "k1", "fact one")], "deletions": [], "rejections": []})
    backend._extract(2)
    (record,) = [e for e in backend._events if e.get("reason") == "annotation_recovery_conflict"]
    assert record["operation_id"] and record["step"] == 2
    assert [row.key for row in backend._system.memory_search("minisweagent", query=None)] == ["k1"]
    posts_before = len(capture_server.requests)
    # The lane stays disabled: later extractions post nothing, still extract natively.
    backend.record([{"role": "user", "content": "another fact"}], step=3)
    fake_client.queue.append({"candidates": [approved_candidate(2, "k2", "fact two")], "deletions": [], "rejections": []})
    backend._extract(4)
    assert len(capture_server.requests) == posts_before
    assert sorted(row.key for row in backend._system.memory_search("minisweagent", query=None)) == ["k1", "k2"]
    backend.finalize()


def test_ambiguous_start_posts_changes_unbound(capture_server, fake_client, traced_backend):
    def responder(path, events):
        if any(e["type"] == "memory_generate_start" for e in events):
            return 500, {"error": "flaky"}
        return 202, {"recorded": len(events), "duplicates": 0, "role_call_cursor": 0}

    capture_server.responder = responder
    backend = traced_backend(annotate_retries=0)
    backend.set_task("task")
    backend.record([{"role": "user", "content": "remember the rolling fix"}], step=1)
    fake_client.queue.append({"candidates": [approved_candidate(1, "k1", "fact one")], "deletions": [], "rejections": []})
    backend._extract(2)
    # The change/end still post (no cursor to guess at), carrying no binding.
    changes = capture_server.events("memory_change")
    ends = capture_server.events("memory_generate_end")
    assert len(changes) == 1 and len(ends) == 1
    assert "binding" not in changes[0] and "binding" not in ends[0]
    backend.finalize()

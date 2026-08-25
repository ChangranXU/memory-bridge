"""T4, T6, T7: extraction flow, lifecycle via extraction, finalize flush."""

import json

from minisweagent.models.test_models import DeterministicToolcallModel

from conftest import SUBMIT_COMMAND, approved_candidate, assert_db_closed, db_rows, make_format_error_model, on_tool


def _memory(output_dir, every_n=2, **overrides):
    return {
        "enabled": True,
        "scope": "instance",
        "output_dir": str(output_dir),
        "extract_every_n_steps": every_n,
        **overrides,
    }


def _read_memory_json(output_dir):
    return json.loads((output_dir / "memory.json").read_text())


def test_candidate_approved_and_checkpoint_advances(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    """T4: candidate decision at the N-step tick lands approved; checkpoint advances."""
    fake_client.rules.append(
        on_tool("trigger_candidate", lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "k1", "fact one")))
    )
    outputs = [
        make_bash_output("s0", ["echo trigger_candidate"]),
        make_bash_output("s1", ["echo s1"]),
        make_bash_output("s2", ["echo s2"]),
        make_bash_output("s3", [SUBMIT_COMMAND]),
    ]
    agent = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "inst", every_n=2),
        cost_limit=100.0,
    )
    info = agent.run("task")
    assert info["exit_status"] == "Submitted"

    # Two extraction attempts: the step-2 tick and the final flush.
    assert len(fake_client.requests) == 2
    first_ids = [m["id"] for m in fake_client.requests[0]["messages"]]
    final_ids = [m["id"] for m in fake_client.requests[1]["messages"]]
    assert first_ids == list(range(1, 7))  # system, instance, a0, t0, a1, t1
    assert final_ids and min(final_ids) > max(first_ids)  # checkpoint advanced

    rows = db_rows(tmp_path / "inst" / "cure_memory.sqlite3", "SELECT key, value, review_status FROM memories")
    assert rows == [("k1", "fact one", "approved")]

    data = _read_memory_json(tmp_path / "inst")
    assert data["counts"]["extraction_calls"] == 2
    assert data["counts"]["extraction_errors"] == 0
    assert data["counts"]["memories_approved"] == 1
    extraction_steps = [e["step"] for e in data["events"] if e["kind"] == "extraction"]
    assert extraction_steps == [2, "final"]


def test_error_holds_checkpoint_and_next_tick_retries(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    """T4: LLM error -> no writes, no checkpoint advance; next tick retries the batch."""
    fake_client.queue.append("http_500")
    fake_client.rules.append(
        on_tool("trigger_retry", lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "k1", "fact one")))
    )
    outputs = [
        make_bash_output("s0", ["echo trigger_retry"]),
        make_bash_output("s1", ["echo s1"]),
        make_bash_output("s2", ["echo s2"]),
        make_bash_output("s3", ["echo s3"]),
        make_bash_output("s4", [SUBMIT_COMMAND]),
    ]
    agent = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "inst", every_n=2),
        cost_limit=100.0,
    )
    agent.run("task")

    # tick@2 errors (checkpoint held), tick@4 retries the same first message id.
    assert len(fake_client.requests) == 3  # two ticks + final flush
    first_retry_ids = [m["id"] for m in fake_client.requests[0]["messages"]]
    second_retry_ids = [m["id"] for m in fake_client.requests[1]["messages"]]
    assert first_retry_ids[0] == second_retry_ids[0]
    assert len(second_retry_ids) > len(first_retry_ids)

    data = _read_memory_json(tmp_path / "inst")
    assert data["counts"]["extraction_errors"] == 1
    assert data["counts"]["memories_approved"] == 1  # retry succeeded
    assert not any(e.get("op") == "extract_breaker" for e in data["events"])
    error_events = [e for e in data["events"] if e["kind"] == "extraction" and e["errors"]]
    assert error_events[0]["errors"] == ["llm_decision_failed:http_500"]


def test_breaker_trips_after_consecutive_errors_but_final_flush_runs(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client
):
    """T4: breaker caps a dead endpoint's retries; finalize still attempts one flush."""
    fake_client.queue.extend(["http_500", "http_500"])
    outputs = [make_bash_output(f"s{i}", [f"echo b{i}"]) for i in range(6)]
    outputs.append(make_bash_output("submit", [SUBMIT_COMMAND]))
    agent = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "inst", every_n=2, extract_max_consecutive_errors=2),
        cost_limit=100.0,
    )
    agent.run("task")

    # Ticks at steps 2 and 4 both fail -> breaker; the step-6 tick is a no-op
    # (would have been a 4th client call); the final flush is the 3rd call.
    assert len(fake_client.requests) == 3
    data = _read_memory_json(tmp_path / "inst")
    assert data["counts"]["extraction_calls"] == 3
    assert data["counts"]["extraction_errors"] == 2
    assert any(e.get("op") == "extract_breaker" for e in data["events"])
    extraction_events = [e for e in data["events"] if e["kind"] == "extraction"]
    assert [e["step"] for e in extraction_events] == [2, 4, "final"]
    assert extraction_events[-1]["errors"] == []


def test_sensitive_guard_rejects_locally_and_checkpoint_advances(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client
):
    """T4: sensitive text is rejected by the local guard, never sent to the LLM,
    and the checkpoint still advances past it."""
    outputs = [
        make_bash_output(None, ['echo "the api_key is fake"']),
        make_bash_output("s1", ["echo s1"]),
        make_bash_output("s2", ["echo s2"]),
        make_bash_output("s3", [SUBMIT_COMMAND]),
    ]
    agent = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "inst", every_n=2),
        cost_limit=100.0,
    )
    agent.run("task")

    # The assistant Actions block and the tool observation both carry the
    # sensitive marker; neither may appear in any LLM request payload.
    for request in fake_client.requests:
        assert all("api_key" not in (m["content"] or "") for m in request["messages"])
    # Checkpoint advanced: message id sets across requests are disjoint and increasing.
    id_sets = [[m["id"] for m in request["messages"]] for request in fake_client.requests]
    assert id_sets[0] and max(id_sets[0]) < min(id_sets[1])

    data = _read_memory_json(tmp_path / "inst")
    assert data["counts"]["extraction_errors"] == 0
    assert data["counts"]["memories_rejected_sensitive"] == 2
    first_event = next(e for e in data["events"] if e["kind"] == "extraction")
    assert first_event["rejected_by_reason"] == {"sensitive_information": 2}
    assert first_event["errors"] == []


def test_missed_periodic_boundary_serviced_on_next_clean_step(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client
):
    """T4: a FormatError exactly at a boundary skips the hook; the next clean step
    services the missed bucket exactly once."""
    outputs = [
        make_bash_output("s0", ["echo s0"]),
        make_bash_output("unused", ["echo unused"]),  # never consumed (index 1 raises)
        make_bash_output("s2", ["echo s2"]),
        make_bash_output("s3", ["echo s3"]),
        make_bash_output("s4", [SUBMIT_COMMAND]),
    ]
    model = make_format_error_model(outputs, error_at=1)
    agent = make_agent(
        model,
        memory=_memory(tmp_path / "inst", every_n=2),
        cost_limit=100.0,
    )
    info = agent.run("task")
    assert info["exit_status"] == "Submitted"

    data = _read_memory_json(tmp_path / "inst")
    # Bucket 1 (missed at step 2 due to the FormatError) fires once at step 3,
    # not again at step 4; step 4 services bucket 2.
    extraction_steps = [e["step"] for e in data["events"] if e["kind"] == "extraction"]
    assert extraction_steps == [3, 4, "final"]


def test_lifecycle_via_extraction(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    """T6: supersede candidates and deletions produce superseded/deleted rows;
    approved-only recall reflects them. The kb row lands in the general layer
    (approved_candidate's scope="user"), so its deletion names that layer
    explicitly — a layer-less deletion stays in the session's own layer."""
    fake_client.rules.extend(
        [
            on_tool("trigger_a", lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "ka", "value one"))),
            on_tool("trigger_b", lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "kb", "delete me target"))),
            on_tool("trigger_sup", lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "ka", "value two"))),
            on_tool(
                "trigger_del",
                lambda msg, dec: dec["deletions"].append(
                    {"message_id": msg["id"], "target": "delete me target", "scope": "user"}
                ),
            ),
        ]
    )
    outputs = [
        make_bash_output("s0", ["echo trigger_a"]),
        make_bash_output("s1", ["echo trigger_b"]),
        make_bash_output("s2", ["echo trigger_sup"]),
        make_bash_output("s3", ["echo trigger_del"]),
        make_bash_output("s4", [SUBMIT_COMMAND]),
    ]
    agent = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "inst", every_n=2),
        cost_limit=100.0,
    )
    agent.run("task")

    rows = db_rows(
        tmp_path / "inst" / "cure_memory.sqlite3",
        "SELECT id, key, value, review_status, supersedes, superseded_by FROM memories ORDER BY id",
    )
    assert [(row[1], row[2], row[3]) for row in rows] == [
        ("ka", "value one", "superseded"),
        ("kb", "delete me target", "deleted"),
        ("ka", "value two", "approved"),
    ]
    v1_id, _, v2_id = rows[0][0], rows[1], rows[2][0]
    assert json.loads(rows[2][4]) == [v1_id]  # v2 supersedes v1
    assert rows[0][5] == v2_id  # v1 superseded_by v2

    data = _read_memory_json(tmp_path / "inst")
    assert data["counts"]["memories_approved"] == 3
    assert data["counts"]["memories_deleted"] == 1
    final_statuses = {(m["key"], m["review_status"]) for m in data["final_memories"]}
    assert final_statuses == {("ka", "superseded"), ("kb", "deleted"), ("ka", "approved")}


def test_finalize_flushes_tail_and_is_idempotent(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client, backend_spy
):
    """T7: extract_every_n_steps=0 -> only the final flush; fake sees the tail;
    memory.json written; store closed; second finalize is a no-op."""
    fake_client.rules.append(
        on_tool("trigger_final", lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "kf", "tail fact")))
    )
    outputs = [
        make_bash_output("s0", ["echo trigger_final"]),
        make_bash_output("s1", [SUBMIT_COMMAND]),
    ]
    agent = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "inst", every_n=0),
        cost_limit=100.0,
    )
    agent.run("task")

    assert len(fake_client.requests) == 1  # no periodic ticks, one flush
    assert any("trigger_final" in (m["content"] or "") for m in fake_client.requests[0]["messages"])

    output_dir = tmp_path / "inst"
    data = _read_memory_json(output_dir)
    assert [e["step"] for e in data["events"] if e["kind"] == "extraction"] == ["final"]
    assert [(m["key"], m["review_status"]) for m in data["final_memories"]] == [("kf", "approved")]

    assert_db_closed(backend_spy[0]._system)

    before = (output_dir / "memory.json").read_text()
    backend_spy[0].finalize()  # guarded no-op: must not clobber the good artifact
    assert (output_dir / "memory.json").read_text() == before
    assert len(fake_client.requests) == 1


# ---------------------------------------------------------------------------
# ChatGPTMemoryDecisionClient unit pins (offline; stubbed HTTP)
# ---------------------------------------------------------------------------
class _StubbedResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def test_empty_completion_is_an_error_that_holds_the_checkpoint(tmp_path, monkeypatch):
    """An empty LLM completion (e.g. a reasoning model that burned its whole
    token budget) is a failed decision, never a silent "nothing to memorize":
    last_error is set, the extraction result carries the error, and the
    session checkpoint does NOT advance past the unprocessed messages."""
    import urllib.request

    from cure_memory.extractor import ChatGPTMemoryDecisionClient
    from cure_memory.system import CUREMemorySystem

    body = json.dumps({"choices": [{"message": {"content": "  "}, "finish_reason": "length"}]}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: _StubbedResponse(body))
    client = ChatGPTMemoryDecisionClient(
        model="m", base_url="https://extract.invalid/v1", api_key="k", max_retries=0
    )
    system = CUREMemorySystem(str(tmp_path / "empty.sqlite3"), llm_client=client)
    try:
        system.start_session("alice", session_id="s1")
        system.record_message("user", "remember the rolling rule")
        result = system.extract_runtime_memories()
        assert client.last_error == "empty_content"
        assert result.errors == ["llm_decision_failed:empty_content"]
        assert system._last_extracted_message_id_by_session["s1"] == 0  # checkpoint held
    finally:
        system.close()


def _stubbed_client(tmp_path, monkeypatch, content: str):
    """A real ChatGPTMemoryDecisionClient whose HTTP layer returns one canned
    completion carrying ``content`` (the parsing/validation layers run)."""
    import urllib.request

    from cure_memory.extractor import ChatGPTMemoryDecisionClient

    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: _StubbedResponse(body))
    return ChatGPTMemoryDecisionClient(model="m", base_url="https://extract.invalid/v1", api_key="k", max_retries=0)


def test_wrong_schema_decision_is_an_error_that_holds_the_checkpoint(tmp_path, monkeypatch):
    """A parseable JSON dict without the decision keys is a wrong-schema
    response, not a "nothing worth memorizing" decision: last_error is set,
    the extraction result carries the error, and the checkpoint holds."""
    from cure_memory.system import CUREMemorySystem

    client = _stubbed_client(tmp_path, monkeypatch, '{"summary": "nothing notable"}')
    system = CUREMemorySystem(str(tmp_path / "schema.sqlite3"), llm_client=client)
    try:
        system.start_session("alice", session_id="s1")
        system.record_message("user", "remember the rolling rule")
        result = system.extract_runtime_memories()
        assert client.last_error == "invalid_decision_schema"
        assert result.errors == ["llm_decision_failed:invalid_decision_schema"]
        assert system._last_extracted_message_id_by_session["s1"] == 0  # checkpoint held
    finally:
        system.close()


def test_non_list_decision_value_is_a_schema_error(tmp_path, monkeypatch):
    """A decision key present with a non-list value violates the schema just
    as a missing key does — silently reading it as empty would advance the
    checkpoint over messages the model never really processed."""
    from cure_memory.system import CUREMemorySystem

    client = _stubbed_client(tmp_path, monkeypatch, '{"candidates": {}, "deletions": [], "rejections": []}')
    system = CUREMemorySystem(str(tmp_path / "schema.sqlite3"), llm_client=client)
    try:
        system.start_session("alice", session_id="s1")
        system.record_message("user", "remember the rolling rule")
        result = system.extract_runtime_memories()
        assert client.last_error == "invalid_decision_schema"
        assert result.errors == ["llm_decision_failed:invalid_decision_schema"]
    finally:
        system.close()


def test_non_dict_decision_items_are_a_schema_error(tmp_path, monkeypatch):
    """String items inside a decision list pass the key-type check but the
    application layer keeps only dict items — the same silently-dropped
    content one level deeper: a schema error that holds the checkpoint."""
    from cure_memory.system import CUREMemorySystem

    client = _stubbed_client(
        tmp_path, monkeypatch, '{"candidates": ["remember the rolling fix"], "deletions": [], "rejections": []}'
    )
    system = CUREMemorySystem(str(tmp_path / "items.sqlite3"), llm_client=client)
    try:
        system.start_session("alice", session_id="s1")
        system.record_message("user", "remember the rolling fix")
        result = system.extract_runtime_memories()
        assert client.last_error == "invalid_decision_schema"
        assert result.errors == ["llm_decision_failed:invalid_decision_schema"]
        assert system._last_extracted_message_id_by_session["s1"] == 0  # checkpoint held
    finally:
        system.close()


def test_null_decision_value_reads_as_an_empty_list(tmp_path, monkeypatch):
    """A present decision key carrying null is the common "none" idiom — no
    content to lose: the decision is valid, reads as empty lists, and the
    checkpoint advances instead of retrying a legitimate answer forever."""
    from cure_memory.system import CUREMemorySystem

    client = _stubbed_client(tmp_path, monkeypatch, '{"candidates": [], "deletions": null, "rejections": []}')
    system = CUREMemorySystem(str(tmp_path / "null.sqlite3"), llm_client=client)
    try:
        system.start_session("alice", session_id="s1")
        message = system.record_message("user", "remember the rolling rule")
        result = system.extract_runtime_memories()
        assert client.last_error is None
        assert result.errors == []
        assert system._last_extracted_message_id_by_session["s1"] == message.id  # advanced
    finally:
        system.close()


def test_partial_schema_decision_is_a_valid_empty_decision(tmp_path, monkeypatch):
    """A dict carrying only some decision keys is schema-valid: absent keys
    read as empty lists, so an all-empty partial decision advances the
    checkpoint instead of retrying a legitimate "nothing" answer forever."""
    from cure_memory.system import CUREMemorySystem

    client = _stubbed_client(tmp_path, monkeypatch, '{"candidates": []}')
    system = CUREMemorySystem(str(tmp_path / "partial.sqlite3"), llm_client=client)
    try:
        system.start_session("alice", session_id="s1")
        message = system.record_message("user", "remember the rolling rule")
        result = system.extract_runtime_memories()
        assert client.last_error is None
        assert result.errors == []
        assert system._last_extracted_message_id_by_session["s1"] == message.id  # advanced
    finally:
        system.close()


def test_unconfigured_client_is_offline_safe_and_never_falls_back_to_openai_env(tmp_path, monkeypatch):
    """No constructor/env settings -> empty decision with missing_api_key.
    $OPENAI_API_KEY is deliberately NOT picked up: the fallback chain used to
    forward it to a hardcoded third-party default endpoint."""
    monkeypatch.delenv("CURE_MEMORY_LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")

    from cure_memory.extractor import ChatGPTMemoryDecisionClient

    client = ChatGPTMemoryDecisionClient()
    assert client.api_key is None
    assert client.decide_memory_updates({}) == {"candidates": [], "deletions": [], "rejections": []}
    assert client.last_error == "missing_api_key"


def test_api_key_without_base_url_is_offline_safe(tmp_path, monkeypatch):
    """A configured key with no endpoint degrades exactly like a missing key
    (empty decision + last_error) — never an uncaught ValueError from urlopen
    on the relative URL, which sits outside the retryable exception tuple."""
    monkeypatch.delenv("CURE_MEMORY_LLM_BASE_URL", raising=False)

    from cure_memory.extractor import ChatGPTMemoryDecisionClient

    client = ChatGPTMemoryDecisionClient(model="m", api_key="sk-test", max_retries=0)
    assert client.decide_memory_updates({}) == {"candidates": [], "deletions": [], "rejections": []}
    assert client.last_error == "missing_base_url"


def test_whitespace_base_url_is_missing_base_url(monkeypatch):
    """A whitespace-only endpoint is no endpoint: it must take the same
    offline-safe missing_base_url path, not slip past the emptiness check
    into an uncaught urlopen ValueError."""
    monkeypatch.delenv("CURE_MEMORY_LLM_BASE_URL", raising=False)

    from cure_memory.extractor import ChatGPTMemoryDecisionClient

    client = ChatGPTMemoryDecisionClient(model="m", base_url="  ", api_key="sk-test", max_retries=0)
    assert client.decide_memory_updates({}) == {"candidates": [], "deletions": [], "rejections": []}
    assert client.last_error == "missing_base_url"

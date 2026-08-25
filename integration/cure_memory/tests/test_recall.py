"""T5: transient recall injection."""

import json

import pytest
from minisweagent.models.test_models import DeterministicToolcallModel

from conftest import SUBMIT_COMMAND, CapturingToolcallModel, approved_candidate, db_rows, make_crashing_model, on_tool

from shared_bridge.prompts import RECALL_POLICY_DEFAULT

RECALL_PROMPT_FIRST_LINE = "Use recalled memory as context for coding tasks, not as unquestionable truth."


def _transient(messages):
    return [m for m in messages if m.get("extra", {}).get("transient_recall")]


def _memory(output_dir, scope="instance", every_n=1, **overrides):
    return {
        "enabled": True,
        "scope": scope,
        "output_dir": str(output_dir),
        "extract_every_n_steps": every_n,
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


def test_approved_memory_injected_transiently(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    """Approved task-matching memory reaches the model as one transient user message;
    the persisted trajectory never contains it."""
    _approve_recall_key(fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(model, memory=_memory(tmp_path / "inst"), cost_limit=100.0)
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"

    # First query ran before any extraction: no marker. Later queries have it.
    assert _transient(model.captured[0]) == []
    for captured in model.captured[1:]:
        markers = _transient(captured)
        assert len(markers) == 1
        content = markers[0]["content"]
        assert markers[0]["role"] == "user"
        # The shared policy composes first (its do-not-respond sentence
        # survives), then the CURE sections.
        assert content.startswith(RECALL_POLICY_DEFAULT)
        assert "## CURE Memory Policy" in content
        assert RECALL_PROMPT_FIRST_LINE in content
        assert "## Relevant Approved Memories" in content
        assert "recall_key: remembered fact xyz" in content

    # Marker gone from the live and persisted trajectory.
    assert _transient(agent.messages) == []
    traj_path = tmp_path / "traj.json"
    agent.save(traj_path)
    assert "transient_recall" not in traj_path.read_text()

    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["counts"]["recall_injections"] == 2
    recall_events = [e for e in data["events"] if e["kind"] == "recall"]
    assert [(e["step"], e["n_memories"]) for e in recall_events] == [(2, 1), (3, 1)]


def test_no_approved_memory_no_injection(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(model, memory=_memory(tmp_path / "inst"), cost_limit=100.0)
    agent.run("fix the recall_key bug")
    assert all(_transient(captured) == [] for captured in model.captured)
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["counts"]["recall_injections"] == 0


def test_pending_review_memory_not_injected(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    fake_client.rules.append(
        on_tool(
            "trigger_recall",
            lambda msg, dec: dec["candidates"].append(
                approved_candidate(msg["id"], "recall_key", "weak fact", review_status="pending_review", confidence=0.9)
            ),
        )
    )
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(model, memory=_memory(tmp_path / "inst"), cost_limit=100.0)
    agent.run("fix the recall_key bug")
    assert all(_transient(captured) == [] for captured in model.captured)
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["counts"]["memories_pending"] == 1
    assert data["counts"]["recall_injections"] == 0
    assert [(m["key"], m["review_status"]) for m in data["final_memories"]] == [("recall_key", "pending_review")]


def _seed_three_memories(tmp_path, make_agent, make_bash_output, fake_client):
    fake_client.rules.append(
        on_tool(
            "cap_trigger",
            lambda msg, dec: dec["candidates"].extend(
                approved_candidate(msg["id"], f"cap_k{i}", f"capterm fact {i}") for i in range(3)
            ),
        )
    )
    outputs = [make_bash_output("s0", ["echo cap_trigger"]), make_bash_output("s1", [SUBMIT_COMMAND])]
    agent1 = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "runs" / "inst1", scope="run"),
        instance_id="inst1",
        cost_limit=100.0,
    )
    agent1.run("cap task one")


def test_recall_capped_by_max_memories(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    _seed_three_memories(tmp_path, make_agent, make_bash_output, fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output, "echo2")))
    agent2 = make_agent(
        model,
        memory=_memory(tmp_path / "runs" / "inst2", scope="run", max_memories=2),
        instance_id="inst2",
        cost_limit=100.0,
    )
    agent2.run("capterm cleanup")
    markers = _transient(model.captured[0])
    assert len(markers) == 1
    assert markers[0]["content"].count("\n- [") == 2  # exactly two bullets
    data = json.loads((tmp_path / "runs" / "inst2" / "memory.json").read_text())
    recall_events = [e for e in data["events"] if e["kind"] == "recall"]
    assert recall_events[0]["n_memories"] == 2


def test_recall_capped_by_max_total_recall_chars(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    """Rank-then-fill with equal-length lines: the budget keeps the top two
    whole lines and the lowest-ranked memory is skipped (nothing remains —
    below the truncation floor). The budget counts the rendered memory lines
    only (the header is excluded)."""
    _seed_three_memories(tmp_path, make_agent, make_bash_output, fake_client)
    # The seeded rows carry inst1's session stamp, so every rendered line is
    # "<bullet> (from earlier episode inst1)" — all three are equal-length.
    line_len = len("- [fact:general] cap_k0: capterm fact 0 (from earlier episode inst1)")
    cap = 2 * line_len + 1  # exactly two bullets fit the lines budget
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output, "echo2")))
    agent2 = make_agent(
        model,
        memory=_memory(tmp_path / "runs" / "inst2", scope="run", max_total_recall_chars=cap),
        instance_id="inst2",
        cost_limit=100.0,
    )
    agent2.run("capterm cleanup")
    markers = _transient(model.captured[0])
    assert len(markers) == 1
    content = markers[0]["content"]
    assert content.index("## Relevant Approved Memories") > 0  # the full header is there regardless of the cap
    assert "[truncated]" not in content
    assert content.count("\n- [") == 2  # exactly two whole bullets
    # tie on score -> store order (updated_at DESC, id DESC): cap_k2, cap_k1, cap_k0
    assert "cap_k2" in content and "cap_k1" in content
    assert "cap_k0" not in content  # lowest-ranked dropped first
    data = json.loads((tmp_path / "runs" / "inst2" / "memory.json").read_text())
    recall_events = [e for e in data["events"] if e["kind"] == "recall"]
    assert recall_events[0]["n_memories"] == 2  # delivered lines, not the 3 selected


def test_recall_budget_below_first_line_injects_nothing(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client
):
    """A cap too small for even the first memory line injects nothing at all
    (no mangled partial header reaches the model)."""
    _seed_three_memories(tmp_path, make_agent, make_bash_output, fake_client)
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output, "echo2")))
    agent2 = make_agent(
        model,
        memory=_memory(tmp_path / "runs" / "inst2", scope="run", max_total_recall_chars=10),
        instance_id="inst2",
        cost_limit=100.0,
    )
    agent2.run("capterm cleanup")
    assert all(_transient(captured) == [] for captured in model.captured)
    data = json.loads((tmp_path / "runs" / "inst2" / "memory.json").read_text())
    assert data["counts"]["recall_injections"] == 0


def test_limit_preflight_does_not_count_injection(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    """A terminal step-limit preflight never invokes the model: marker appended and
    removed, but recall_injections stays 0."""
    _approve_recall_key(fake_client)
    outputs = [make_bash_output("s0", ["echo trigger_recall"])]
    agent = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "inst"),
        cost_limit=100.0,
        step_limit=1,
    )
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "LimitsExceeded"
    assert _transient(agent.messages) == []
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["counts"]["recall_injections"] == 0
    assert [e for e in data["events"] if e["kind"] == "recall"] == []


def test_note_recall_failure_never_masks_model_outcome(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client, monkeypatch
):
    from cure_memory_bridge.backend import CureMemoryBackend

    def boom(self, payload, step):
        raise RuntimeError("accounting boom")

    monkeypatch.setattr(CureMemoryBackend, "note_recall", boom)
    _approve_recall_key(fake_client)

    # Clean run: the model's Submitted outcome wins over the accounting failure.
    agent = make_agent(
        DeterministicToolcallModel(outputs=_recall_script(make_bash_output)),
        memory=_memory(tmp_path / "inst1"),
        cost_limit=100.0,
    )
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"
    assert _transient(agent.messages) == []

    # Crashing run: the original model exception wins over the accounting failure.
    crashing = make_crashing_model(_recall_script(make_bash_output), crash_after=1)
    agent2 = make_agent(crashing, memory=_memory(tmp_path / "inst2"), cost_limit=100.0)
    with pytest.raises(RuntimeError, match="model boom"):
        agent2.run("fix the recall_key bug")


# ---------------------------------------------------------------------------
# Provenance, relevance floor, and search failure counting (backend level)
# ---------------------------------------------------------------------------
def _extract_candidate(backend, fake_client, key, value, step=1):
    """Record one message and run one extraction that approves key=value."""
    backend.record([{"role": "user", "content": f"note about {key}"}], step=step)
    fake_client.queue.append(
        {"candidates": [approved_candidate(999, key, value)], "deletions": [], "rejections": []}
    )
    backend._extract(step)


def test_provenance_names_the_episode_of_the_current_version(tmp_path, make_backend, fake_client):
    """The origin label names the episode that created the memory's CURRENT
    version: a manual memory_replace preserves the original sources, while the
    extraction path's _upsert_memory supersedes and re-stamps the NEW
    episode's sources when the value changes."""
    one = make_backend(instance_id="one", output_dir=str(tmp_path / "runs" / "one"), scope="run")
    one.start()
    one.set_task("alpha task")
    _extract_candidate(one, fake_client, "shared_key", "alpha fact one")
    one.finalize()

    two = make_backend(instance_id="two", output_dir=str(tmp_path / "runs" / "two"), scope="run")
    two.start()
    two.set_task("alpha task")
    recall = two.recall_context()
    assert "- [fact:general] shared_key: alpha fact one (from earlier episode one)" in recall["content"]
    assert recall["origins"] == [one._session_id]

    # memory_replace preserves the replaced row's sources: the new version
    # still names the FIRST episode.
    (row,) = two._system.memory_search("minisweagent", query=None, review_status=None)
    two._system.memory_replace("minisweagent", row.id, "alpha fact replaced")
    two._mark_store_changed()  # a native write outside the extract path
    recall = two.recall_context()
    assert "- [fact:general] shared_key: alpha fact replaced (from earlier episode one)" in recall["content"]

    # The extraction path re-stamps on a value change: the superseding row
    # carries THIS episode's sources.
    _extract_candidate(two, fake_client, "shared_key", "alpha fact two", step=2)
    recall = two.recall_context()
    assert "- [fact:general] shared_key: alpha fact two (from this episode)" in recall["content"]
    assert recall["origins"] == [two._session_id]
    two.finalize()


def test_recall_min_score_filters_on_the_term_count_scale(tmp_path, make_backend, fake_client):
    """CURE's score is an unbounded term-count stamped transiently on the
    search rows: the floor drops below-floor hits before the fill, and the
    stamp never lands in the store."""
    backend = make_backend()
    backend.start()
    backend.set_task("alpha beta task")
    _extract_candidate(backend, fake_client, "one_term", "alpha only")
    _extract_candidate(backend, fake_client, "two_term", "alpha beta")
    rows = backend._system.memory_search("minisweagent", query="alpha beta task")
    assert [row.metadata["score"] for row in rows] == [2, 1]  # ranked term counts

    floored = make_backend(instance_id="floored", recall_min_score=2)
    floored.start()
    floored.set_task("alpha beta task")
    recall = floored.recall_context()
    assert recall["n_memories"] == 1
    assert "two_term" in recall["content"] and "one_term" not in recall["content"]
    # The stamp is transient: the store's rows keep no score.
    stored = db_rows(backend._db_path, "SELECT metadata FROM memories")
    assert stored and all("score" not in json.loads(row[0]) for row in stored)
    floored.finalize()
    backend.finalize()


def test_search_failure_counts_search_errors_and_backend_errors(tmp_path, make_backend):
    """CURE's _search gains the private counting the base contract describes:
    increment search_errors, then re-raise into the recall envelope (which
    counts backend_errors) — today a search failure surfaces only as the
    envelope's generic count."""
    backend = make_backend()
    backend.start()
    backend.set_task("t")
    backend._system.store.conn.close()  # the native search raises underneath
    assert backend.recall_context() is None  # contained
    assert backend._counts["search_errors"] == 1
    assert backend._counts["backend_errors"] == 1


def test_rewrite_tick_replaces_the_recall_query_mid_run(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client, capture_server
):
    """Agent level: the step() rewrite tick calls the QUERY endpoint through
    side_model; a successful rewrite replaces the recall query, and the
    rewritten query still drives the (cache-invalidated) recall search."""
    _approve_recall_key(fake_client)
    capture_server.responder = lambda path, events: (
        200,
        {"choices": [{"message": {"content": '{"query": "recall_key current need"}'}, "finish_reason": "stop"}]},
    )
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=_recall_script(make_bash_output)))
    agent = make_agent(
        model,
        memory=_memory(
            tmp_path / "inst",
            rewrite_every_n_steps=1,
            rewrite_model="q-model",
            rewrite_base_url=capture_server.url,
            rewrite_api_key="k",
        ),
        cost_limit=100.0,
    )
    info = agent.run("fix the recall_key bug")
    assert info["exit_status"] == "Submitted"
    assert any(r["path"] == "/chat/completions" for r in capture_server.requests)
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["counts"]["rewrite_calls"] == 2  # the ticks at steps 1 and 2 (submit ends the loop)
    assert data["counts"]["rewrite_successes"] == 2
    assert data["counts"]["rewrite_failures"] == 0
    assert data["settings"]["rewrite_model"] == "q-model"
    assert data["counts"]["recall_injections"] == 2  # the rewritten query still matched

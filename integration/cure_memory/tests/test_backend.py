"""T8 (backend side), T10, T11, T13: backend behavior."""

import json
import sqlite3

import pytest
from minisweagent.models.test_models import DeterministicToolcallModel

from conftest import (
    CURE_REPO,
    SUBMIT_COMMAND,
    CapturingToolcallModel,
    approved_candidate,
    assert_db_closed,
    on_tool,
)
from cure_memory.prompts import MEMORY_POLICY_PROMPT, memory_policy_prompt
from cure_memory_bridge.backend import CureMemoryBackend
from cure_memory_bridge.config import CureMemoryConfig
from shared_bridge.backend import _repo_of
from shared_bridge.prompts import EXTRACTION_GUIDELINES_DEFAULT, extraction_episode_context


def _episode_context(instance_id: str) -> str:
    """The base-composed context exactly as the backend conveys it, so the
    pins below track the real renderer instead of a hand-copied string."""
    return extraction_episode_context(instance_id, _repo_of(instance_id))


def test_mid_run_failure_contained(tmp_path, make_backend):
    """T8: store closed early -> record/extract/recall contained + counted; finalize
    still writes memory.json (final_memories: []) and closes."""
    backend = make_backend()
    backend.start()
    backend.set_task("some task")
    backend.record([{"role": "user", "content": "hello"}], step=0)
    assert backend._counts["messages_recorded"] == 1

    backend._system.store.conn.close()
    backend.record([{"role": "user", "content": "after close"}], step=1)
    backend.maybe_extract(10)
    assert backend.recall_context() is None
    backend.finalize()  # must not raise

    counts = backend._counts
    assert counts["backend_errors"] >= 2  # record + recall (+ dump)
    assert counts["extraction_errors"] == 2  # the tick + the final flush
    data = json.loads((tmp_path / "test-instance" / "memory.json").read_text())
    assert data["available"] is True
    assert data["final_memories"] == []
    assert any(e.get("op") == "finalize_dump" for e in data["events"])
    assert_db_closed(backend._system)


def test_mid_run_failure_strict_raises(tmp_path, extract_env, fake_client):
    backend = CureMemoryBackend(
        CureMemoryConfig(enabled=True, output_dir=str(tmp_path / "strict"), scope="instance", strict=True),
        "strict-id",
    )
    backend.start()
    backend._system.store.conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        backend.record([{"role": "user", "content": "x"}], step=1)
    with pytest.raises(sqlite3.ProgrammingError):
        backend.maybe_extract(10)
    with pytest.raises(sqlite3.ProgrammingError):
        backend.finalize()


def test_empty_tick_is_not_a_counted_extraction(tmp_path, make_backend, fake_client):
    """Base contract: an unready tick (no unextracted messages) is not a
    counted call — no extraction_calls increment, no decision request; the
    final flush over an empty tail is equally silent."""
    backend = make_backend()
    backend.start()
    backend.maybe_extract(10)  # a bucket boundary with nothing recorded
    backend._extract("final")  # the finalize flush path
    assert backend._counts["extraction_calls"] == 0
    assert backend._counts["extraction_errors"] == 0
    assert fake_client.requests == []
    backend.finalize()
    assert backend._counts["extraction_calls"] == 0


def test_stale_external_checkout_is_unavailable_at_start(tmp_path, make_backend, monkeypatch):
    """An external cure checkout (cure_repo_path / $CURE_MEMORY_REPO) predating
    has_unextracted_messages must fail at START with one clear error event —
    not as an AttributeError on every extraction tick until the breaker trips
    (extraction silently never runs)."""
    from cure_memory.system import CUREMemorySystem

    monkeypatch.delattr(CUREMemorySystem, "has_unextracted_messages")
    backend = make_backend()
    backend.start()  # contained (non-strict)
    assert backend._available is False
    assert backend._system is None  # the capability check precedes construction
    data = json.loads((tmp_path / "test-instance" / "memory.json").read_text())
    assert data["available"] is False
    assert any("has_unextracted_messages" in e.get("error", "") for e in data["events"])


def test_external_checkout_predating_policy_guidelines_is_unavailable_at_start(tmp_path, make_backend, monkeypatch):
    """A checkout new enough to carry has_unextracted_messages but predating
    the policy_guidelines constructor kwarg would otherwise die at
    construction with a raw TypeError — the guard names the cause at start."""
    from cure_memory.system import CUREMemorySystem

    monkeypatch.setattr(CUREMemorySystem, "__init__", lambda self, db_path, llm_client=None: None)
    backend = make_backend()
    backend.start()  # contained (non-strict)
    assert backend._available is False
    assert backend._system is None  # the capability check precedes construction
    data = json.loads((tmp_path / "test-instance" / "memory.json").read_text())
    assert data["available"] is False
    assert any("policy_guidelines" in e.get("error", "") for e in data["events"])


def test_partial_startup_failure_closes_connection(tmp_path, make_backend, monkeypatch):
    """T8: failure after CUREMemorySystem construction but before an active session
    closes the partial connection in both modes."""
    probe = make_backend(instance_id="probe")
    probe._import_cure()

    def boom(*args, **kwargs):
        raise RuntimeError("start_session boom")

    monkeypatch.setattr(probe._SystemClass, "start_session", boom)

    plain = make_backend(instance_id="plain")
    plain.start()  # contained
    assert plain._available is False
    assert_db_closed(plain._system)
    data = json.loads((tmp_path / "plain" / "memory.json").read_text())
    assert data["available"] is False
    assert any(e.get("op") == "start" for e in data["events"])

    strict = make_backend(instance_id="strict", strict=True)
    with pytest.raises(RuntimeError, match="start_session boom"):
        strict.start()
    assert_db_closed(strict._system)


def test_restart_closes_previous_system(tmp_path, make_backend):
    """A re-start must close the previous episode's SQLite handle (WAL/DB
    lock), not just drop the reference."""
    backend = make_backend()
    backend.start()
    first = backend._system
    backend.start()
    assert backend._available is True
    assert backend._system is not first
    assert backend._session_id is not None
    assert_db_closed(first)


def test_failed_restart_writes_initial_derived_fields(tmp_path, make_backend, monkeypatch):
    """A failed re-start writes the initial artifact literal: derived fields
    (db_path, cure_system_path) are null, never the previous episode's."""
    backend = make_backend()
    backend.start()
    assert backend._db_path is not None
    monkeypatch.delenv("EXTRACT_MODEL")
    backend.start()
    assert backend._available is False
    assert backend._system is None  # no stale closed handle survives the re-start
    data = json.loads((tmp_path / "test-instance" / "memory.json").read_text())
    assert data["available"] is False
    assert data["session_id"] is None
    assert data["db_path"] is None
    assert data["cure_system_path"] is None
    assert data["settings"]["extract_model"] == ""


def test_post_finalize_recall_is_dormant_not_counted(tmp_path, make_backend):
    """Finalize leaves _available=True with the system closed: a stray
    post-finalize recall is a silent no-op, not a counted backend error."""
    backend = make_backend()
    backend.start()
    backend.set_task("some task")
    backend.finalize()
    assert backend.recall_context() is None
    assert backend._counts["backend_errors"] == 0
    assert not any(e.get("op") == "recall" for e in backend._events)


def test_post_finalize_record_and_extract_are_dormant(tmp_path, make_backend):
    """The same dormancy covers the rest of the work surface: a stray
    post-finalize record/extract tick never touches the closed handle —
    nothing is stored, counted, or logged."""
    backend = make_backend()
    backend.start()
    backend.set_task("some task")
    backend.record([{"role": "user", "content": "hello"}], step=0)
    backend.finalize()
    counts = dict(backend._counts)
    n_events = len(backend._events)
    backend.record([{"role": "user", "content": "late"}], step=1)
    backend.maybe_extract(10)
    assert backend._counts == counts
    assert len(backend._events) == n_events


def test_run_scope_sharing(tmp_path, make_agent, make_bash_output, extract_env, fake_client, backend_spy):
    """T10: shared run DB — episode 2 recalls episode 1's approved memory at its
    first query; distinct sessions; no cross-episode reprocessing."""
    fake_client.rules.append(
        on_tool(
            "trigger_shared",
            lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "shared_fact", "shared_xarray_quirk")),
        )
    )
    memory1 = {"enabled": True, "scope": "run", "output_dir": str(tmp_path / "runs" / "i1"), "extract_every_n_steps": 1}
    memory2 = {**memory1, "output_dir": str(tmp_path / "runs" / "i2")}
    agent1 = make_agent(
        DeterministicToolcallModel(
            outputs=[make_bash_output("s0", ["echo trigger_shared"]), make_bash_output("s1", [SUBMIT_COMMAND])]
        ),
        memory=memory1,
        instance_id="i1",
        cost_limit=100.0,
    )
    agent1.run("first task")
    capturing = CapturingToolcallModel(
        DeterministicToolcallModel(outputs=[make_bash_output("s0", ["echo plain"]), make_bash_output("s1", [SUBMIT_COMMAND])])
    )
    agent2 = make_agent(capturing, memory=memory2, instance_id="i2", cost_limit=100.0)
    agent2.run("use shared_xarray_quirk wisely")

    first, second = backend_spy
    assert first._db_path == second._db_path == (tmp_path / "runs" / "cure_memory.sqlite3").resolve()
    assert first._session_id != second._session_id

    markers = [m for m in capturing.captured[0] if m.get("extra", {}).get("transient_recall")]
    assert len(markers) == 1
    assert "shared_fact: shared_xarray_quirk" in markers[0]["content"]

    # Episode 1: tick@1 + final flush. Episode 2: tick@1 + final flush.
    assert len(fake_client.requests) == 4
    for request in fake_client.requests[:2]:
        assert {m["session_id"] for m in request["messages"]} == {first._session_id}
    for request in fake_client.requests[2:]:
        assert {m["session_id"] for m in request["messages"]} == {second._session_id}


def test_instance_scope_isolation(tmp_path, make_agent, make_bash_output, extract_env, fake_client, backend_spy):
    """T11: per-instance DBs — no cross-contamination even with a matching query."""
    fake_client.rules.append(
        on_tool(
            "trigger_shared",
            lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "shared_fact", "shared_xarray_quirk")),
        )
    )
    memory1 = {"enabled": True, "scope": "instance", "output_dir": str(tmp_path / "i1"), "extract_every_n_steps": 1}
    memory2 = {**memory1, "output_dir": str(tmp_path / "i2")}
    agent1 = make_agent(
        DeterministicToolcallModel(
            outputs=[make_bash_output("s0", ["echo trigger_shared"]), make_bash_output("s1", [SUBMIT_COMMAND])]
        ),
        memory=memory1,
        instance_id="i1",
        cost_limit=100.0,
    )
    agent1.run("first task")
    capturing = CapturingToolcallModel(
        DeterministicToolcallModel(outputs=[make_bash_output("s0", ["echo plain"]), make_bash_output("s1", [SUBMIT_COMMAND])])
    )
    agent2 = make_agent(capturing, memory=memory2, instance_id="i2", cost_limit=100.0)
    agent2.run("use shared_xarray_quirk wisely")

    first, second = backend_spy
    assert first._db_path != second._db_path
    assert (tmp_path / "i1" / "cure_memory.sqlite3").exists()
    assert (tmp_path / "i2" / "cure_memory.sqlite3").exists()
    assert all(not m.get("extra", {}).get("transient_recall") for m in capturing.captured[0])
    data = json.loads((tmp_path / "i2" / "memory.json").read_text())
    assert data["counts"]["recall_injections"] == 0


def test_import_failure_loud_and_contained(tmp_path, make_agent, make_bash_output, extract_env, fake_client, monkeypatch):
    """T13: import failure -> available=false, initial memory.json, episode completes;
    strict mode raises instead. The extraction client is never consulted."""
    monkeypatch.setattr(CureMemoryBackend, "_import_cure", lambda self: False)

    agent = make_agent(
        DeterministicToolcallModel(outputs=[make_bash_output("s0", [SUBMIT_COMMAND])]),
        memory={"enabled": True, "scope": "instance", "output_dir": str(tmp_path / "inst")},
        cost_limit=100.0,
    )
    info = agent.run("task")
    assert info["exit_status"] == "Submitted"
    assert fake_client.requests == []
    assert not (tmp_path / "inst" / "cure_memory.sqlite3").exists()
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["enabled"] is True
    assert data["available"] is False
    assert any(e.get("op") == "start" for e in data["events"])
    assert agent.serialize()["info"]["memory"]["available"] is False

    strict_agent = make_agent(
        DeterministicToolcallModel(outputs=[make_bash_output("s0", [SUBMIT_COMMAND])]),
        memory={"enabled": True, "scope": "instance", "output_dir": str(tmp_path / "strict"), "strict": True},
        cost_limit=100.0,
    )
    with pytest.raises(RuntimeError, match="could not be imported"):
        strict_agent.run("task")
    assert json.loads((tmp_path / "strict" / "memory.json").read_text())["available"] is False


def test_explicit_repo_resolves_origin(tmp_path, make_backend):
    """T13: an explicit cure_repo_path resolves and its system.py is recorded."""
    backend = make_backend(cure_repo_path=str(CURE_REPO))
    backend.start()
    expected = str((CURE_REPO / "cure_memory" / "system.py").resolve())
    assert backend._available is True
    assert backend._cure_system_path == expected
    backend.finalize()
    data = json.loads((tmp_path / "test-instance" / "memory.json").read_text())
    assert data["cure_system_path"] == expected


def test_origin_mismatch_refused(tmp_path, make_backend, extract_env, fake_client):
    """T13: explicit candidate while a different cure_memory is already cached
    -> hard origin mismatch, backend unavailable, client never consulted."""
    primed = make_backend(instance_id="primed")
    primed.start()  # caches the real cure_memory in sys.modules
    assert primed._available is True
    primed.finalize()

    fake_repo = tmp_path / "fake_cure_repo"
    (fake_repo / "cure_memory").mkdir(parents=True)
    (fake_repo / "cure_memory" / "system.py").write_text("# decoy")

    mismatched = make_backend(instance_id="mismatched", cure_repo_path=str(fake_repo))
    mismatched.start()
    assert mismatched._available is False
    data = json.loads((tmp_path / "mismatched" / "memory.json").read_text())
    assert data["available"] is False
    assert "origin mismatch" in data["events"][0]["error"]
    assert fake_client.requests == []


# ---------------------------------------------------------------------------
# Extraction guidelines (the shared policy layer composed into the policy prompt)
# ---------------------------------------------------------------------------
def test_default_guidelines_ride_the_policy_prompt(tmp_path, make_backend, fake_client):
    """No override: the decision request's policy prompt is the native CURE
    policy with the shared default guidelines (episode context appended) as
    one extra section, and memory.json records the conveyed text."""
    backend = make_backend()
    backend.start()
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend.maybe_extract(10)
    conveyed = f"{EXTRACTION_GUIDELINES_DEFAULT}\n\n{_episode_context('test-instance')}"
    policy = fake_client.requests[0]["policy_prompt"]
    assert policy.startswith(MEMORY_POLICY_PROMPT)
    assert policy.endswith(f"Additional extraction guidelines for this run:\n{conveyed}")
    backend.finalize()
    data = json.loads((tmp_path / "test-instance" / "memory.json").read_text())
    assert data["settings"]["extraction_guidelines"] == conveyed


def test_guidelines_override_replaces_the_default(make_backend, fake_client):
    """An override replaces the shared default wholesale — the composed policy
    carries the override, never default + override concatenated. The base's
    episode context still rides along: it is episode fact, not policy."""
    backend = make_backend(extraction_guidelines="prefer operational facts")
    backend.start()
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend.maybe_extract(10)
    policy = fake_client.requests[0]["policy_prompt"]
    assert policy.endswith(
        f"Additional extraction guidelines for this run:\n"
        f"prefer operational facts\n\n{_episode_context('test-instance')}"
    )
    assert EXTRACTION_GUIDELINES_DEFAULT not in policy


def test_empty_guidelines_keep_the_policy_prompt_byte_identical():
    """The composition no-op: empty/blank guidelines convey nothing, so the
    policy prompt is exactly the native constant (an integration whose engine
    accepts no prompt rules changes no bytes)."""
    assert memory_policy_prompt("") == MEMORY_POLICY_PROMPT
    assert memory_policy_prompt("   ") == MEMORY_POLICY_PROMPT
    composed = memory_policy_prompt("one guideline")
    assert composed.startswith(MEMORY_POLICY_PROMPT)
    assert composed.endswith("Additional extraction guidelines for this run:\none guideline")

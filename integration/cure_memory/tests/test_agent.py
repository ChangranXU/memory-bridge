"""T1, T8 (agent side), T9: agent-level behavior."""

import json
import sqlite3

import pytest
from minisweagent.models.test_models import DeterministicToolcallModel
from minisweagent.run.benchmarks.utils.common import ProgressTrackingAgent

from conftest import (
    SUBMIT_COMMAND,
    CapturingToolcallModel,
    DummyProgressManager,
    approved_candidate,
    assert_db_closed,
    make_crashing_model,
    on_tool,
    scrub_timestamps,
)
from cure_memory_bridge.agent import CureMemoryAgent
from cure_memory_bridge.backend import CureMemoryBackend


def _memory(output_dir, **overrides):
    return {
        "enabled": True,
        "scope": "instance",
        "output_dir": str(output_dir),
        "extract_every_n_steps": 1,
        **overrides,
    }


def test_disabled_arm_matches_stock_agent(agent_config, make_bash_output, tmp_path):
    """T1: memory.enabled unset yields a byte-identical trajectory and zero artifacts."""
    from minisweagent.environments.local import LocalEnvironment

    spec = [("step1", ["echo hello"]), ("step2", [SUBMIT_COMMAND])]
    stock = ProgressTrackingAgent(
        DeterministicToolcallModel(outputs=[make_bash_output(c, a) for c, a in spec]),
        LocalEnvironment(),
        progress_manager=DummyProgressManager(),
        instance_id="test",
        **agent_config,
    )
    bridge = CureMemoryAgent(
        DeterministicToolcallModel(outputs=[make_bash_output(c, a) for c, a in spec]),
        LocalEnvironment(),
        progress_manager=DummyProgressManager(),
        instance_id="test",
        **agent_config,
    )

    assert stock.run("task") == bridge.run("task")
    assert scrub_timestamps(stock.messages) == scrub_timestamps(bridge.messages)
    assert bridge._mem is None
    assert bridge._last_run_stats is None
    assert not (tmp_path / "memory.json").exists()
    assert not (tmp_path / "cure_memory.sqlite3").exists()


def test_backend_failure_contained_and_run_completes(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client, monkeypatch, backend_spy
):
    """T8: store failing mid-run is contained + counted; the episode still completes;
    a failing final dump still writes memory.json with final_memories: []."""
    original_extract = CureMemoryBackend._extract

    def extract_then_kill(self, step):
        try:
            return original_extract(self, step)
        finally:
            if step != "final":
                self._system.store.conn.close()

    monkeypatch.setattr(CureMemoryBackend, "_extract", extract_then_kill)

    outputs = [
        make_bash_output("s0", ["echo one"]),
        make_bash_output("s1", ["echo two"]),
        make_bash_output("s2", [SUBMIT_COMMAND]),
    ]
    agent = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "inst"),
        cost_limit=100.0,
    )
    info = agent.run("task")
    assert info["exit_status"] == "Submitted"

    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["available"] is True
    # record/recall/extract failures after the close are all contained and counted
    assert data["counts"]["backend_errors"] >= 3
    assert data["counts"]["extraction_errors"] >= 2
    assert data["final_memories"] == []
    assert any(e.get("op") == "finalize_dump" for e in data["events"])
    assert_db_closed(backend_spy[0]._system)


def test_strict_backend_failure_raises_into_agent_loop(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client, monkeypatch, backend_spy
):
    """T8: with strict=true the contained failure raises instead."""
    original_extract = CureMemoryBackend._extract

    def extract_then_kill(self, step):
        try:
            return original_extract(self, step)
        finally:
            if step != "final":
                self._system.store.conn.close()

    monkeypatch.setattr(CureMemoryBackend, "_extract", extract_then_kill)

    outputs = [make_bash_output("s0", ["echo one"]), make_bash_output("s1", [SUBMIT_COMMAND])]
    agent = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "inst", strict=True),
        cost_limit=100.0,
    )
    with pytest.raises(sqlite3.ProgrammingError):
        agent.run("task")
    assert (tmp_path / "inst" / "memory.json").exists()


def test_finalize_on_model_crash(tmp_path, make_agent, make_bash_output, extract_env, fake_client, backend_spy):
    """T9: model raises mid-run; memory.json still written; db closed; stats via _last_run_stats."""
    outputs = [make_bash_output("s0", ["echo one"])]
    model = make_crashing_model(outputs, crash_after=1)
    agent = make_agent(model, memory=_memory(tmp_path / "inst"), cost_limit=100.0)

    with pytest.raises(RuntimeError, match="model boom"):
        agent.run("task")

    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["enabled"] is True
    assert data["available"] is True
    assert data["counts"]["messages_recorded"] > 0
    assert data["counts"]["extraction_calls"] >= 1  # tick after step 1 + final flush
    assert_db_closed(backend_spy[0]._system)

    serialized = agent.serialize()
    assert serialized["info"]["memory"]["enabled"] is True
    assert serialized["info"]["memory"]["available"] is True
    assert serialized["info"]["memory"]["counts"]["messages_recorded"] > 0


@pytest.fixture
def patched_finalize(monkeypatch):
    original_finalize = CureMemoryBackend.finalize

    def _finalize(self):
        original_finalize(self)
        raise ValueError("finalize boom")

    monkeypatch.setattr(CureMemoryBackend, "finalize", _finalize)


def test_finalize_error_does_not_mask_model_exception(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client, patched_finalize, backend_spy
):
    """T9: a raising finalize neither masks the model's exception nor skips stats capture."""
    model = make_crashing_model([make_bash_output("s0", ["echo one"])], crash_after=1)
    agent = make_agent(model, memory=_memory(tmp_path / "inst"), cost_limit=100.0)

    with pytest.raises(RuntimeError, match="model boom"):
        agent.run("task")

    assert (tmp_path / "inst" / "memory.json").exists()
    assert agent.serialize()["info"]["memory"]["available"] is True


def test_finalize_error_contained_or_raised_without_primary_exception(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client, patched_finalize, backend_spy
):
    """T9: with no primary exception the finalize failure is contained (non-strict)
    and propagated (strict) only after cleanup and stats capture."""
    outputs = [make_bash_output("s0", [SUBMIT_COMMAND])]

    clean = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "clean"),
        cost_limit=100.0,
    )
    info = clean.run("task")  # contained: finalize boom does not surface
    assert info["exit_status"] == "Submitted"
    assert (tmp_path / "clean" / "memory.json").exists()
    assert clean.serialize()["info"]["memory"]["available"] is True

    strict = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "strict", strict=True),
        cost_limit=100.0,
    )
    with pytest.raises(ValueError, match="finalize boom"):
        strict.run("task")
    assert (tmp_path / "strict" / "memory.json").exists()
    assert strict.serialize()["info"]["memory"]["available"] is True
    assert_db_closed(backend_spy[-1]._system)


def test_raising_annotation_hooks_never_reach_the_agent_loop(
    tmp_path, make_agent, make_bash_output, extract_env, fake_client, monkeypatch
):
    """A backend whose annotation touchpoints raise (e.g. a future third
    integration's override bug) must not break the episode: the agent guards
    contain the failure and the recall block still reaches the model."""

    def _boom(self):
        raise RuntimeError("annotation boom")

    monkeypatch.setattr(CureMemoryBackend, "main_lane_cursor", _boom)
    monkeypatch.setattr(CureMemoryBackend, "consume_annotation_duration", _boom)
    fake_client.rules.append(
        on_tool(
            "trigger_recall",
            lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "recall_key", "remembered fact xyz")),
        )
    )
    outputs = [
        make_bash_output("s0", ["echo trigger_recall"]),
        make_bash_output("s1", ["echo step1"]),
        make_bash_output("s2", [SUBMIT_COMMAND]),
    ]
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=outputs))
    agent = make_agent(model, memory=_memory(tmp_path / "inst"), cost_limit=100.0)
    # CURE recall scores the task's terms against key/value text, so the task
    # must literally name the approved key.
    info = agent.run("recall_key")
    assert info["exit_status"] == "Submitted"
    # Queries 2 and 3 ran after the step-1 approval: the transient block was
    # placed despite the cursor read and duration consume raising around it.
    transient = [m for m in model.captured[1] if m.get("extra", {}).get("transient_recall")]
    assert len(transient) == 1
    assert "recall_key" in transient[0]["content"]

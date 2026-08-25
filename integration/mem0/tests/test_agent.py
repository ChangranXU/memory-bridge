"""Agent-level behavior: on/off arms, transient recall, artifacts."""

import json

import pytest
from minisweagent.models.test_models import DeterministicToolcallModel
from minisweagent.run.benchmarks.utils.common import ProgressTrackingAgent

from tests.conftest import SUBMIT_COMMAND, CapturingToolcallModel, DummyProgressManager, scrub_timestamps
from mem0_bridge.agent import Mem0Agent

from shared_bridge.prompts import RECALL_POLICY_DEFAULT


def _memory(output_dir, **overrides):
    return {
        "enabled": True,
        "scope": "instance",
        "output_dir": str(output_dir),
        "api_key": "test-key",
        "extract_every_n_steps": 1,
        **overrides,
    }


def test_disabled_arm_matches_stock_agent(agent_config, make_bash_output, tmp_path):
    """memory.enabled unset yields a byte-identical trajectory and zero artifacts."""
    from minisweagent.environments.local import LocalEnvironment

    spec = [("step1", ["echo hello"]), ("step2", [SUBMIT_COMMAND])]
    stock = ProgressTrackingAgent(
        DeterministicToolcallModel(outputs=[make_bash_output(c, a) for c, a in spec]),
        LocalEnvironment(),
        progress_manager=DummyProgressManager(),
        instance_id="test",
        **agent_config,
    )
    bridge = Mem0Agent(
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


def test_run_with_memory_completes_and_writes_artifacts(
    tmp_path, make_agent, make_bash_output, fake_client, backend_spy
):
    outputs = [
        make_bash_output("s0", ["echo one"]),
        make_bash_output("s1", [SUBMIT_COMMAND]),
    ]
    agent = make_agent(DeterministicToolcallModel(outputs=outputs), memory=_memory(tmp_path / "inst"), cost_limit=100.0)
    info = agent.run("the task text")
    assert info["exit_status"] == "Submitted"

    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["enabled"] is True
    assert data["available"] is True
    assert data["effective_user_id"] == "minisweagent:test-instance"
    assert data["counts"]["messages_recorded"] > 0
    assert data["counts"]["extraction_calls"] >= 1
    assert data["counts"]["extraction_errors"] == 0
    assert len(data["final_memories"]) > 0

    serialized = agent.serialize()
    assert serialized["info"]["memory"]["enabled"] is True
    assert serialized["info"]["memory"]["available"] is True
    assert serialized["info"]["memory"]["counts"]["messages_recorded"] > 0
    assert fake_client.closed is True


def test_recall_injected_transiently_and_never_recorded(
    tmp_path, make_agent, make_bash_output, fake_client, backend_spy
):
    """Pre-existing memories inject a recall block before each model call; the
    block reaches the model but never the recorded trajectory or the store."""
    fake_client.memories["pre-1"] = {
        "id": "pre-1",
        "memory": "the failing test lives in test_concat.py",
        "user_id": "minisweagent:test-instance",
        "score": 0.9,
        "metadata": {},
    }
    outputs = [
        make_bash_output("s0", ["echo one"]),
        make_bash_output("s1", [SUBMIT_COMMAND]),
    ]
    model = CapturingToolcallModel(DeterministicToolcallModel(outputs=outputs))
    agent = make_agent(model, memory=_memory(tmp_path / "inst"), cost_limit=100.0)
    info = agent.run("fix the concat bug")
    assert info["exit_status"] == "Submitted"

    # Every captured model call saw the recall block...
    assert model.captured
    for messages in model.captured:
        assert any(
            isinstance(m.get("content"), str)
            and m["content"].startswith(RECALL_POLICY_DEFAULT)
            and "## Persistent Memory (mem0)" in m["content"]
            for m in messages
        )
    # ...but the persistent trajectory never contains it.
    assert not any(
        isinstance(m.get("content"), str) and RECALL_POLICY_DEFAULT in m["content"] for m in agent.messages
    )
    sent = [m for call in fake_client.add_calls for m in call["messages"]]
    assert not any("Persistent Memory" in m["content"] for m in sent)

    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["counts"]["recall_injections"] >= 1
    assert data["counts"]["search_errors"] == 0


def test_backend_unavailable_still_completes_run(tmp_path, make_agent, make_bash_output, monkeypatch):
    """A missing API key degrades to a no-op backend; the episode still runs."""
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    outputs = [make_bash_output("s0", [SUBMIT_COMMAND])]
    agent = make_agent(
        DeterministicToolcallModel(outputs=outputs),
        memory=_memory(tmp_path / "inst", api_key=""),
        cost_limit=100.0,
    )
    info = agent.run("task")
    assert info["exit_status"] == "Submitted"
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["available"] is False
    assert agent.serialize()["info"]["memory"]["available"] is False


def test_finalize_on_model_crash(tmp_path, make_agent, make_bash_output, fake_client, backend_spy):
    """Model raises mid-run; memory.json still written; client closed; stats captured."""
    from tests.conftest import make_crashing_model

    model = make_crashing_model([make_bash_output("s0", ["echo one"])], crash_after=1)
    agent = make_agent(model, memory=_memory(tmp_path / "inst"), cost_limit=100.0)

    with pytest.raises(RuntimeError, match="model boom"):
        agent.run("task")

    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert data["enabled"] is True
    assert data["available"] is True
    assert data["counts"]["messages_recorded"] > 0
    assert fake_client.closed is True
    assert agent.serialize()["info"]["memory"]["available"] is True

"""T12: runner offline with process_instance."""

import json
from unittest.mock import patch

import pytest
from minisweagent.models.test_models import DeterministicToolcallModel
from minisweagent.run.benchmarks import swebench as _swebench

from conftest import SENTINEL_EXTRACT_KEY, approved_candidate, on_tool


@pytest.fixture
def use_bridge_runner():
    """Import the bridge runner (monkeypatch) and restore the class after the test."""
    original = _swebench.ProgressTrackingAgent
    import cure_memory_bridge.run.swebench  # noqa: F401

    yield
    _swebench.ProgressTrackingAgent = original


def test_runner_offline_process_instance(
    use_bridge_runner, tmp_path, make_bash_output, extract_env, fake_client, progress_manager_factory
):
    """The bridge runner produces preds.json, traj (with info.memory), memory.json
    and cure_memory.sqlite3; the fake extraction client is consulted; the sentinel
    extraction key appears in no artifact."""
    from minisweagent.run.benchmarks.swebench import process_instance

    fake_client.rules.append(
        on_tool(
            "trigger_runner",
            lambda msg, dec: dec["candidates"].append(approved_candidate(msg["id"], "runner_fact", "runner value")),
        )
    )

    instance = {"instance_id": "test__repo-1", "problem_statement": "fix it"}
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    config = {
        "agent": {
            "system_template": "You are a test assistant.",
            "instance_template": "Task: {{task}}",
            "memory": {
                "enabled": True,
                "scope": "instance",
                "output_dir": str(output_dir),
                "extract_every_n_steps": 1,
            },
        },
        "environment": {"environment_class": "local", "cwd": str(cwd)},
        "model": {"model_name": "deterministic_toolcall"},
    }

    outputs = [
        make_bash_output("save", ["echo trigger_runner"]),
        make_bash_output("submit", ["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && echo patch"]),
    ]
    with patch("minisweagent.run.benchmarks.swebench.get_model") as mock_get_model:
        mock_get_model.return_value = DeterministicToolcallModel(outputs=outputs)
        process_instance(instance, output_dir, config, progress_manager_factory(1))

    preds = json.loads((output_dir / "preds.json").read_text())
    assert preds["test__repo-1"]["model_patch"] == "patch\n"

    traj = json.loads((output_dir / "test__repo-1" / "test__repo-1.traj.json").read_text())
    assert traj["info"]["memory"]["enabled"] is True
    assert traj["info"]["memory"]["available"] is True
    assert "transient_recall" not in json.dumps(traj)

    memory_path = output_dir / "memory.json"
    memory = json.loads(memory_path.read_text())
    assert memory["counts"]["memories_approved"] == 1
    assert memory["counts"]["extraction_errors"] == 0
    assert fake_client.requests  # the fake extraction client was consulted
    assert (output_dir / "cure_memory.sqlite3").exists()

    # The sentinel extraction API key appears in no artifact.
    blob = json.dumps(traj) + memory_path.read_text()
    assert SENTINEL_EXTRACT_KEY not in blob

"""Shared test fixtures for the tencentdb memory integration (fully offline)."""

import copy
import json
from pathlib import Path

import pytest

from tencentdb.tests.fake_gateway import CONVO_SEARCH_COMMAND, SUBMIT_COMMAND, FakeGatewayClient, SCENE_READ_COMMAND
from shared_bridge.testing import CaptureServer


def write_gateway_yaml(run_root, l1_idle_timeout_seconds=30):
    """Write a minimal ``<run_root>/tdai/tdai-gateway.yaml`` for the backend's
    start-time readback (``memory.pipeline.l1IdleTimeoutSeconds`` — the
    driver's writer emits it as a plain numeric literal, mirrored here)."""
    path = Path(run_root) / "tdai" / "tdai-gateway.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"memory:\n  pipeline:\n    l1IdleTimeoutSeconds: {l1_idle_timeout_seconds}\n")
    return path


@pytest.fixture(autouse=True)
def gateway_yaml(tmp_path):
    """Every started backend resolves the L1 idle timeout from
    ``<run_root>/tdai/tdai-gateway.yaml`` at start; the suite's run roots are
    tmp_path, so each test gets the driver's pin shape by default. Tests
    needing another value rewrite the returned path."""
    return write_gateway_yaml(tmp_path)

@pytest.fixture
def submit_command():
    return SUBMIT_COMMAND


@pytest.fixture
def scene_read_command():
    return SCENE_READ_COMMAND


@pytest.fixture
def convo_search_command():
    return CONVO_SEARCH_COMMAND


@pytest.fixture
def fake_client(monkeypatch):
    """Inject one FakeGatewayClient into every backend started in the test."""
    from tencentdb_bridge.backend import TencentDBBackend

    client = FakeGatewayClient()
    monkeypatch.setattr(TencentDBBackend, "_make_client", lambda self, settings: client)
    return client


# ---------------------------------------------------------------------------
# Agent / backend factories
# ---------------------------------------------------------------------------
class DummyProgressManager:
    def on_instance_start(self, instance_id):
        pass

    def update_instance_status(self, instance_id, status):
        pass

    def on_instance_end(self, instance_id, status):
        pass

    def on_uncaught_exception(self, instance_id, exception):
        pass


@pytest.fixture
def agent_config():
    return {
        "system_template": "You are a test assistant.",
        "instance_template": "Task: {{task}}",
    }


@pytest.fixture
def make_bash_output():
    """Build DeterministicToolcallModel outputs from (content, commands) specs."""
    from minisweagent.models.test_models import make_toolcall_output

    def _factory(content, commands):
        tool_calls, actions = [], []
        for i, command in enumerate(commands):
            call_id = f"call_{i}"
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": command}, ensure_ascii=False)},
                }
            )
            actions.append({"command": command, "tool_call_id": call_id})
        return make_toolcall_output(content, tool_calls, actions)

    return _factory


@pytest.fixture
def make_agent(tmp_path, agent_config):
    from minisweagent.environments.local import LocalEnvironment

    from tencentdb_bridge.agent import TencentDBAgent

    def _factory(model, memory: dict | None = None, instance_id="test-instance", **extra_config):
        config = {**agent_config, **extra_config}
        if memory is not None:
            config["memory"] = memory
        return TencentDBAgent(
            model,
            LocalEnvironment(),
            progress_manager=DummyProgressManager(),
            instance_id=instance_id,
            **config,
        )

    return _factory


@pytest.fixture
def make_backend(tmp_path, fake_client):
    from tencentdb_bridge.backend import TencentDBBackend
    from tencentdb_bridge.config import TencentDBConfig

    def _factory(instance_id="test-instance", **overrides):
        output_dir = overrides.pop("output_dir", str(tmp_path / instance_id))
        overrides.setdefault("run_root", str(tmp_path))
        config = TencentDBConfig(enabled=True, output_dir=output_dir, **overrides)
        return TencentDBBackend(config, instance_id)

    return _factory


@pytest.fixture
def no_sleep(monkeypatch):
    """Collect the backend's finalize idle-wait sleeps instead of running them."""
    from tencentdb_bridge.backend import TencentDBBackend

    sleeps: list[float] = []
    monkeypatch.setattr(TencentDBBackend, "_sleep", lambda self, seconds: sleeps.append(seconds))
    return sleeps


@pytest.fixture
def backend_spy(monkeypatch):
    """Capture every TencentDBBackend instance the agent constructs."""
    from tencentdb_bridge.agent import TencentDBAgent
    from tencentdb_bridge.backend import TencentDBBackend

    created = []

    class SpyingBackend(TencentDBBackend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(TencentDBAgent, "backend_class", SpyingBackend)
    return created


# ---------------------------------------------------------------------------
# Deterministic model helpers
# ---------------------------------------------------------------------------
class CapturingToolcallModel:
    """DeterministicToolcallModel that deep-copies the message list per query."""

    def __init__(self, base):
        self._base = base
        self.captured: list[list[dict]] = []

    def __getattr__(self, name):
        return getattr(self._base, name)

    def query(self, messages, **kwargs):
        self.captured.append(copy.deepcopy(messages))
        return self._base.query(messages, **kwargs)


def make_crashing_model(outputs, crash_after, exception_factory=None):
    """DeterministicToolcallModel subclass raising a serializable exception mid-run."""
    from minisweagent.models import GLOBAL_MODEL_STATS
    from minisweagent.models.test_models import DeterministicToolcallModel

    class CrashingToolcallModel(DeterministicToolcallModel):
        def query(self, messages, **kwargs):
            self.current_index += 1
            if self.current_index == crash_after:
                raise (exception_factory or (lambda: RuntimeError("model boom")))()
            output = self.config.outputs[self.current_index]
            GLOBAL_MODEL_STATS.add(self.config.cost_per_call)
            return output

    return CrashingToolcallModel(outputs=outputs)


def scrub_timestamps(messages):
    out = []
    for msg in messages:
        msg = dict(msg)
        extra = msg.get("extra")
        if isinstance(extra, dict):
            msg["extra"] = {k: v for k, v in extra.items() if k != "timestamp"}
        out.append(msg)
    return out


# ---------------------------------------------------------------------------
# Annotation capture server (shared_bridge.testing — one implementation for
# every suite in the bundle)
# ---------------------------------------------------------------------------
@pytest.fixture
def capture_server():
    server = CaptureServer().start()
    yield server
    server.stop()


@pytest.fixture
def traced_backend(tmp_path, capture_server, fake_client, no_sleep):
    """Backend traced against the capture server: the main lane derives from
    the model URL; the memory lane carries no model URL (container-side
    extraction), so its explicit config endpoint resolves via the no-model-URL
    lane rule. Depends on no_sleep so finalizes never idle-wait for real."""
    from tencentdb_bridge.backend import TencentDBBackend
    from tencentdb_bridge.config import TencentDBConfig

    def _factory(**overrides):
        config = TencentDBConfig(
            enabled=True,
            output_dir=str(tmp_path / "inst"),
            run_root=str(tmp_path),
            annotate_memory_url=capture_server.annotate_url("MEMORY"),
            **overrides,
        )
        backend = TencentDBBackend(config, "test-instance", model_base_url=capture_server.lane_url("MAIN"))
        backend.start()
        assert backend._available
        assert backend._trace is not None
        return backend

    return _factory

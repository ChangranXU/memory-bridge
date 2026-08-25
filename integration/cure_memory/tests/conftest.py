"""Shared test fixtures for the CURE memory integration (fully offline)."""

import copy
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from shared_bridge.testing import CaptureServer, TEST_TRAJECTORY_ID  # noqa: F401  (re-exported for test_annotate.py)

CURE_REPO = Path(__file__).resolve().parents[1] / "src"
os.environ.setdefault("CURE_MEMORY_REPO", str(CURE_REPO))

MINI_SWE_AGENT = Path(__file__).resolve().parents[3] / "mini-swe-agent"
SWEBENCH_CONFIG_PATH = MINI_SWE_AGENT / "src/minisweagent/config/benchmarks/swebench.yaml"

SUBMIT_COMMAND = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\necho done"
SENTINEL_EXTRACT_KEY = "sk-v2-test-sentinel-extract-key"


# ---------------------------------------------------------------------------
# Scripted fake decision client (mirrors test_cure_memory_product.py:12-97)
# ---------------------------------------------------------------------------
class ScriptedDecisionClient:
    """Offline duck-type stand-in for ChatGPTMemoryDecisionClient.

    queue: per-call outcomes popped first — a string sets last_error (empty
    decision), a dict is returned verbatim. When the queue is empty, rules
    apply: (substring, fragment_fn) pairs where fragment_fn(message, decision)
    mutates the decision for each request message containing substring.
    """

    model = "fake-extract-model"

    def __init__(self):
        self.requests: list[dict] = []
        self.last_error = None
        self.queue: list = []
        self.rules: list = []

    def decide_memory_updates(self, request):
        self.requests.append(request)
        if self.queue:
            outcome = self.queue.pop(0)
            if isinstance(outcome, str):
                self.last_error = outcome
                return {"candidates": [], "deletions": [], "rejections": []}
            self.last_error = None
            return outcome
        self.last_error = None
        decision = {"candidates": [], "deletions": [], "rejections": []}
        for message in request["messages"]:
            for substring, fragment in self.rules:
                if substring in (message.get("content") or ""):
                    fragment(message, decision)
        return decision


def approved_candidate(message_id, key, value, **overrides):
    """Decision candidate that passes CURE's confidence gating as approved."""
    candidate = {
        "message_id": message_id,
        "memory_type": "fact",
        "scope": "user",
        "key": key,
        "value": value,
        "description": key,
        "confidence": 0.95,
        "review_status": "approved",
        "source_type": "explicit_user",
        "evidence": [value],
        "sensitivity": "private",
        "needs_verification": False,
    }
    candidate.update(overrides)
    return candidate


def on_tool(substring, fragment):
    """Rule tuple gated to tool-role messages so it fires once per trajectory step
    (the assistant Actions block and the tool observation carry the same text)."""

    def gated(message, decision):
        if message.get("role") == "tool":
            fragment(message, decision)

    return (substring, gated)


@pytest.fixture
def extract_env(monkeypatch):
    """Sentinel EXTRACT_* settings so backend.start() can resolve a client."""
    monkeypatch.setenv("EXTRACT_MODEL", "fake-extract-model")
    monkeypatch.setenv("EXTRACT_BASE_URL", "https://extract.invalid/v1")
    monkeypatch.setenv("EXTRACT_API_KEY", SENTINEL_EXTRACT_KEY)
    return {"model": "fake-extract-model", "base_url": "https://extract.invalid/v1", "api_key": SENTINEL_EXTRACT_KEY}


@pytest.fixture
def fake_client(monkeypatch):
    """Inject one ScriptedDecisionClient into every backend started in the test."""
    from cure_memory_bridge.backend import CureMemoryBackend

    client = ScriptedDecisionClient()
    monkeypatch.setattr(CureMemoryBackend, "_make_llm_client", lambda self, settings: client)
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

    from cure_memory_bridge.agent import CureMemoryAgent

    def _factory(model, memory: dict | None = None, instance_id="test-instance", **extra_config):
        config = {**agent_config, **extra_config}
        if memory is not None:
            config["memory"] = memory
        return CureMemoryAgent(
            model,
            LocalEnvironment(),
            progress_manager=DummyProgressManager(),
            instance_id=instance_id,
            **config,
        )

    return _factory


@pytest.fixture
def make_backend(tmp_path, extract_env, fake_client):
    from cure_memory_bridge.backend import CureMemoryBackend
    from cure_memory_bridge.config import CureMemoryConfig

    def _factory(instance_id="test-instance", **overrides):
        output_dir = overrides.pop("output_dir", str(tmp_path / instance_id))
        config = CureMemoryConfig(enabled=True, output_dir=output_dir, **overrides)
        return CureMemoryBackend(config, instance_id)

    return _factory


@pytest.fixture
def traced_backend(tmp_path, capture_server, fake_client):
    """Backend traced against the capture server on both lanes (MAIN + EXTRACT)."""
    from cure_memory_bridge.backend import CureMemoryBackend
    from cure_memory_bridge.config import CureMemoryConfig

    def _factory(**overrides):
        config = CureMemoryConfig(
            enabled=True,
            output_dir=str(tmp_path / "inst"),
            extract_model="fake-extract-model",
            extract_base_url=capture_server.lane_url("EXTRACT"),
            extract_api_key="k",
            **overrides,
        )
        backend = CureMemoryBackend(config, "test-instance", model_base_url=capture_server.lane_url("MAIN"))
        backend.start()
        assert backend._available
        return backend

    return _factory


@pytest.fixture
def backend_spy(monkeypatch):
    """Capture every CureMemoryBackend instance the agent constructs."""
    from cure_memory_bridge.agent import CureMemoryAgent
    from cure_memory_bridge.backend import CureMemoryBackend

    created = []

    class SpyingBackend(CureMemoryBackend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(CureMemoryAgent, "backend_class", SpyingBackend)
    return created


# ---------------------------------------------------------------------------
# Annotation capture server (shared_bridge.testing — one implementation for
# every suite in the bundle)
# ---------------------------------------------------------------------------
@pytest.fixture
def capture_server():
    server = CaptureServer().start()
    yield server
    server.stop()


# ---------------------------------------------------------------------------
# Deterministic model variants
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


def make_format_error_model(outputs, error_at):
    """DeterministicToolcallModel subclass raising FormatError at one query index."""
    from minisweagent.exceptions import FormatError
    from minisweagent.models import GLOBAL_MODEL_STATS
    from minisweagent.models.test_models import DeterministicToolcallModel

    class FlakyToolcallModel(DeterministicToolcallModel):
        def query(self, messages, **kwargs):
            self.current_index += 1
            if self.current_index == error_at:
                raise FormatError(
                    {
                        "role": "user",
                        "content": "format error: no tool calls in response",
                        "extra": {"interrupt_type": "FormatError"},
                    }
                )
            output = self.config.outputs[self.current_index]
            GLOBAL_MODEL_STATS.add(self.config.cost_per_call)
            return output

    return FlakyToolcallModel(outputs=outputs)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def db_rows(db_path, query, params=()):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def assert_db_closed(system):
    with pytest.raises(sqlite3.ProgrammingError):
        system.store.conn.execute("SELECT 1")


def scrub_timestamps(messages):
    out = []
    for msg in messages:
        msg = dict(msg)
        extra = msg.get("extra")
        if isinstance(extra, dict):
            msg["extra"] = {k: v for k, v in extra.items() if k != "timestamp"}
        out.append(msg)
    return out


@pytest.fixture(scope="session")
def swebench_config():
    import yaml

    with open(SWEBENCH_CONFIG_PATH) as f:
        return yaml.safe_load(f)


@pytest.fixture
def progress_manager_factory(tmp_path):
    from minisweagent.run.benchmarks.utils.batch_progress import RunBatchProgressManager

    def _factory(n=1):
        return RunBatchProgressManager(n, tmp_path / f"exit_statuses_{time.time()}.yaml")

    return _factory

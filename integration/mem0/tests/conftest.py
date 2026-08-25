"""Shared test fixtures for the mem0 memory integration (fully offline)."""

import copy
import json

import pytest

from shared_bridge.testing import CaptureServer

SUBMIT_COMMAND = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\necho done"


# ---------------------------------------------------------------------------
# Scripted fake platform client (the backend's _make_platform_client seam)
# ---------------------------------------------------------------------------
class FakePlatformClient:
    """Offline duck-type stand-in for Mem0PlatformClient.

    Behaves like the hosted API surface the backend/endpoint use: add
    returns one ADD result per message (or the scripted error), search
    returns the user's memories in insertion order, and unknown ids 404.
    """

    def __init__(self):
        self.add_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.get_all_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.delete_calls: list[str] = []
        self.memories: dict[str, dict] = {}
        self.closed = False
        self.add_error: Exception | None = None
        self.search_error: Exception | None = None
        self.add_event: str = "ADD"

    def ping(self):
        return {"status": "ok", "org_id": "org-test", "project_id": "proj-test"}

    def add(self, *, messages, user_id, run_id=None, infer=True, metadata=None,
            custom_instructions=None, poll_budget=60.0, poll_interval=1.0):
        self.add_calls.append({
            "messages": [dict(m) for m in messages],
            "user_id": user_id,
            "run_id": run_id,
            "infer": infer,
            "metadata": metadata,
            "custom_instructions": custom_instructions,
        })
        if self.add_error is not None:
            raise self.add_error
        results = []
        for message in messages:
            memory_id = f"m{len(self.memories) + 1}"
            self.memories[memory_id] = {
                "id": memory_id,
                "memory": f"fact: {message['content'][:40]}",
                "user_id": user_id,
                "run_id": run_id,
                "score": 0.9,
                "metadata": {},
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
            results.append({"id": memory_id, "memory": self.memories[memory_id]["memory"], "event": self.add_event})
        return results

    def search(self, *, query, user_id, top_k=10, threshold=0.0, timeout=None):
        self.search_calls.append(
            {"query": query, "user_id": user_id, "top_k": top_k, "threshold": threshold, "timeout": timeout}
        )
        if self.search_error is not None:
            raise self.search_error
        hits = [dict(m) for m in self.memories.values() if m["user_id"] == user_id]
        return hits[:top_k]

    def get_all(self, *, user_id, page_size=100):
        self.get_all_calls.append({"user_id": user_id, "page_size": page_size})
        # The real v3 surfaces split provenance: search rows echo the add-time
        # run_id, while get-all rows omit run_id and carry the same value
        # under session_id — model both, not one row shape for both.
        hits = []
        for memory in self.memories.values():
            if memory["user_id"] != user_id:
                continue
            row = dict(memory)
            run_id = row.pop("run_id", None)
            if run_id is not None:
                row["session_id"] = run_id
            hits.append(row)
        return {"count": len(hits), "next": None, "previous": None, "results": hits}

    def get(self, memory_id):
        memory = self.memories.get(memory_id)
        if memory is None:
            raise _api_error(404, "Memory not found!")
        return dict(memory)

    def update(self, memory_id, *, text=None, metadata=None):
        memory = self.memories.get(memory_id)
        if memory is None:
            raise _api_error(404, "Memory not found!")
        if text is not None:
            memory["memory"] = text
        if metadata is not None:
            memory["metadata"] = metadata
        return dict(memory)

    def delete(self, memory_id):
        if memory_id not in self.memories:
            raise _api_error(404, "Memory not found!")
        del self.memories[memory_id]
        return {"message": "Memory deleted successfully!"}

    def close(self):
        self.closed = True


def _api_error(status_code, reason):
    from mem0_bridge.client import Mem0ApiError

    return Mem0ApiError(status_code, reason)


@pytest.fixture
def fake_client(monkeypatch):
    """Inject one FakePlatformClient into every backend started in the test."""
    from mem0_bridge.backend import Mem0Backend

    client = FakePlatformClient()
    monkeypatch.setattr(Mem0Backend, "_make_platform_client", lambda self, settings: client)
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

    from mem0_bridge.agent import Mem0Agent

    def _factory(model, memory: dict | None = None, instance_id="test-instance", **extra_config):
        config = {**agent_config, **extra_config}
        if memory is not None:
            config["memory"] = memory
        return Mem0Agent(
            model,
            LocalEnvironment(),
            progress_manager=DummyProgressManager(),
            instance_id=instance_id,
            **config,
        )

    return _factory


@pytest.fixture
def make_backend(tmp_path, fake_client):
    from mem0_bridge.backend import Mem0Backend
    from mem0_bridge.config import Mem0Config

    def _factory(instance_id="test-instance", **overrides):
        output_dir = overrides.pop("output_dir", str(tmp_path / instance_id))
        overrides.setdefault("api_key", "test-key")
        config = Mem0Config(enabled=True, output_dir=output_dir, **overrides)
        return Mem0Backend(config, instance_id)

    return _factory


@pytest.fixture
def backend_spy(monkeypatch):
    """Capture every Mem0Backend instance the agent constructs."""
    from mem0_bridge.agent import Mem0Agent
    from mem0_bridge.backend import Mem0Backend

    created = []

    class SpyingBackend(Mem0Backend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(Mem0Agent, "backend_class", SpyingBackend)
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
def traced_backend(tmp_path, capture_server, fake_client):
    """Backend traced against the capture server: the main lane derives from
    the model URL; the memory lane carries no model URL (hosted extraction),
    so its explicit config endpoint resolves via the no-model-URL lane rule."""
    from mem0_bridge.backend import Mem0Backend
    from mem0_bridge.config import Mem0Config

    def _factory(**overrides):
        config = Mem0Config(
            enabled=True,
            output_dir=str(tmp_path / "inst"),
            api_key="test-key",
            annotate_memory_url=capture_server.annotate_url("MEMORY"),
            **overrides,
        )
        backend = Mem0Backend(config, "test-instance", model_base_url=capture_server.lane_url("MAIN"))
        backend.start()
        assert backend._available
        assert backend._trace is not None
        return backend

    return _factory

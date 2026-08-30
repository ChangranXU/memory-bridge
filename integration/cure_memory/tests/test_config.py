"""T2: config mechanics."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from cure_memory_bridge.config import CureMemoryConfig

SENTINEL = "sk-v2-config-sentinel"


def test_defaults():
    cfg = CureMemoryConfig()
    assert cfg.enabled is False
    assert cfg.scope == "run"
    assert cfg.user_id == "minisweagent"
    assert cfg.output_dir == ""
    assert cfg.strict is False
    assert cfg.max_message_chars == 4000
    assert cfg.extract_every_n_steps == 10
    assert cfg.extract_max_tokens == 1600
    assert cfg.extract_reasoning_effort == "low"
    assert cfg.extract_max_consecutive_errors == 3
    assert cfg.inject_recall is True
    assert cfg.max_memories == 10
    assert cfg.max_chars_per_memory == 0
    assert cfg.max_total_recall_chars == 2000


def test_output_dir_required_when_enabled():
    with pytest.raises(ValidationError, match="output_dir"):
        CureMemoryConfig(enabled=True)
    with pytest.raises(ValidationError, match="output_dir"):
        CureMemoryConfig(enabled=True, output_dir="   ")
    # disabled arm needs no output_dir
    assert CureMemoryConfig().output_dir == ""


def test_output_dir_is_stripped():
    """Surrounding whitespace is normalized away (user_id already is), never
    preserved into a spaced relative artifact path."""
    assert CureMemoryConfig(enabled=True, output_dir="  /tmp/x  ").output_dir == "/tmp/x"


def test_blank_user_id_fails():
    with pytest.raises(ValidationError, match="user_id"):
        CureMemoryConfig(user_id="   ")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_message_chars", 0),
        ("extract_max_tokens", 0),
        ("extract_every_n_steps", -1),
        ("extract_timeout", 0.0),
        ("extract_max_retries", -1),
        ("extract_max_consecutive_errors", -1),
        ("max_memories", 0),
        ("max_chars_per_memory", -1),
        ("max_total_recall_chars", -1),
    ],
)
def test_invalid_numeric_bounds_fail(field, value):
    with pytest.raises(ValidationError):
        CureMemoryConfig(**{field: value})


@pytest.mark.parametrize(
    ("field",),
    [
        ("extract_every_n_steps",),
        ("extract_max_consecutive_errors",),
        ("extract_max_retries",),
        ("max_chars_per_memory",),
        ("max_total_recall_chars",),
    ],
)
def test_zero_is_valid_for_documented_disable_switches(field):
    assert getattr(CureMemoryConfig(**{field: 0}), field) == 0


def test_unknown_memory_keys_fail():
    with pytest.raises(ValidationError):
        CureMemoryConfig(unknown_key=True)


def test_stock_agent_config_ignores_memory_subtree():
    from minisweagent.agents.default import AgentConfig

    cfg = AgentConfig(system_template="s", instance_template="i", memory={"enabled": True, "output_dir": "/tmp"})
    assert not hasattr(cfg, "memory")


def test_cure_memory_agent_config_populates_memory():
    from cure_memory_bridge.agent import CureMemoryAgentConfig

    cfg = CureMemoryAgentConfig(
        system_template="s",
        instance_template="i",
        memory={"enabled": True, "output_dir": "/tmp", "scope": "instance"},
    )
    assert cfg.memory.enabled is True
    assert cfg.memory.output_dir == "/tmp"
    assert cfg.memory.scope == "instance"


def test_db_path_derivation(tmp_path):
    from cure_memory_bridge.backend import CureMemoryBackend

    instance_backend = CureMemoryBackend(
        CureMemoryConfig(enabled=True, output_dir=str(tmp_path / "inst"), scope="instance"), "id"
    )
    run_backend = CureMemoryBackend(
        CureMemoryConfig(enabled=True, output_dir=str(tmp_path / "runs" / "id"), scope="run"), "id"
    )
    assert instance_backend._derive_db_path() == (tmp_path / "inst" / "cure_memory.sqlite3").resolve()
    assert run_backend._derive_db_path() == (tmp_path / "runs" / "cure_memory.sqlite3").resolve()


def test_extract_settings_config_overrides_env(tmp_path, monkeypatch):
    from cure_memory_bridge.backend import CureMemoryBackend

    monkeypatch.setenv("EXTRACT_MODEL", "env-model")
    monkeypatch.setenv("EXTRACT_BASE_URL", "https://env.invalid/v1")
    monkeypatch.setenv("EXTRACT_API_KEY", "env-key")
    backend = CureMemoryBackend(CureMemoryConfig(enabled=True, output_dir=str(tmp_path)), "id")
    assert backend._resolve_settings() == {
        "model": "env-model",
        "base_url": "https://env.invalid/v1",
        "api_key": "env-key",
    }
    configured = CureMemoryBackend(
        CureMemoryConfig(
            enabled=True,
            output_dir=str(tmp_path),
            extract_model="cfg-model",
            extract_base_url="https://cfg.invalid/v1",
            extract_api_key="cfg-key",
        ),
        "id",
    )
    assert configured._resolve_settings() == {
        "model": "cfg-model",
        "base_url": "https://cfg.invalid/v1",
        "api_key": "cfg-key",
    }


@pytest.mark.parametrize(
    ("env",),
    [
        ({"EXTRACT_MODEL": "m"},),
        ({"EXTRACT_BASE_URL": "https://b.invalid/v1"},),
        ({"EXTRACT_API_KEY": "k"},),
        ({"EXTRACT_MODEL": "  ", "EXTRACT_BASE_URL": "https://b.invalid/v1", "EXTRACT_API_KEY": "k"},),
    ],
)
def test_extract_settings_missing_any_of_three_refuses_client(tmp_path, monkeypatch, env):
    """Any missing/blank field -> unavailable, client constructor never reached."""
    from cure_memory_bridge.backend import CureMemoryBackend

    for var in ("EXTRACT_MODEL", "EXTRACT_BASE_URL", "EXTRACT_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    spy_calls = []
    monkeypatch.setattr(
        CureMemoryBackend, "_make_llm_client", lambda self, settings: spy_calls.append(settings)
    )
    backend = CureMemoryBackend(CureMemoryConfig(enabled=True, output_dir=str(tmp_path)), "id")
    backend.start()
    assert backend._available is False
    assert spy_calls == []
    data = json.loads((tmp_path / "memory.json").read_text())
    assert data["available"] is False
    assert any(event["kind"] == "error" and event.get("op") == "start" for event in data["events"])


def test_secret_api_key_never_serialized(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    """Sentinel key must be absent from repr, both model_dump modes, and agent config dumps."""
    from minisweagent.models.test_models import DeterministicToolcallModel

    cfg = CureMemoryConfig(enabled=True, output_dir=str(tmp_path), extract_api_key=SENTINEL)
    assert SENTINEL not in repr(cfg)
    assert SENTINEL not in json.dumps(cfg.model_dump())
    assert SENTINEL not in json.dumps(cfg.model_dump(mode="json"))
    assert "extract_api_key" not in cfg.model_dump()

    model = DeterministicToolcallModel(
        outputs=[make_bash_output("s", ["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\necho done"])]
    )
    agent = make_agent(
        model,
        memory={"enabled": True, "output_dir": str(tmp_path / "inst"), "scope": "instance", "extract_api_key": SENTINEL},
    )
    agent.run("task")
    serialized = json.dumps(agent.serialize())
    assert SENTINEL not in serialized
    assert "extract_api_key" not in serialized


def test_lane_urls_never_serialized(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    """A hand-set extract/rewrite base URL embeds the bearer trajectory ID
    (rule 4, like the annotate URLs): absent from repr, both model_dump modes,
    and the serialized agent config; memory.json keeps the sanitized form."""
    from minisweagent.models.test_models import DeterministicToolcallModel

    bearer = "bearer-secret-traj-id"
    url = f"http://h/EXTRACT/trajectories/{bearer}/v1"
    cfg = CureMemoryConfig(enabled=True, output_dir=str(tmp_path), extract_base_url=url, rewrite_base_url=url)
    assert bearer not in repr(cfg)
    assert bearer not in json.dumps(cfg.model_dump())
    assert bearer not in json.dumps(cfg.model_dump(mode="json"))
    assert "extract_base_url" not in cfg.model_dump()
    assert "rewrite_base_url" not in cfg.model_dump()

    model = DeterministicToolcallModel(
        outputs=[make_bash_output("s", ["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\necho done"])]
    )
    agent = make_agent(
        model,
        memory={
            "enabled": True,
            "output_dir": str(tmp_path / "inst"),
            "scope": "instance",
            "extract_base_url": url,
            "rewrite_base_url": url,
        },
    )
    agent.run("task")
    serialized = json.dumps(agent.serialize())
    assert bearer not in serialized
    assert "extract_base_url" not in serialized
    assert "rewrite_base_url" not in serialized
    # The artifact keeps the sanitized record instead.
    data = json.loads((tmp_path / "inst" / "memory.json").read_text())
    assert bearer not in json.dumps(data)
    hashed = hashlib.sha256(bearer.encode()).hexdigest()[:16]
    assert data["settings"]["extract_base_url"] == f"http://h/EXTRACT/trajectories/{hashed}/v1"

"""Config defaults, env fallback, and shared validators."""

import pytest
from pydantic import ValidationError

from mem0_bridge.config import Mem0Config


def test_defaults():
    config = Mem0Config()
    assert config.mode == "platform"
    assert config.api_key == ""
    assert config.base_url == ""
    assert config.server_url == ""
    assert config.server_api_key == ""
    assert config.run_root == ""
    assert config.infer is True
    assert config.search_threshold == 0.0
    assert config.poll_budget == 60.0
    assert config.poll_interval == 1.0
    assert config.scope == "run"
    assert config.enabled is False
    assert config.user_id == "minisweagent"


def test_credential_fields_excluded_from_dumps():
    """api_key and server_api_key are credential fields (rule 4): excluded
    from dumps and hidden from reprs."""
    config = Mem0Config(api_key="k1", server_api_key="k2")
    dumped = config.model_dump()
    assert "api_key" not in dumped and "server_api_key" not in dumped
    assert "k1" not in repr(config) and "k2" not in repr(config)


def test_api_key_env_fallback_is_backend_side():
    import os

    assert "MEM0_API_KEY" not in os.environ or Mem0Config().api_key in ("", os.environ["MEM0_API_KEY"])


def test_enabled_requires_output_dir():
    with pytest.raises(ValidationError, match="output_dir"):
        Mem0Config(enabled=True, output_dir="   ")


def test_blank_user_id_rejected():
    with pytest.raises(ValidationError, match="user_id"):
        Mem0Config(enabled=True, output_dir="/tmp/x", user_id="  ")


def test_unknown_keys_forbidden():
    with pytest.raises(ValidationError):
        Mem0Config(apikey="typo")


def test_search_threshold_bounds():
    with pytest.raises(ValidationError):
        Mem0Config(search_threshold=1.5)


def test_defaults_yaml_overlay_parses():
    from pathlib import Path

    import yaml

    overlay_path = Path(__file__).resolve().parents[1] / "configs" / "memory_defaults.yaml"
    overlay = yaml.safe_load(overlay_path.read_text())
    config = Mem0Config(**overlay["agent"]["memory"])
    assert config.enabled is False
    assert config.poll_budget == 120
    assert config.infer is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("db_path", "/tmp/x.sqlite3"),
        ("extract_model", "m"),
        ("extract_base_url", "https://b.invalid/v1"),
        ("extract_api_key", "k"),
        ("extract_max_tokens", 100),
        ("extract_reasoning_effort", "low"),
        ("extract_timeout", 30),
        ("extract_max_retries", 1),
    ],
)
def test_local_extraction_fields_rejected(field, value):
    """The local extraction client belongs to integrations with their own
    memory lane; mem0's hosted arm carries none, so these keys must fail
    loudly instead of being silently inert."""
    with pytest.raises(ValidationError):
        Mem0Config(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("annotate", False),
        ("annotate_main_url", "http://h/annotate"),
        ("annotate_memory_url", "http://h/annotate"),
        ("annotate_timeout", 0.5),
        ("annotate_retries", 1),
        ("annotate_max_consecutive_errors", 3),
    ],
)
def test_annotation_fields_accepted(field, value):
    """The trajectory-annotation fields are shared (MemoryConfig): mem0 traces
    through the proxy like every integration, so they parse here too."""
    assert getattr(Mem0Config(**{field: value}), field) == value


def test_annotation_shared_defaults():
    config = Mem0Config()
    assert config.annotate is True
    assert config.annotate_main_url == ""
    assert config.annotate_memory_url == ""
    assert config.annotate_timeout == 0.5
    assert config.annotate_retries == 1
    assert config.annotate_max_consecutive_errors == 3


def test_annotate_urls_are_credential_fields():
    """The annotate URLs embed the bearer trajectory ID, so they follow rule
    4 like api_key: excluded from dumps and hidden from reprs."""
    import json

    url = "http://h/MAIN/trajectories/bearer-secret-id/annotate"
    config = Mem0Config(annotate_main_url=url, annotate_memory_url=url)
    assert "bearer-secret-id" not in repr(config)
    dumped = config.model_dump()
    assert "annotate_main_url" not in dumped and "annotate_memory_url" not in dumped
    assert "bearer-secret-id" not in json.dumps(dumped)

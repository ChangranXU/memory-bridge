"""Config defaults, validation, and the arm overlay."""

import pathlib

import pytest
from pydantic import ValidationError

from tencentdb_bridge.config import TencentDBConfig


def test_defaults():
    config = TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r")
    assert config.endpoint == "http://127.0.0.1:8420"
    assert config.service_id == "default"
    assert config.drain_budget == 180.0
    assert config.finalize_drain_budget == 300.0
    assert config.drain_interval == 1.0
    assert config.embedding_provider == "none"
    assert config.recall_min_score is None  # never inherit a floor
    assert config.add_timeout == 300.0
    assert config.conversation_search_limit == 5


def test_unknown_keys_rejected():
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", tencent_api_key="k")


def test_scene_index_knobs_removed():
    # The L2 section renders unbounded (native parity): the two bridge-local
    # index caps are gone, so extra="forbid" rejects them as unknown keys.
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", scene_summary_chars=100)
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", scene_index_limit=5)


def test_max_context_chars_removed():
    # The single budget knob was replaced by the native pair
    # (max_chars_per_memory / max_total_recall_chars): a leftover overlay pin
    # fails loudly as an unknown key.
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", max_context_chars=2000)


def test_l1_idle_timeout_removed():
    # The arm-side copy is gone (the backend resolves the generated gateway
    # yaml at start — the single source of truth): a leftover overlay pin
    # fails loudly as an unknown key.
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", l1_idle_timeout=30)


def test_enabled_requires_output_dir():
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, run_root="/tmp/r")


def test_blank_user_id_rejected():
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", user_id="  ")


def test_drain_budgets_must_be_positive():
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", drain_budget=0)
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", finalize_drain_budget=-1)


def test_conversation_search_limit_bounds():
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", conversation_search_limit=0)
    with pytest.raises(ValidationError):
        TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", conversation_search_limit=101)


def test_conversation_search_limit_persists_to_settings(make_backend):
    backend = make_backend(conversation_search_limit=7)
    backend.start()
    assert backend._settings["conversation_search_limit"] == 7


def test_api_key_is_credential_field():
    config = TencentDBConfig(enabled=True, output_dir="/tmp/x", run_root="/tmp/r", api_key="secret")
    dumped = config.model_dump()
    assert "api_key" not in dumped
    assert "secret" not in repr(config)


def test_arm_overlay_parses_with_pinned_values(tmp_path):
    import yaml

    overlay = yaml.safe_load(
        pathlib.Path(__file__).resolve().parents[1].joinpath("configs/memory_defaults.yaml").read_text()
    )
    memory = overlay["agent"]["memory"]
    assert memory["enabled"] is False
    config = TencentDBConfig(output_dir="/tmp/x", run_root="/tmp/r", **memory)
    assert config.scope == "run"
    assert config.extract_every_n_steps == 10  # the cross-arm bridge-tick cadence
    assert config.extract_max_consecutive_errors == 3
    assert config.rewrite_every_n_steps == 10
    assert config.inject_recall is True
    assert config.max_memories == 10
    assert config.max_message_chars == 4000
    assert config.max_total_recall_chars == 2000  # the shipped total render bound
    assert config.max_chars_per_memory == 0  # per-line cap off (the native default)
    # The overlay raises the drain/add ceilings over the config defaults:
    # one tick chains 2-3 serial L1 cycles (one cycle consumes at most 10 L0
    # rows; the 2N=20 over-fetch is backlog detection) and, with the vector
    # lane degraded, one cycle was observed at ~185 s — a 300 s budget loses
    # that chain by seconds.
    assert config.drain_budget == 600
    assert config.finalize_drain_budget == 600
    assert config.add_timeout == 600
    assert config.drain_interval == 1.0
    assert config.recall_min_score is None
    assert config.search_timeout == 30  # query-embed ceiling when the vector lane is on

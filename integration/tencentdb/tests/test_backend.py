"""Backend behavior tests (fake gateway client, fully offline)."""

import json
from pathlib import Path

import pytest

from tencentdb_bridge.backend import _NAV_MARKER, _UNKNOWN_ORIGIN, _parse_scene_nav


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
class _RaisingClient:
    def __init__(self, error):
        self._error = error

    def health(self):
        raise self._error

    def close(self):
        pass


def test_start_fail_closed_on_unreachable_gateway(make_backend, monkeypatch):
    from tencentdb_bridge.client import TencentDBApiError

    backend = make_backend()
    monkeypatch.setattr(
        type(backend), "_make_client", lambda self, settings: _RaisingClient(TencentDBApiError(503, "unreachable"))
    )
    backend.start()
    assert backend._available is False
    assert backend._client is None
    assert any(event["kind"] == "error" and event.get("op") == "start" for event in backend._events)


def test_start_fail_closed_when_run_root_missing(tmp_path, fake_client):
    from tencentdb_bridge.backend import TencentDBBackend
    from tencentdb_bridge.config import TencentDBConfig

    backend = TencentDBBackend(
        TencentDBConfig(enabled=True, output_dir=str(tmp_path / "inst")), "test-instance"
    )
    backend.start()
    assert backend._available is False


def test_start_success_writes_sidecar_and_settings(make_backend, fake_client, tmp_path):
    backend = make_backend(instance_id="pydata__xarray-2905")
    backend.start()
    assert backend._available is True
    sidecar = tmp_path / "tdai" / "episodes.jsonl"
    records = [json.loads(line) for line in sidecar.read_text().splitlines()]
    assert records[0]["event"] == "start"
    assert records[0]["instance_id"] == "pydata__xarray-2905"
    assert records[0]["session_id"] == backend._session_id
    assert backend._session_id.startswith("pydata__xarray-2905-")
    # Settings carry the pinned gateway config but never a credential.
    settings = backend._settings
    assert settings["prompt_mode"] == "code"
    assert settings["l1_idle_timeout"] == 30.0  # resolved from the fixture gateway yaml
    assert settings["l1_idle_timeout_source"] == "gateway-yaml"  # the resolution, not a guess
    assert settings["bm25_language"] == "en"
    assert settings["team_id"] == "minisweagent"
    assert settings["agent_id"] == "memory-bridge"
    assert "api_key" not in settings
    assert settings["extraction_guidelines"] == ""  # no conveyance channel


def test_sidecar_failure_degrades_never_fails(make_backend, fake_client, tmp_path):
    # Attribution is observability: an unwritable sidecar (episodes.jsonl's
    # path is a DIRECTORY, so every read/append fails) logs and degrades
    # origins to the "unknown" sentinel — start must still succeed.
    (tmp_path / "tdai" / "episodes.jsonl").mkdir()
    backend = make_backend()
    backend.start()
    assert backend._available is True
    assert backend._origin_session("2026-01-01T00:00:00Z") == ""  # no windows loadable


# ---------------------------------------------------------------------------
# Gateway-yaml idle-timeout resolution (the single source of truth)
# ---------------------------------------------------------------------------
def test_start_fails_loudly_without_the_gateway_yaml(make_backend, tmp_path):
    """A missing gateway yaml fails the start — no guessed idle timeout ever
    frames the finalize wait (silently guessing is the divergence the
    single-source readback removes)."""
    backend = make_backend(run_root=str(tmp_path / "no-yaml"))
    backend.start()
    assert backend._available is False
    errors = [event for event in backend._events if event["kind"] == "error" and event.get("op") == "start"]
    assert errors and "tdai-gateway.yaml" in errors[0]["error"]


def test_start_fails_loudly_when_the_idle_timeout_key_is_missing(make_backend, tmp_path):
    (tmp_path / "tdai" / "tdai-gateway.yaml").write_text("memory:\n  pipeline:\n    everyNConversations: 5\n")
    backend = make_backend()
    backend.start()
    assert backend._available is False
    errors = [event for event in backend._events if event["kind"] == "error" and event.get("op") == "start"]
    assert errors and "l1IdleTimeoutSeconds" in errors[0]["error"]


def test_start_fails_loudly_on_a_non_numeric_idle_timeout(make_backend, gateway_yaml):
    gateway_yaml.write_text("memory:\n  pipeline:\n    l1IdleTimeoutSeconds: soon\n")
    backend = make_backend()
    backend.start()
    assert backend._available is False
    errors = [event for event in backend._events if event["kind"] == "error" and event.get("op") == "start"]
    assert errors and "l1IdleTimeoutSeconds" in errors[0]["error"]


def test_start_fails_loudly_on_a_negative_idle_timeout(make_backend, gateway_yaml):
    # A negative value is numeric but not a timeout: left through, it would
    # detonate at the finalize drain's sleep instead of failing the start.
    gateway_yaml.write_text("memory:\n  pipeline:\n    l1IdleTimeoutSeconds: -30\n")
    backend = make_backend()
    backend.start()
    assert backend._available is False
    errors = [event for event in backend._events if event["kind"] == "error" and event.get("op") == "start"]
    assert errors and "l1IdleTimeoutSeconds" in errors[0]["error"]


def test_start_idle_timeout_resolution_raises_under_strict(make_backend, tmp_path):
    """The yaml failures fail the start under strict too (loud in both modes —
    contained unavailable non-strict, raised strict)."""
    from shared_bridge.backend import _BackendUnavailable

    backend = make_backend(run_root=str(tmp_path / "no-yaml"), strict=True)
    with pytest.raises(_BackendUnavailable, match="tdai-gateway.yaml"):
        backend.start()
    assert backend._available is False


# ---------------------------------------------------------------------------
# Role folding
# ---------------------------------------------------------------------------
def test_role_folding_in_add_bodies(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.record([{"role": "system", "content": "sys prompt"}], step=0)
    backend.record([{"role": "user", "content": "task"}], step=0)
    backend.record([{"role": "assistant", "content": "thinking", "extra": {"actions": [{"command": "ls"}]}}], step=1)
    backend.record([{"role": "tool", "content": "file list", "tool_call_id": "call_0"}], step=1)
    backend.maybe_extract(10)
    body = fake_client.add_calls[0]
    roles = [message["role"] for message in body["messages"]]
    assert roles == ["user", "user", "assistant", "user"]  # system/tool fold; user round per step


def test_scope_instance_suffixes_user_id(make_backend, fake_client):
    backend = make_backend(scope="instance", instance_id="pydata__xarray-2905")
    backend.start()
    assert backend.effective_user_id() == "minisweagent:pydata__xarray-2905"


def test_make_client_wires_endpoint_service_id_and_key(tmp_path):
    # The REAL _make_client (every other test monkeypatches it): a dropped
    # service_id or an ignored settings["endpoint"] fails the real container
    # at parseV2Auth, not any fake — pin the wiring here.
    from tencentdb_bridge.backend import TencentDBBackend
    from tencentdb_bridge.client import TencentDBClient
    from tencentdb_bridge.config import TencentDBConfig

    config = TencentDBConfig(
        enabled=True, output_dir=str(tmp_path / "o"), run_root=str(tmp_path), service_id="svc-x", api_key="k"
    )
    backend = TencentDBBackend(config, "test-instance")
    client = backend._make_client({"endpoint": "http://127.0.0.1:8420/"})
    try:
        assert isinstance(client, TencentDBClient)
        assert client._endpoint == "http://127.0.0.1:8420"  # trailing slash stripped
        assert client._headers["x-tdai-service-id"] == "svc-x"
        assert client._headers["Authorization"] == "Bearer k"
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Extraction tick
# ---------------------------------------------------------------------------
def _record_steps(backend, steps):
    for step, messages in steps:
        backend.record(messages, step=step)


def test_extraction_tick_success_immediate_idle(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    _record_steps(backend, [(i, [{"role": "user", "content": f"step {i}"}]) for i in range(12)])
    backend.maybe_extract(10)
    assert backend._counts["extraction_calls"] == 1
    assert fake_client.add_calls, "the buffered messages must be flushed"
    assert fake_client.drain_calls == 1  # the tick drained L1 once
    # The watermark query resolved produced rows: every fresh row is version 0.
    assert backend._counts["memories_added"] == len(fake_client.rows)
    assert backend._counts["memories_updated"] == 0
    assert backend._pending == []


def test_extraction_tick_no_pending_is_not_counted(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.maybe_extract(10)
    assert backend._counts["extraction_calls"] == 0
    assert fake_client.add_calls == []


def test_drain_timeout_is_extraction_failure_and_breaker(make_backend, fake_client, no_sleep):
    backend = make_backend(extract_max_consecutive_errors=2)
    backend.start()
    fake_client.idle_answers = [False]  # every drain attempt fails within (scripted) budget
    backend.record([{"role": "user", "content": "m1"}], step=1)
    backend.maybe_extract(10)
    assert backend._counts["extraction_errors"] == 1
    # The add itself succeeded (L0 persisted server-side): the buffer is
    # cleared — a re-add would re-feed the pipeline wholesale — and the
    # unresolved watermark survives for the next successful resolve.
    assert backend._pending == []
    assert backend._watermark is not None
    fake_client.idle_answers = [False]
    backend.record([{"role": "user", "content": "m2"}], step=11)
    backend.maybe_extract(20)
    assert backend._extract_breaker is True
    # The breaker gates periodic ticks only; the final flush still runs.
    fake_client.idle_answers = []
    backend.finalize()
    assert backend._counts["extraction_calls"] == 3  # two failed ticks + the final flush


def test_drain_failure_keeps_watermark_for_next_resolve(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    fake_client.idle_answers = [False]
    backend.record([{"role": "user", "content": "m1"}], step=1)
    backend.maybe_extract(10)  # add ok, drain failed: rows exist but uncounted
    assert backend._counts["memories_added"] == 0
    fake_client.idle_answers = []
    backend.record([{"role": "user", "content": "m2"}], step=11)
    backend.maybe_extract(20)  # resolves the whole production window
    assert backend._counts["memories_added"] == len(fake_client.rows)
    # The window never narrows: rows stay re-visible to later resolves, and
    # the (id, version) dedup set counts each produced row exactly once.
    assert backend._watermark is not None
    backend.record([{"role": "user", "content": "m3"}], step=21)
    backend.maybe_extract(30)
    # The re-resolved earlier rows are deduped; only the new row counts.
    assert backend._counts["memories_added"] == len(fake_client.rows)


def test_below_threshold_tail_landing_after_the_tick_is_counted(make_backend, fake_client, no_sleep):
    """A below-threshold tick arms the L1 idle timer, which the status API
    never exposes: the drain sees idle and the resolve returns 0 rows. The
    timer-fired tail lands later and must still be counted — here by the
    finalize resolve, with the pending buffer already empty."""
    backend = make_backend()
    backend.start()
    fake_client.auto_produce = False  # nothing produced synchronously
    backend.record([{"role": "user", "content": "m1"}], step=1)
    backend.maybe_extract(10)
    assert backend._counts["memories_added"] == 0
    assert backend._watermark is not None  # the window stays open for the tail
    # The armed timer fires server-side: the tail lands after the tick.
    fake_client.rows.append(
        {
            "id": "a9",
            "type": "atomic",
            "content": "tail fact",
            "version": 0,
            "created_at": _stamp(),
            "updated_at": _stamp(),
            "task_id": "test-instance",
        }
    )
    backend.finalize()  # pending already empty: drain-only final flush
    assert backend._counts["extraction_errors"] == 0
    assert backend._counts["memories_added"] == 1


def test_store_delta_counters_version_split(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.record([{"role": "user", "content": "fresh"}], step=1)
    backend.maybe_extract(10)
    assert backend._counts["memories_added"] == 1
    assert backend._counts["memories_updated"] == 0
    # A dedup merge rewrites the row: the second tick's pipeline produces a
    # version-1 row (fresh rows carry version 0).
    fake_client.rows.clear()
    fake_client.next_version = 1
    backend.record([{"role": "user", "content": "again"}], step=11)
    backend.maybe_extract(20)
    assert backend._counts["memories_added"] == 1
    assert backend._counts["memories_updated"] == 1
    assert "memories_deleted" not in backend._counts  # dedup deletes are unobservable


def _stamp(offset_seconds=0) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Finalize drain (idle-timer-aware)
# ---------------------------------------------------------------------------
def test_finalize_pays_one_idle_wait_and_records_drain(make_backend, fake_client, no_sleep):
    backend = make_backend()
    backend.start()
    backend.record([{"role": "user", "content": "tail"}], step=1)
    backend.finalize()
    # Unconditional idle wait: the fixture yaml's 30 + margin, exactly once.
    assert no_sleep == [pytest.approx(35.0)]
    assert fake_client.drain_calls == 2  # before and after the idle wait
    sidecar = json.loads(Path(backend.config.run_root, "tdai", "episodes.jsonl").read_text().splitlines()[-1])
    assert sidecar["event"] == "drain"
    assert sidecar["session_id"] == backend._session_id


def test_finalize_idle_wait_derives_from_the_gateway_yaml(make_backend, fake_client, no_sleep, gateway_yaml):
    """The finalize wait is the yaml's value, not a host-side copy: a
    different l1IdleTimeoutSeconds flows straight into the drain and the
    settings artifact."""
    gateway_yaml.write_text("memory:\n  pipeline:\n    l1IdleTimeoutSeconds: 45\n")
    backend = make_backend()
    backend.start()
    assert backend._settings["l1_idle_timeout"] == 45.0
    assert backend._settings["l1_idle_timeout_source"] == "gateway-yaml"
    backend.record([{"role": "user", "content": "tail"}], step=1)
    backend.finalize()
    assert no_sleep == [pytest.approx(50.0)]  # 45 + margin (5), exactly once


def test_finalize_with_empty_pending_but_prior_adds_still_drains(make_backend, fake_client, no_sleep):
    # The last tick flushed everything but its below-threshold add armed the
    # idle timer — finalize must still wait it out (the armed-timer case).
    backend = make_backend()
    backend.start()
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.maybe_extract(10)
    assert backend._pending == []
    backend.finalize()
    assert no_sleep == [pytest.approx(35.0)]
    assert backend._counts["extraction_calls"] == 2  # the tick + the final (drain-only)
    # The final resolve re-sees the tick's row through the open window; the
    # dedup set counts it exactly once.
    assert backend._counts["memories_added"] == 1


def test_finalize_without_any_adds_is_not_counted(make_backend, fake_client, no_sleep):
    backend = make_backend()
    backend.start()
    backend.finalize()
    assert backend._counts["extraction_calls"] == 0
    assert no_sleep == []


def test_chained_finalize_drains_both_waits(make_backend, fake_client, no_sleep):
    # An earlier tick's L1 task still draining when finalize starts, plus a
    # below-threshold final add arming a second timer: the finalize must run
    # BOTH waits plus the unconditional idle sleep (the two-cycle chain the
    # finalize budget exists for), and no error is counted.
    backend = make_backend()
    backend.start()
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.maybe_extract(10)
    backend.record([{"role": "user", "content": "tail"}], step=11)
    fake_client.idle_answers = [True, True]
    backend.finalize()
    assert backend._counts["extraction_errors"] == 0
    assert no_sleep == [pytest.approx(35.0)]
    assert fake_client.drain_calls == 3  # the tick + the finalize's two waits


def test_finalize_drain_overrun_is_one_extraction_failure(make_backend, fake_client, no_sleep):
    backend = make_backend()
    backend.start()
    backend.record([{"role": "user", "content": "m"}], step=1)
    fake_client.idle_answers = [True, False]  # first poll ok, post-idle poll never idles
    backend.finalize()
    assert backend._counts["extraction_errors"] == 1
    assert backend._counts["extraction_calls"] == 1
    # The drain record was still written (the window boundary is when the
    # episode stopped waiting).
    sidecar = json.loads(Path(backend.config.run_root, "tdai", "episodes.jsonl").read_text().splitlines()[-1])
    assert sidecar["event"] == "drain"


def test_finalize_tail_drain_gets_a_fresh_tick_budget(make_backend, fake_client, no_sleep):
    """The post-idle-wait drain must not ride the finalize deadline remainder:
    the unconditional sleep can consume it entirely, and the timer-fired tail
    task then needs a full L1 cycle (drain_budget), not a ~0 best-effort poll."""
    backend = make_backend(finalize_drain_budget=0.01, drain_budget=300.0)
    backend.start()
    backend.record([{"role": "user", "content": "m"}], step=1)
    fake_client.idle_answers = [True, True]
    backend.finalize()
    assert backend._counts["extraction_errors"] == 0
    assert len(fake_client.drain_budgets) == 2
    assert fake_client.drain_budgets[0] <= 0.01  # the deadline-bounded backlog drain
    assert fake_client.drain_budgets[1] == 300.0  # the tail drain: a fresh per-tick budget


def test_failed_add_retains_only_the_unconfirmed_tail(make_backend, fake_client):
    """A mid-chunk conversation/add failure has its earlier chunks confirmed
    server-side: the retry buffer keeps only the uncertain tail, never
    re-feeding the confirmed prefix (wholesale duplicates + double-counted
    user-rounds)."""
    from tencentdb_bridge.client import TencentDBApiError

    backend = make_backend()
    backend.start()
    for i in range(5):
        backend.record([{"role": "user", "content": f"m{i}"}], step=i + 1)
    error = TencentDBApiError(503, "gateway transport error: mid-chunk")
    error.persisted_messages = 3  # the first chunk(s) returned success
    fake_client.add_error = error
    backend.maybe_extract(10)
    assert backend._counts["extraction_errors"] == 1
    assert [m["content"] for m in backend._pending] == ["m3", "m4"]
    # The next tick re-adds only the retained tail.
    fake_client.add_error = None
    backend.record([{"role": "user", "content": "m5"}], step=6)
    backend.maybe_extract(20)
    assert [m["content"] for m in fake_client.add_calls[1]["messages"]] == ["m3", "m4", "m5"]


# ---------------------------------------------------------------------------
# Recall: L1 hits (L2 index, L3 persona)
# ---------------------------------------------------------------------------
def test_recall_renders_l1_hits_with_provenance(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.search_hits = [
        {"id": "a1", "content": "run pytest with -k", "score": 0.03, "created_at": _stamp(30)}
    ]
    payload = backend.recall_context()
    assert payload is not None
    assert "- run pytest with -k (from this episode)" in payload["content"]
    assert payload["memories"][0]["id"] == "a1"


def test_l3_persona_prepended_and_survives_full_l1_page(make_backend, fake_client):
    backend = make_backend(max_memories=3)
    backend.start()
    backend.set_task("fix the bug")
    fake_client.persona = {"content": "Prefers minimal diffs.\nUses conda.", "created_at": _stamp(-60)}
    fake_client.search_hits = [
        {"id": f"a{i}", "content": f"fact {i}", "score": 0.01, "created_at": _stamp(-60)} for i in range(5)
    ]
    payload = backend.recall_context()
    lines = [line for line in payload["content"].splitlines() if line.startswith("- ")]
    # The persona pseudo-hit is FIRST (prepended) and the slice keeps it even
    # though L1 alone fills max_memories (list-order slice, no score sort).
    assert lines[0].startswith("- (user profile) Prefers minimal diffs. Uses conda.")
    assert payload["memories"][0]["id"] == "persona"


def test_persona_navigation_tail_stripped_at_exact_marker(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.persona = {"content": f"Profile body.\n\n{_NAV_MARKER}\n- [scene](x)"}
    fake_client.search_hits = [{"id": "a1", "content": "fact", "score": 0.01, "created_at": _stamp(-60)}]
    payload = backend.recall_context()
    assert "Profile body" in payload["content"]
    assert "Scene Navigation" not in payload["content"]
    assert "scene](x)" not in payload["content"]


def test_oversized_persona_renders_unbounded_and_l1_keeps_the_full_budget(make_backend, fake_client):
    """The persona is the arm's one budget-EXEMPT layer (native parity:
    applyRecallBudget governs L1 memory lines only): a profile far beyond
    max_total_recall_chars renders in full — no truncation — and the L1 lines
    still receive the WHOLE budget. The old quarter-share cap's crowd-out
    rationale is reversed: outside the budget the persona can no longer
    displace a single L1 line."""
    backend = make_backend(max_total_recall_chars=2000)
    backend.start()
    backend.set_task("fix the bug")
    persona_body = "deep insight. " * 200  # ~2800 chars collapsed — beyond the whole budget
    fake_client.persona = {"content": persona_body, "created_at": _stamp(-60)}
    fake_client.search_hits = [
        {"id": f"a{i}", "content": f"fact {i} " + "x" * 600, "score": 0.01, "created_at": _stamp(-60)} for i in range(3)
    ]
    payload = backend.recall_context()
    lines = [line for line in payload["content"].splitlines() if line.startswith("- ")]
    persona_lines = [line for line in lines if line.startswith("- (user profile)")]
    assert len(persona_lines) == 1
    # Full length, whitespace-collapsed only — the truncation suffix never
    # touches the persona line.
    assert persona_lines[0] == f"- (user profile) {persona_body.strip()} (from an earlier episode)"
    assert "[truncated]" not in persona_lines[0]
    # The L1 facts rank below the persona and still receive the FULL budget:
    # all three render whole (3 x ~635 chars — had the persona consumed any
    # budget, lines this size could not all have followed it).
    l1_lines = [line for line in lines if "fact " in line]
    assert len(l1_lines) == 3
    assert all("[truncated]" not in line for line in l1_lines)
    assert payload["n_memories"] == 4


def test_persona_missing_renders_nothing(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.persona = {"content": None}
    fake_client.search_hits = [{"id": "a1", "content": "fact", "score": 0.01, "created_at": _stamp(-60)}]
    payload = backend.recall_context()
    assert not any(line.startswith("- (user profile)") for line in payload["content"].splitlines())


def test_add_uses_the_dedicated_add_timeout(make_backend, fake_client):
    # With the vector lane on, the gateway embeds every L0 message
    # sequentially inside the add — the call must not die client-side while
    # the gateway is still processing it.
    backend = make_backend(add_timeout=123.0)
    backend.start()
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.maybe_extract(10)
    assert fake_client.add_calls[0]["timeout"] == 123.0


def test_search_and_watermark_carry_task_id_repo_key(make_backend, fake_client):
    backend = make_backend(instance_id="pydata__xarray-2905")
    backend.start()
    backend.set_task("fix the bug")
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.maybe_extract(10)
    assert fake_client.add_calls[0]["task_id"] == "pydata__xarray"
    backend.recall_context()
    assert fake_client.search_calls[0]["task_id"] == "pydata__xarray"
    assert fake_client.query_calls[0]["task_id"] == "pydata__xarray"
    # The L1 fetch is a small overfetch under the wire cap (the base's
    # floor/slice work below the fetch).
    assert fake_client.search_calls[0]["limit"] == backend.config.max_memories + 5
    # The search carries the configured search_timeout (the arm overlay pins
    # 30 s for the query embedding under a slow vector lane — never the
    # drain budget).
    assert fake_client.search_calls[0]["timeout"] == backend.config.search_timeout


def test_search_failure_never_raises_into_loop(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.search_error = RuntimeError("gateway down")
    assert backend.recall_context() is None
    assert backend._counts["search_errors"] == 1
    assert backend._counts["backend_errors"] == 1


def test_aux_layer_failure_degrades_not_fails_search(make_backend, fake_client, monkeypatch):
    # A transient blip on core_read (L3) or scenario_ls (L2) must not take
    # the L1 recall down with it: the search still renders its L1 payload,
    # counts no search error, and the dropped layer refreshes next cycle.
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.search_hits = [{"id": "a1", "content": "fact", "score": 0.01, "created_at": _stamp(-60)}]
    fake_client.persona = {"content": "Profile."}

    def broken(**kwargs):
        raise RuntimeError("aux layer down")

    monkeypatch.setattr(fake_client, "core_read", broken)
    monkeypatch.setattr(fake_client, "scenario_ls", broken)
    payload = backend.recall_context()
    assert payload is not None
    assert "- fact" in payload["content"]
    assert "(user profile)" not in payload["content"]
    assert "Scenario Files" not in payload["content"]
    assert backend._counts["search_errors"] == 0

    # Next cycle: the aux layers recover and come back (a counted extract
    # tick invalidates the search cache, so the next recall re-searches).
    monkeypatch.setattr(fake_client, "core_read", lambda **kwargs: dict(fake_client.persona))
    monkeypatch.setattr(fake_client, "scenario_ls", lambda **kwargs: [dict(entry) for entry in fake_client.scene_entries])
    fake_client.scene_entries = [{"path": "scenes/a.md", "summary": "s"}]
    backend.record([{"role": "user", "content": "m2"}], step=2)
    backend.maybe_extract(20)
    payload = backend.recall_context()
    assert "- (user profile) Profile." in payload["content"]
    assert "scenes/a.md" in payload["content"]


def test_startup_count_is_repo_scoped(make_backend, fake_client):
    # Decision 5: every arm-side data-plane call carries the episode's repo
    # key — the run-start store_count diagnostic included.
    backend = make_backend(instance_id="pydata__xarray-2905")
    backend.start()
    assert fake_client.count_calls == 1
    assert fake_client.count_task_ids == ["pydata__xarray"]


def test_search_io_seconds_accrue_to_exemption(make_backend, fake_client, monkeypatch):
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.search_hits = [{"id": "a1", "content": "fact", "score": 0.01, "created_at": _stamp(-60)}]
    # The base times _search() itself; one mocked monotonic tick per call
    # makes the accrual assertable (a bare >= 0.0 pins nothing).
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "shared_bridge.backend.time.monotonic", lambda: clock.__setitem__("t", clock["t"] + 1.0) or clock["t"]
    )
    backend.recall_context()
    assert backend.consume_annotation_duration() >= 1.0


def test_timed_accrues_and_drains(make_backend, fake_client, monkeypatch):
    # The subclass-owned accrual mechanism: native-call seconds written
    # directly onto the base's plain _io_duration attribute and drained
    # through consume_annotation_duration.
    backend = make_backend()
    backend.start()
    clock = {"t": 0.0}
    monkeypatch.setattr("tencentdb_bridge.backend.time.monotonic", lambda: clock.__setitem__("t", clock["t"] + 1.0) or clock["t"])
    with backend._timed():
        pass
    assert backend._io_duration == 1.0
    assert backend.consume_annotation_duration() == 1.0
    assert backend._io_duration == 0.0


def test_recall_payload_is_persona_then_l1(make_backend, fake_client):
    # The whole injected hit list: the L3 pseudo-hit prepended to the L1
    # page, in list order — no interleave, no other hit layers.
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.persona = {"content": "Prefers minimal diffs.", "created_at": _stamp(-60)}
    fake_client.search_hits = [
        {"id": "a1", "content": "fact one", "score": 0.03, "created_at": _stamp(-60)},
        {"id": "a2", "content": "fact two", "score": 0.02, "created_at": _stamp(-60)},
    ]
    payload = backend.recall_context()
    assert [hit["id"] for hit in payload["memories"]] == ["persona", "a1", "a2"]


# ---------------------------------------------------------------------------
# L2 index section
# ---------------------------------------------------------------------------
def test_scene_index_render_unbounded_and_undefined_summary(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.scene_entries = [
        {"path": "scenes/dir/", "version": 0},  # directory: skipped
        {"path": "scenes/a.md", "summary": "a" * 250},  # no summary cap: renders whole
        {"path": "scenes/b.md"},  # summary absent upstream
        {"path": "scenes/c.md", "summary": "reached"},  # no count cap
    ]
    fake_client.search_hits = [{"id": "a1", "content": "fact", "score": 0.01, "created_at": _stamp(-60)}]
    payload = backend.recall_context()
    content = payload["content"]
    assert f"- scenes/a.md — {'a' * 250}" in content
    assert "- scenes/b.md" in content
    assert "- scenes/c.md — reached" in content
    assert "scenes/dir/" not in content
    # The curl guide bakes in the mandatory headers and the isolation ids.
    assert "-X POST http://host.docker.internal:8420/v3/scenario/read" in content
    assert "'Authorization: Bearer local'" in content
    assert "'x-tdai-service-id: default'" in content
    assert '"team_id":"minisweagent"' in content
    assert f'"user_id":"{backend.effective_user_id()}"' in content


def test_no_scenes_no_index_section(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.search_hits = [{"id": "a1", "content": "fact", "score": 0.01, "created_at": _stamp(-60)}]
    payload = backend.recall_context()
    assert "Scenario Files" not in payload["content"]


def test_with_no_hits_nothing_injected_at_all(make_backend, fake_client):
    # Base-rule consequence: zero hits + no persona -> the base injects
    # nothing, so the L2 index reaches the model only once some hit exists.
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.scene_entries = [{"path": "scenes/a.md", "summary": "s"}]
    assert backend.recall_context() is None


def test_conversation_search_guide_rendered_unconditionally(make_backend, fake_client):
    # No scenes: the scene section is absent, but the conversation-search
    # guide still rides the header — like native's always-registered tool it
    # is available whenever the header reaches the model.
    backend = make_backend(instance_id="pydata__xarray-2905", conversation_search_limit=7)
    backend.start()
    backend.set_task("fix the bug")
    fake_client.search_hits = [{"id": "a1", "content": "fact", "score": 0.01, "created_at": _stamp(-60)}]
    payload = backend.recall_context()
    content = payload["content"]
    assert "Scenario Files" not in content  # the scene section stays scene-gated
    assert "-X POST http://host.docker.internal:8420/v3/conversation/search" in content
    assert "'Authorization: Bearer local'" in content
    assert "'x-tdai-service-id: default'" in content
    assert '"team_id":"minisweagent"' in content
    assert f'"user_id":"{backend.effective_user_id()}"' in content
    assert '"task_id":"pydata__xarray"' in content  # the repo tier is baked in
    assert '"limit":7' in content  # the configured conversation_search_limit renders
    assert "tee -a /tmp/tdai-l0-searches.md" in content


def test_conversation_search_guide_follows_the_scene_section(make_backend, fake_client):
    # With scenes present the guide renders after the L2 scene section and
    # before the recalled-memories section.
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    fake_client.scene_entries = [{"path": "scenes/a.md", "summary": "s"}]
    fake_client.search_hits = [{"id": "a1", "content": "fact", "score": 0.01, "created_at": _stamp(-60)}]
    payload = backend.recall_context()
    content = payload["content"]
    assert content.index("## Scenario Files (L2 index)") < content.index("## Conversation Search")
    assert content.index("## Conversation Search") < content.index("## Recalled Memories")


# ---------------------------------------------------------------------------
# L2 scene-navigation tail: parsing, heat ordering, render, lifecycle
# ---------------------------------------------------------------------------
def _nav_tail(*blocks: tuple) -> str:
    """Build a native-shape scene-navigation tail (scene-navigation.ts):
    marker, lead-in, blank-line-joined blocks, footer. Each block is
    (ls_path, heat, updated_or_None, summary). The nav path is prefixed with
    scene_blocks/ — the join key is the nav path minus that prefix matched
    against the ls path VERBATIM, so a fixture whose nav path forgets the
    prefix (or uses a different ls spelling) silently fails to join and every
    absence-of-heat assertion passes while pinning nothing."""
    parts = []
    for ls_path, heat, updated, summary in blocks:
        heat_line = f"**热度**: {heat}"
        if updated is not None:
            heat_line += f" | **更新**: {updated}"
        parts.append(f"### Path: scene_blocks/{ls_path}\n{heat_line}\nSummary: {summary}")
    return (
        f"{_NAV_MARKER}\n"
        "*以下是当前场景记忆的索引，可根据需要 read 读取详细内容。*\n\n"
        + "\n\n".join(parts)
        + "\n\n📌 使用说明：\n"
        "- Path 是 scene block 的绝对路径，可直接使用 **read** 工具读取完整内容（参数: filePath）\n"
        "- 热度：该场景被记忆命中的累计次数，越高越重要\n"
        "- Summary：场景的核心要点摘要"
    )


def _scene_lines(content: str) -> list[str]:
    return [line for line in content.splitlines() if line.startswith("- scenes/")]


def _prime_recall(backend, fake_client):
    backend.set_task("fix the bug")
    fake_client.search_hits = [{"id": "a1", "content": "fact", "score": 0.01, "created_at": _stamp(-60)}]


def test_parse_scene_nav_realistic_tail():
    tail = (
        f"{_NAV_MARKER}\n"
        "*以下是当前场景记忆的索引，可根据需要 read 读取详细内容。*\n\n"
        "### Path: scene_blocks/scenes/a.md\n"
        "**热度**: 3 🔥 | **更新**: 2026-08-24T09:58:00.000Z\n"
        "Summary: Debugging flow notes\n\n"
        "### Path: scene_blocks/scenes/b.md\n"
        "**热度**: 7 | **更新**: 2026-08-24T10:11:12.000Z\n"
        "Summary: Repo layout\n\n"
        "### Path: scene_blocks/scenes/c.md\n"
        "**热度**: 1\n"
        "Summary: API notes\n\n"
        "📌 使用说明：\n"
        "- Path 是 scene block 的绝对路径，可直接使用 **read** 工具读取完整内容（参数: filePath）\n"
        "- 热度：该场景被记忆命中的累计次数，越高越重要\n"
        "- Summary：场景的核心要点摘要"
    )
    # The emoji run trails the digits; the footer (fullwidth-colon 热度：/Summary：
    # prose plus a `- Path 是 …` bullet) contributes no entries.
    assert _parse_scene_nav(tail) == {
        "scenes/a.md": {"heat": 3, "updated": "2026-08-24T09:58:00.000Z"},
        "scenes/b.md": {"heat": 7, "updated": "2026-08-24T10:11:12.000Z"},
        "scenes/c.md": {"heat": 1, "updated": None},
    }


def test_parse_scene_nav_skips_block_without_heat_line():
    tail = (
        "### Path: scene_blocks/x.md\n"
        "Summary: no heat line in this block\n\n"
        "### Path: scene_blocks/y.md\n"
        "**热度**: 2\n"
        "Summary: ok"
    )
    assert _parse_scene_nav(tail) == {"y.md": {"heat": 2, "updated": None}}


def test_parse_scene_nav_garbage_tail_is_empty():
    assert _parse_scene_nav("random prose\n- [scene](x)\nnothing parseable") == {}
    assert _parse_scene_nav("") == {}


def test_parse_scene_nav_summary_cannot_seed_updated():
    # The 更新 clause is searched on the block's own heat line only: a Summary
    # line containing the literal marker never seeds a stamp.
    tail = "### Path: scene_blocks/x.md\n**热度**: 5\nSummary: see **更新**: not-a-stamp inline"
    assert _parse_scene_nav(tail) == {"x.md": {"heat": 5, "updated": None}}


def test_parse_scene_nav_never_raises_on_junk():
    junks = [
        "### Path: ",
        "### Path: scene_blocks/\n**热度**: ",
        "**热度**: 12\n**更新**: 2026",
        "### Path: scene_blocks/x.md\n**热度**: 99999999999999999999",
        "### Path: scene_blocks/x.md\n**热度**:1\n### Path: scene_blocks/y.md\n**热度**: 2 | **更新**: ",
        "\x00### Path: scene_blocks/\x00.md\n**热度**: 1",
    ]
    for junk in junks:
        _parse_scene_nav(junk)  # persona text is model-generated: never raise


def test_scene_index_orders_heat_desc_with_trailing_navless(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    _prime_recall(backend, fake_client)
    # ls order is deliberately NOT heat-desc.
    fake_client.scene_entries = [
        {"path": "scenes/low.md", "summary": "low heat"},
        {"path": "scenes/high.md", "summary": "high heat"},
        {"path": "scenes/mid.md", "summary": "mid heat"},
        {"path": "scenes/none.md", "summary": "no nav entry"},
    ]
    fake_client.persona = {
        "content": "Profile.\n\n"
        + _nav_tail(
            ("scenes/low.md", 1, None, "low"),
            ("scenes/high.md", 9, "2026-08-24T10:00:00.000Z", "high"),
            ("scenes/mid.md", 5, "2026-08-24T09:00:00.000Z", "mid"),
        )
    }
    payload = backend.recall_context()
    # POSITIVE heat pins: a silent join failure renders nav-less lines and
    # fails these. The stamp-less entry drops just that clause; the nav-less
    # entry trails in ls order with the plain line shape.
    assert _scene_lines(payload["content"]) == [
        "- scenes/high.md — heat 9, updated 2026-08-24T10:00:00.000Z — high heat",
        "- scenes/mid.md — heat 5, updated 2026-08-24T09:00:00.000Z — mid heat",
        "- scenes/low.md — heat 1 — low heat",
        "- scenes/none.md — no nav entry",
    ]


def test_scene_index_nav_entry_absent_from_ls_never_renders(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    _prime_recall(backend, fake_client)
    fake_client.scene_entries = [{"path": "scenes/a.md", "summary": "s"}]
    fake_client.persona = {
        "content": "Profile.\n\n"
        + _nav_tail(
            ("scenes/a.md", 1, None, "a"),
            ("scenes/ghost.md", 99, None, "merged or deleted between nav regeneration and the ls call"),
        )
    }
    payload = backend.recall_context()
    # ls is the existence truth: the nav-only ghost never renders.
    assert _scene_lines(payload["content"]) == ["- scenes/a.md — heat 1 — s"]
    assert "ghost.md" not in payload["content"]


def test_scene_index_heat_ties_keep_ls_order(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    _prime_recall(backend, fake_client)
    fake_client.scene_entries = [
        {"path": "scenes/b.md", "summary": "b"},
        {"path": "scenes/a.md", "summary": "a"},
        {"path": "scenes/c.md", "summary": "c"},
    ]
    fake_client.persona = {
        "content": "Profile.\n\n"
        + _nav_tail(
            ("scenes/a.md", 3, None, "a"),
            ("scenes/c.md", 1, None, "c"),
            ("scenes/b.md", 3, None, "b"),
        )
    }
    payload = backend.recall_context()
    # The 3-heat tie renders in ls order (b before a); c trails.
    assert _scene_lines(payload["content"]) == [
        "- scenes/b.md — heat 3 — b",
        "- scenes/a.md — heat 3 — a",
        "- scenes/c.md — heat 1 — c",
    ]


def test_scene_index_no_count_cap_renders_every_file(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    _prime_recall(backend, fake_client)
    fake_client.scene_entries = [{"path": f"scenes/s{i:02d}.md", "summary": f"summary {i}"} for i in range(25)]
    fake_client.persona = {
        "content": "Profile.\n\n" + _nav_tail(*[(f"scenes/s{i:02d}.md", i + 1, None, f"s {i}") for i in range(25)])
    }
    payload = backend.recall_context()
    lines = _scene_lines(payload["content"])
    # No count cap: all 25 render, ordered heat-desc (ls order is heat-ASC).
    assert len(lines) == 25
    assert [line.split(" — ")[0] for line in lines] == [f"- scenes/s{i:02d}.md" for i in reversed(range(25))]
    assert lines[0] == "- scenes/s24.md — heat 25 — summary 24"
    assert lines[-1] == "- scenes/s00.md — heat 1 — summary 0"


def test_scene_index_heat_decoration_drops_clauses_cleanly(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    _prime_recall(backend, fake_client)
    fake_client.scene_entries = [
        {"path": "scenes/stamped.md", "summary": "has both"},
        {"path": "scenes/unstamped.md", "summary": "no stamp"},
        {"path": "scenes/bare.md"},  # no ls summary
    ]
    fake_client.persona = {
        "content": "Profile.\n\n"
        + _nav_tail(
            ("scenes/stamped.md", 3, "2026-08-24T10:11:12.000Z", "x"),
            ("scenes/unstamped.md", 2, None, "x"),
            ("scenes/bare.md", 1, None, "x"),
        )
    }
    payload = backend.recall_context()
    lines = _scene_lines(payload["content"])
    assert lines[0] == "- scenes/stamped.md — heat 3, updated 2026-08-24T10:11:12.000Z — has both"
    # No 更新 stamp: just that clause drops — never a dangling separator.
    assert lines[1] == "- scenes/unstamped.md — heat 2 — no stamp"
    assert "updated" not in lines[1]
    # No ls summary (a nav entry exists iff the index entry does, so heat can
    # exist with no summary): the summary clause drops the same way.
    assert lines[2] == "- scenes/bare.md — heat 1"


def test_scene_index_summary_whitespace_collapsed_full_length(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    _prime_recall(backend, fake_client)
    summary = ("alpha   beta\n gamma \t delta " * 12).strip()
    collapsed = " ".join(summary.split())
    assert len(collapsed) > 200  # beyond the removed summary cap
    fake_client.scene_entries = [{"path": "scenes/long.md", "summary": summary}]
    payload = backend.recall_context()
    # One-line shape normalization at FULL length — the cap is gone.
    assert f"- scenes/long.md — {collapsed}" in payload["content"]


def test_scene_index_without_nav_tail_renders_ls_order(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    _prime_recall(backend, fake_client)
    fake_client.scene_entries = [
        {"path": "scenes/b.md", "summary": "b"},
        {"path": "scenes/a.md", "summary": "a"},
    ]
    # Not-yet-generated persona answers 200 with null content: empty stash.
    fake_client.persona = {"content": None}
    payload = backend.recall_context()
    lines = _scene_lines(payload["content"])
    assert lines == ["- scenes/b.md — b", "- scenes/a.md — a"]
    assert not any(" — heat " in line for line in lines)
    # A persona body WITHOUT the nav marker means an empty stash too.
    fake_client.persona = {"content": "Profile only, no tail."}
    backend.record([{"role": "user", "content": "m"}], step=2)
    backend.maybe_extract(20)  # a counted tick invalidates the search cache
    payload = backend.recall_context()
    lines = _scene_lines(payload["content"])
    assert lines == ["- scenes/b.md — b", "- scenes/a.md — a"]
    assert not any(" — heat " in line for line in lines)


def test_scene_index_failed_persona_read_keeps_last_known_ordering(make_backend, fake_client, monkeypatch):
    backend = make_backend()
    backend.start()
    _prime_recall(backend, fake_client)
    fake_client.scene_entries = [
        {"path": "scenes/low.md", "summary": "low"},
        {"path": "scenes/high.md", "summary": "high"},
    ]
    fake_client.persona = {
        "content": "Profile.\n\n" + _nav_tail(("scenes/low.md", 1, None, "l"), ("scenes/high.md", 8, None, "h"))
    }
    payload = backend.recall_context()
    ordered = ["- scenes/high.md — heat 8 — high", "- scenes/low.md — heat 1 — low"]
    assert _scene_lines(payload["content"]) == ordered

    def broken(**kwargs):
        raise RuntimeError("core_read down")

    monkeypatch.setattr(fake_client, "core_read", broken)
    backend.record([{"role": "user", "content": "m"}], step=2)
    backend.maybe_extract(20)  # invalidate the search cache -> a fresh cycle
    payload = backend.recall_context()
    # The failed read drops the persona line for the cycle but keeps the
    # last-known ordering (derived metadata: stale heats misorder at worst,
    # and ls kept existence fresh on the same failed cycle).
    assert "(user profile)" not in payload["content"]
    assert _scene_lines(payload["content"]) == ordered


def test_scene_index_nav_stash_cleared_on_new_episode(make_backend, fake_client, monkeypatch):
    backend = make_backend()
    backend.start()
    _prime_recall(backend, fake_client)
    fake_client.scene_entries = [{"path": "scenes/a.md", "summary": "a"}]
    fake_client.persona = {"content": "Profile.\n\n" + _nav_tail(("scenes/a.md", 4, None, "a"))}
    payload = backend.recall_context()
    assert _scene_lines(payload["content"]) == ["- scenes/a.md — heat 4 — a"]
    assert backend._nav_scenes

    # A new episode (start resets the extras) clears the stash: a failed
    # first read then renders heat-less in ls order instead of resurrecting
    # the previous episode's ordering.
    backend.start()
    assert backend._nav_scenes == {}

    def broken(**kwargs):
        raise RuntimeError("core_read down")

    monkeypatch.setattr(fake_client, "core_read", broken)
    backend.set_task("fix the bug")
    payload = backend.recall_context()
    assert _scene_lines(payload["content"]) == ["- scenes/a.md — a"]


def test_memory_json_settings_carry_no_scene_index_knobs(make_backend, fake_client, no_sleep):
    backend = make_backend()
    backend.start()
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.finalize()
    artifact = json.loads(Path(backend.config.output_dir, "memory.json").read_text())
    assert "scene_summary_chars" not in artifact["settings"]
    assert "scene_index_limit" not in artifact["settings"]


# ---------------------------------------------------------------------------
# Agent scene-read observation
# ---------------------------------------------------------------------------
def _read_action(command, call_id="call_0"):
    return {"role": "assistant", "content": "", "extra": {"actions": [{"command": command, "tool_call_id": call_id}]}}


def _observation(call_id="call_0", content="scene body"):
    return {"role": "tool", "content": content, "tool_call_id": call_id}


def test_scene_read_armed_and_closed_on_matching_id(make_backend, fake_client, scene_read_command):
    backend = make_backend()
    backend.start()
    backend.record([_read_action(scene_read_command)], step=1)
    assert backend._counts["agent_scene_reads"] == 0  # armed, not yet observed
    backend.record([_observation()], step=1)
    assert backend._counts["agent_scene_reads"] == 1
    assert backend._counts["scene_read_chars"] == len("scene body")
    events = [event for event in backend._events if event["kind"] == "scene_read"]
    assert events and events[0]["path"] == "scenes/debugging.md"


def test_marker_mention_without_parsed_path_never_arms(make_backend, fake_client):
    """A command mentioning the scene-read route but carrying no parseable
    "path" pair (e.g. grep over the injected guide text) is not a scene
    read: no slot arms, so its observation counts nothing."""
    backend = make_backend()
    backend.start()
    grep = "grep -n 'v3/scenario/read' README.md"
    backend.record([_read_action(grep)], step=1)
    backend.record([_observation()], step=1)
    assert backend._counts["agent_scene_reads"] == 0
    assert backend._counts["scene_read_chars"] == 0


def test_sibling_observation_does_not_close(make_backend, fake_client, scene_read_command):
    backend = make_backend()
    backend.start()
    backend.record([_read_action(scene_read_command, "call_0")], step=1)
    # Multi-action step: the sibling action's observation must not close.
    backend.record([_observation("call_1", "other output")], step=1)
    assert backend._counts["agent_scene_reads"] == 0
    backend.record([_observation("call_0", "scene body")], step=1)
    assert backend._counts["agent_scene_reads"] == 1


def test_multi_action_turn_arms_every_scene_read(make_backend, fake_client, scene_read_command):
    """One assistant turn issuing two scene reads arms both; each closes on
    its own observation."""
    backend = make_backend()
    backend.start()
    other_read = scene_read_command.replace("debugging.md", "testing.md")
    backend.record(
        [
            {
                "role": "assistant",
                "content": "",
                "extra": {
                    "actions": [
                        {"command": scene_read_command, "tool_call_id": "call_0"},
                        {"command": other_read, "tool_call_id": "call_1"},
                    ]
                },
            }
        ],
        step=1,
    )
    backend.record([_observation("call_0", "scene A"), _observation("call_1", "scene BB")], step=1)
    assert backend._counts["agent_scene_reads"] == 2
    assert backend._counts["scene_read_chars"] == len("scene A") + len("scene BB")
    paths = [event["path"] for event in backend._events if event["kind"] == "scene_read"]
    assert paths == ["scenes/debugging.md", "scenes/testing.md"]


def test_later_assistant_message_clears_pending_without_event(make_backend, fake_client, scene_read_command):
    backend = make_backend()
    backend.start()
    backend.record([_read_action(scene_read_command)], step=1)
    backend.record([{"role": "assistant", "content": "moved on", "extra": {"actions": [{"command": "ls", "tool_call_id": "call_9"}]}}], step=2)
    backend.record([_observation()], step=2)  # late tool message: no pending read
    assert backend._counts["agent_scene_reads"] == 0


def test_action_without_tool_call_id_never_arms(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    backend.record(
        [{"role": "assistant", "content": "", "extra": {"actions": [{"command": "curl /v3/scenario/read"}]}}],
        step=1,
    )
    backend.record([_observation(None)], step=1)
    assert backend._counts["agent_scene_reads"] == 0


def test_scene_read_escaped_quote_path_arms(make_backend, fake_client):
    # A model re-typing the guide's curl with shell-escaped quotes issues the
    # same read: the arming regex must recognize \"path\":\"...\" too, or the
    # real read stays uncounted.
    backend = make_backend()
    backend.start()
    command = (
        "curl -sS -X POST http://host.docker.internal:8420/v3/scenario/read "
        '-H "Content-Type: application/json" '
        '-d "{\\"team_id\\":\\"minisweagent\\",\\"path\\":\\"scenes/escaped.md\\"}"'
    )
    backend.record([_read_action(command)], step=1)
    backend.record([_observation()], step=1)
    assert backend._counts["agent_scene_reads"] == 1
    events = [event for event in backend._events if event["kind"] == "scene_read"]
    assert events and events[0]["path"] == "scenes/escaped.md"


def test_scene_read_observer_failure_never_breaks_record(make_backend, fake_client, monkeypatch):
    # Read-detection is measurement-only: an observer failure is contained
    # and recording proceeds (house rule 3 — nothing raises into the loop).
    from tencentdb_bridge.backend import TencentDBBackend

    def _boom(self, message, step):
        raise RuntimeError("observer boom")

    monkeypatch.setattr(TencentDBBackend, "_observe_agent_read", _boom)
    backend = make_backend()
    backend.start()
    backend.record([{"role": "assistant", "content": "thinking"}], step=1)
    backend.record([{"role": "user", "content": "obs"}], step=1)
    assert [message["role"] for message in backend._pending] == ["assistant", "user"]


def test_scene_read_counters_reach_memory_json(make_backend, fake_client, no_sleep, scene_read_command):
    backend = make_backend()
    backend.start()
    backend.record([_read_action(scene_read_command)], step=1)
    backend.record([_observation()], step=1)
    backend.finalize()
    artifact = json.loads(Path(backend.config.output_dir, "memory.json").read_text())
    assert artifact["counts"]["agent_scene_reads"] == 1
    assert artifact["counts"]["scene_read_chars"] == len("scene body")


# ---------------------------------------------------------------------------
# Agent conversation-search observation
# ---------------------------------------------------------------------------
def test_conversation_search_armed_and_closed_on_matching_id(make_backend, fake_client, convo_search_command):
    backend = make_backend()
    backend.start()
    backend.record([_read_action(convo_search_command)], step=1)
    assert backend._counts["agent_conversation_searches"] == 0  # armed, not yet observed
    backend.record([_observation("call_0", "search results")], step=1)
    assert backend._counts["agent_conversation_searches"] == 1
    assert backend._counts["conversation_search_chars"] == len("search results")
    events = [event for event in backend._events if event["kind"] == "conversation_search"]
    assert events and events[0]["query"] == "exact failing command"


def test_conversation_search_marker_without_query_never_arms(make_backend, fake_client):
    """A command mentioning the conversation-search route but carrying no
    parseable "query" pair (e.g. grep over the injected guide text) is not a
    conversation search: no slot arms, so its observation counts nothing."""
    backend = make_backend()
    backend.start()
    grep = "grep -n 'v3/conversation/search' README.md"
    backend.record([_read_action(grep)], step=1)
    backend.record([_observation()], step=1)
    assert backend._counts["agent_conversation_searches"] == 0
    assert backend._counts["conversation_search_chars"] == 0


def test_conversation_search_sibling_observation_does_not_close(make_backend, fake_client, convo_search_command):
    backend = make_backend()
    backend.start()
    backend.record([_read_action(convo_search_command, "call_0")], step=1)
    # Multi-action step: the sibling action's observation must not close.
    backend.record([_observation("call_1", "other output")], step=1)
    assert backend._counts["agent_conversation_searches"] == 0
    backend.record([_observation("call_0", "search results")], step=1)
    assert backend._counts["agent_conversation_searches"] == 1


def test_multi_action_turn_arms_both_read_kinds(make_backend, fake_client, scene_read_command, convo_search_command):
    """One assistant turn issuing a scene read AND a conversation search arms
    both pending maps (separate dicts, both keyed by tool_call_id); each
    closes on its own observation."""
    backend = make_backend()
    backend.start()
    backend.record(
        [
            {
                "role": "assistant",
                "content": "",
                "extra": {
                    "actions": [
                        {"command": scene_read_command, "tool_call_id": "call_0"},
                        {"command": convo_search_command, "tool_call_id": "call_1"},
                    ]
                },
            }
        ],
        step=1,
    )
    backend.record([_observation("call_0", "scene A"), _observation("call_1", "search BB")], step=1)
    assert backend._counts["agent_scene_reads"] == 1
    assert backend._counts["scene_read_chars"] == len("scene A")
    assert backend._counts["agent_conversation_searches"] == 1
    assert backend._counts["conversation_search_chars"] == len("search BB")
    events = [event for event in backend._events if event["kind"] == "conversation_search"]
    assert events and events[0]["query"] == "exact failing command"


def test_chained_command_arms_both_kinds_under_one_id(make_backend, fake_client, scene_read_command, convo_search_command):
    """A single command containing both markers (a chained curl) arms both
    slots under one tool_call_id; the one observation closes both — its chars
    land in BOTH counters (accepted: mini-swe-agent gives one observation per
    action, so the two cannot be separated)."""
    backend = make_backend()
    backend.start()
    backend.record([_read_action(scene_read_command + "; " + convo_search_command)], step=1)
    backend.record([_observation("call_0", "combined output")], step=1)
    assert backend._counts["agent_scene_reads"] == 1
    assert backend._counts["agent_conversation_searches"] == 1
    assert backend._counts["scene_read_chars"] == len("combined output")
    assert backend._counts["conversation_search_chars"] == len("combined output")


def test_later_assistant_message_clears_pending_conversation_search(make_backend, fake_client, convo_search_command):
    backend = make_backend()
    backend.start()
    backend.record([_read_action(convo_search_command)], step=1)
    backend.record([{"role": "assistant", "content": "moved on", "extra": {"actions": [{"command": "ls", "tool_call_id": "call_9"}]}}], step=2)
    backend.record([_observation()], step=2)  # late tool message: no pending search
    assert backend._counts["agent_conversation_searches"] == 0


def test_conversation_search_escaped_quote_query_arms(make_backend, fake_client):
    # A model re-typing the guide's curl with shell-escaped quotes issues the
    # same search: the arming regex must recognize \"query\":\"...\" too, or
    # the real search stays uncounted.
    backend = make_backend()
    backend.start()
    command = (
        "curl -sS -X POST http://host.docker.internal:8420/v3/conversation/search "
        '-H "Content-Type: application/json" '
        '-d "{\\"team_id\\":\\"minisweagent\\",\\"query\\":\\"exact failing command\\"}"'
    )
    backend.record([_read_action(command)], step=1)
    backend.record([_observation()], step=1)
    assert backend._counts["agent_conversation_searches"] == 1
    events = [event for event in backend._events if event["kind"] == "conversation_search"]
    assert events and events[0]["query"] == "exact failing command"


def test_conversation_search_counters_reach_memory_json(make_backend, fake_client, no_sleep, convo_search_command):
    backend = make_backend()
    backend.start()
    backend.record([_read_action(convo_search_command)], step=1)
    backend.record([_observation("call_0", "search results")], step=1)
    backend.finalize()
    artifact = json.loads(Path(backend.config.output_dir, "memory.json").read_text())
    assert artifact["counts"]["agent_conversation_searches"] == 1
    assert artifact["counts"]["conversation_search_chars"] == len("search results")


# ---------------------------------------------------------------------------
# Origin attribution across episodes (the sidecar)
# ---------------------------------------------------------------------------
def test_prior_episode_hits_named_by_window(make_backend, fake_client, tmp_path):
    # A prior episode window from the sidecar: its hits name its session id.
    prior_session = "pydata__xarray-2905-" + "b" * 32
    sidecar = tmp_path / "tdai" / "episodes.jsonl"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"event": "start", "session_id": prior_session, "instance_id": "pydata__xarray-2905", "started_at": _stamp(-3600)})
        + "\n"
        + json.dumps({"event": "drain", "session_id": prior_session, "drained_at": _stamp(-1800)})
        + "\n"
    )
    backend = make_backend(instance_id="pydata__xarray-3095")
    backend.start()
    backend.set_task("fix the bug")
    fake_client.search_hits = [
        {"id": "a1", "content": "prior fact", "score": 0.01, "created_at": _stamp(-2400)}
    ]
    payload = backend.recall_context()
    assert "- prior fact (from earlier episode pydata__xarray-2905)" in payload["content"]


def test_before_first_window_hits_get_unknown_sentinel(make_backend, fake_client, tmp_path):
    prior_session = "pydata__xarray-2905-" + "b" * 32
    sidecar = tmp_path / "tdai" / "episodes.jsonl"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"event": "start", "session_id": prior_session, "instance_id": "pydata__xarray-2905", "started_at": _stamp(-3600)})
        + "\n"
        + json.dumps({"event": "drain", "session_id": prior_session, "drained_at": _stamp(-1800)})
        + "\n"
    )
    backend = make_backend()
    backend.start()
    backend.set_task("fix the bug")
    # A hit created before any window (or in a drain-to-start gap): the
    # sentinel keeps the suffix non-empty ("from an earlier episode").
    fake_client.search_hits = [{"id": "a1", "content": "ancient fact", "score": 0.01, "created_at": _stamp(-7200)}]
    payload = backend.recall_context()
    assert "- ancient fact (from an earlier episode)" in payload["content"]
    assert backend._hit_origin(fake_client.search_hits[0]) == _UNKNOWN_ORIGIN


def test_merged_hit_attributes_to_oldest_episode(make_backend, fake_client, tmp_path):
    # created_at = timestamp_start = min of the merged union -> the OLDEST
    # contributing episode (documented merge bias).
    older, newer = "repo__a-100-" + "b" * 32, "repo__a-200-" + "c" * 32
    sidecar = tmp_path / "tdai" / "episodes.jsonl"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in [
                {"event": "start", "session_id": older, "instance_id": "repo__a-100", "started_at": _stamp(-7200)},
                {"event": "drain", "session_id": older, "drained_at": _stamp(-7000)},
                {"event": "start", "session_id": newer, "instance_id": "repo__a-200", "started_at": _stamp(-3600)},
                {"event": "drain", "session_id": newer, "drained_at": _stamp(-1800)},
            ]
        )
    )
    backend = make_backend()
    backend.start()
    assert backend._origin_session(_stamp(-7100)) == older


def test_hit_without_created_at_gets_sentinel(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    assert backend._hit_origin({"id": "a1", "content": "x"}) == _UNKNOWN_ORIGIN


# ---------------------------------------------------------------------------
# Final dump + settings artifact
# ---------------------------------------------------------------------------
def test_final_dump_repo_scoped_with_origin(make_backend, fake_client, no_sleep):
    backend = make_backend(instance_id="pydata__xarray-2905")
    backend.start()
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.finalize()
    artifact = json.loads(Path(backend.config.output_dir, "memory.json").read_text())
    assert artifact["enabled"] is True
    assert artifact["available"] is True
    assert artifact["settings"]["prompt_mode"] == "code"
    assert artifact["settings"]["l1_idle_timeout"] == 30.0  # resolved from the fixture gateway yaml
    assert artifact["settings"]["l1_idle_timeout_source"] == "gateway-yaml"
    assert artifact["counts"]["extraction_errors"] == 0
    assert artifact["final_memories"]
    assert artifact["final_memories"][0]["origin"] == backend._session_id


def test_memory_json_holds_no_credentials(make_backend, fake_client, no_sleep):
    backend = make_backend(api_key="super-secret")
    backend.start()
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.finalize()
    text = Path(backend.config.output_dir, "memory.json").read_text()
    assert "super-secret" not in text


def test_finalize_is_idempotent(make_backend, fake_client, no_sleep):
    backend = make_backend()
    backend.start()
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.finalize()
    calls = fake_client.add_calls
    backend.finalize()
    assert fake_client.add_calls == calls


# ---------------------------------------------------------------------------
# Regression pins (host-stamped recorded_at, base-owned L1 truncation, dedup key, warnings)
# ---------------------------------------------------------------------------
def test_add_bodies_carry_the_host_stamped_recorded_at(make_backend, fake_client):
    """Every recorded message rides conversation/add with a host-clock
    recorded_at (the upstream schema's optional field, honored over the
    container's receive time): the raw-message timestamps the agent's
    conversation search renders then live in the host clock domain,
    consistent across episodes and immune to host-vs-container skew."""
    from tencentdb_bridge.backend import _parse_iso

    backend = make_backend()
    backend.start()
    backend.record([{"role": "user", "content": "m1"}], step=1)
    backend.maybe_extract(10)
    stamps = [message.get("recorded_at") for message in fake_client.add_calls[0]["messages"]]
    assert stamps and all(isinstance(stamp, str) and _parse_iso(stamp) for stamp in stamps)


def test_store_message_clamps_to_the_wire_content_cap(make_backend, fake_client):
    """A run raising max_message_chars above the wire's 8192-char content cap
    must not draw a gateway 400 on every add (the breaker class): the recorded
    content clamps at the cap — mechanical, like the client's query cap."""
    from tencentdb_bridge.client import MESSAGE_CONTENT_MAX_CHARS

    backend = make_backend(max_message_chars=9000)
    backend.start()
    backend.record([{"role": "user", "content": "z" * 8900}], step=1)
    backend.maybe_extract(10)
    content = fake_client.add_calls[0]["messages"][0]["content"]
    assert len(content) == MESSAGE_CONTENT_MAX_CHARS


def test_store_message_clamp_counts_utf16_units(make_backend, fake_client):
    """The wire cap counts UTF-16 code units (zod's String.length), not Python
    code points: astral-heavy text clamps by units — a code-point cut would
    still bust the cap and draw the breaker-class gateway 400 on every add."""
    from tencentdb_bridge.client import MESSAGE_CONTENT_MAX_CHARS, utf16_units

    backend = make_backend(max_message_chars=9000)
    backend.start()
    backend.record([{"role": "user", "content": "😀" * 8900}], step=1)  # 8900 code points, 17800 UTF-16 units
    backend.maybe_extract(10)
    content = fake_client.add_calls[0]["messages"][0]["content"]
    assert utf16_units(content) == MESSAGE_CONTENT_MAX_CHARS
    assert len(content) == MESSAGE_CONTENT_MAX_CHARS // 2  # every char is one surrogate pair


def test_window_start_floored_to_milliseconds():
    # The one pin on _Window's millisecond floor (the floor stays as one cheap
    # line; the boundary now serves L1 origin attribution alone — see the
    # class docstring).
    from datetime import datetime, timezone

    from tencentdb_bridge.backend import _Window

    window = _Window(datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc), "s")
    assert window.start.microsecond == 123000


def test_dedup_key_treats_absent_version_as_zero(make_backend, fake_client):
    """One fresh row sighted once WITHOUT a version and once WITH the
    store's version-0 default is the same fresh row (the _row_is_fresh
    class): the (id, version) dedup set must count it exactly once."""
    backend = make_backend()
    backend.start()
    fake_client.auto_produce = False
    backend.record([{"role": "user", "content": "m1"}], step=1)
    backend.maybe_extract(10)
    fake_client.rows.append(
        {"id": "a1", "type": "atomic", "content": "fact", "created_at": _stamp(), "updated_at": _stamp(),
         "task_id": "test-instance"}  # first sighting: no version key
    )
    backend.record([{"role": "user", "content": "m2"}], step=11)
    backend.maybe_extract(20)
    assert backend._counts["memories_added"] == 1
    fake_client.rows[0]["version"] = 0  # second sighting: the DDL default
    backend.record([{"role": "user", "content": "m3"}], step=21)
    backend.maybe_extract(30)
    assert backend._counts["memories_added"] == 1  # never counted twice


def test_l1_line_truncation_is_base_owned(make_backend, fake_client):
    """_render_line no longer pre-truncates (the local reserve caps are gone):
    the base's rank-then-fill owns line truncation — an over-budget
    top-ranked L1 fact DELIVERS truncated-to-fit at the 40-char floor with
    the shared marker, never silently skipped."""
    backend = make_backend(max_total_recall_chars=2000)
    backend.start()
    assert backend._render_line({"id": "a1", "content": "x" * 5000}) == "- " + "x" * 5000  # no local cap
    # End to end: the over-budget top-ranked fact delivers truncated.
    backend.set_task("fix the bug")
    fake_client.search_hits = [{"id": "a1", "content": "y" * 5000, "score": 0.5, "created_at": _stamp(-3600)}]
    payload = backend.recall_context()
    assert payload is not None
    (line,) = [ln for ln in payload["content"].splitlines() if ln.startswith("- y")]
    assert len(line) == 2000  # truncated to fit the whole budget (the first line)
    assert line.endswith(" ...[truncated]")


def test_memory_ref_marks_unavailable_content(make_backend, fake_client):
    # A hit with no usable gateway text stamps the unavailable marker (never
    # a coerced empty ref): the version id names the reason.
    backend = make_backend()
    backend.start()
    ref = backend._memory_ref({"id": "a1"})
    assert ref["content"] == {"availability": "unavailable", "reason": "no_gateway_text"}
    assert ref["version_id"] == "a1:unavailable"


def test_recall_min_score_set_draws_one_start_warning(make_backend, fake_client, caplog):
    """A recall_min_score floor compares L1 scores that are not one scale
    across retrieval strategies (the response carries no strategy field, so
    the host cannot even tell which scale it received); the integration's
    documented stance is the floor stays unset — a configured one draws one
    start-time warning (the guideline-override pattern), never a silent
    behavior."""
    backend = make_backend(recall_min_score=0.1)
    with caplog.at_level("WARNING", logger="tencentdb_bridge.backend"):
        backend.start()
    assert backend._available
    assert any("recall_min_score" in record.message for record in caplog.records)

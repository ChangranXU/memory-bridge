"""Backend lifecycle tests (offline, scripted platform client)."""

import json

import pytest

from mem0_bridge.client import Mem0ApiError

from shared_bridge.backend import _repo_of
from shared_bridge.prompts import EXTRACTION_GUIDELINES_DEFAULT, RECALL_POLICY_DEFAULT, extraction_episode_context


def _episode_context(instance_id: str) -> str:
    """The base-composed context exactly as the backend conveys it, so the
    pins below track the real renderer instead of a hand-copied string."""
    return extraction_episode_context(instance_id, _repo_of(instance_id))


def test_effective_user_id_by_scope(make_backend):
    run_scope = make_backend(scope="run", user_id="alice")
    instance_scope = make_backend(scope="instance", user_id="alice", instance_id="pydata__xarray-2905")
    assert run_scope.effective_user_id() == "alice"
    assert instance_scope.effective_user_id() == "alice:pydata__xarray-2905"


def test_start_missing_api_key_unavailable(make_backend, monkeypatch):
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    backend = make_backend(api_key="")
    backend.start()
    assert backend._available is False
    data = json.loads(read_memory_json(backend))
    assert data["available"] is False
    assert data["events"][0]["kind"] == "error"


# ---------------------------------------------------------------------------
# Mode dispatch + mode-conditional fail-closed validation
# ---------------------------------------------------------------------------
def _clear_mode_env(monkeypatch):
    for name in (
        "MEM0_API_KEY", "MEM0_BASE_URL", "MEM0_SERVER_URL", "MEM0_SERVER_API_KEY",
        "EMBEDDING_MODEL", "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_DIMENSIONS",
        "MODEL", "API_KEY", "BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _set_quartet(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_API_KEY", "emb-key")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://emb.invalid")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1536")


def test_server_mode_requires_server_url(make_backend, fake_client, monkeypatch):
    _clear_mode_env(monkeypatch)
    backend = make_backend(mode="server", server_url="")
    backend.start()
    assert backend._available is False
    assert "server_url" in json.loads(read_memory_json(backend))["events"][0]["error"]


def test_server_mode_requires_the_embedding_quartet(make_backend, fake_client, monkeypatch):
    """The OSS engine embeds on every add/search with no lexical fallback:
    a missing quartet would boot healthy and die on the first add — fail closed."""
    _clear_mode_env(monkeypatch)
    _set_quartet(monkeypatch)
    monkeypatch.delenv("EMBEDDING_DIMENSIONS")
    backend = make_backend(mode="server", server_url="http://127.0.0.1:8890")
    backend.start()
    assert backend._available is False
    assert "EMBEDDING_DIMENSIONS" in json.loads(read_memory_json(backend))["events"][0]["error"]


def test_server_mode_resolves(make_backend, fake_client, monkeypatch):
    _clear_mode_env(monkeypatch)
    _set_quartet(monkeypatch)
    backend = make_backend(mode="server", server_url="http://127.0.0.1:8890/")
    backend.start()
    assert backend._available is True
    assert backend.stats()["api_base_url"] == "http://127.0.0.1:8890"
    backend.finalize()
    data = json.loads(read_memory_json(backend))
    assert data["settings"]["mode"] == "server"


def test_library_mode_requires_run_root(make_backend, fake_client, monkeypatch):
    _clear_mode_env(monkeypatch)
    backend = make_backend(mode="library", run_root="")
    backend.start()
    assert backend._available is False
    assert "run_root" in json.loads(read_memory_json(backend))["events"][0]["error"]


def test_library_mode_requires_roster_llm_keys(make_backend, fake_client, monkeypatch, tmp_path):
    _clear_mode_env(monkeypatch)
    _set_quartet(monkeypatch)
    backend = make_backend(mode="library", run_root=str(tmp_path))
    backend.start()
    assert backend._available is False
    assert "MODEL/API_KEY/BASE_URL" in json.loads(read_memory_json(backend))["events"][0]["error"]


def test_library_mode_resolves_and_records_the_llm_upstream(make_backend, fake_client, monkeypatch, tmp_path):
    _clear_mode_env(monkeypatch)
    _set_quartet(monkeypatch)
    monkeypatch.setenv("MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("API_KEY", "roster-key")
    monkeypatch.setenv("BASE_URL", "https://api.deepseek.com")
    backend = make_backend(mode="library", run_root=str(tmp_path))
    backend.start()
    assert backend._available is True
    assert backend.stats()["api_base_url"] == "https://api.deepseek.com"
    backend.finalize()
    data = json.loads(read_memory_json(backend))
    assert data["settings"]["mode"] == "library"


def test_per_mode_identity_scheme(make_backend, fake_client, monkeypatch, tmp_path):
    _clear_mode_env(monkeypatch)
    _set_quartet(monkeypatch)
    monkeypatch.setenv("MODEL", "m")
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("BASE_URL", "https://b.invalid")
    assert make_backend()._identity_scheme == "mem0-platform-memory-v1"
    assert make_backend(mode="server", server_url="http://h")._identity_scheme == "mem0-server-memory-v1"
    assert make_backend(mode="library", run_root=str(tmp_path))._identity_scheme == "mem0-library-memory-v1"


def read_memory_json(backend):
    from pathlib import Path

    return (Path(backend.config.output_dir) / "memory.json").read_text()


def test_start_validates_key_with_ping(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    assert backend._available is True
    assert backend._session_id.startswith("test-instance-")
    assert fake_client.closed is False
    assert backend.stats()["available"] is True
    assert backend.stats()["user_id"] == "minisweagent"


def test_record_buffers_truncates_and_maps_roles(make_backend):
    backend = make_backend(max_message_chars=50)
    backend.start()
    backend.record(
        [
            {"role": "system", "content": "x" * 80},
            {"role": "tool", "content": "tool output"},
            {"role": "assistant", "content": None, "extra": {"actions": [{"command": "ls"}]}},
            {"role": "user", "content": "hi", "extra": {"transient_recall": True}},
            "not-a-dict",
            {"role": "user", "content": ""},
        ],
        step=3,
    )
    assert backend._pending == [
        {"role": "system", "content": "x" * 34 + "\n... [truncated]"},
        {"role": "user", "content": "tool output"},  # unsupported role folds to user
        {"role": "assistant", "content": "Actions:\n[{\"command\": \"ls\"}]"},
    ]
    assert backend._counts["messages_recorded"] == 3
    assert backend._counts["backend_errors"] == 0


def test_extraction_cadence_and_flush(make_backend, fake_client):
    backend = make_backend(extract_every_n_steps=10)
    backend.start()
    backend.record([{"role": "user", "content": "m0"}], step=5)
    backend.maybe_extract(9)  # bucket 0 already consumed by start? no: bucket 0 is initial
    assert fake_client.add_calls == []  # step 9 // 10 == 0, not past the high-water mark

    backend.maybe_extract(10)  # bucket 1
    assert len(fake_client.add_calls) == 1
    call = fake_client.add_calls[0]
    assert [m["content"] for m in call["messages"]] == ["m0"]
    assert call["user_id"] == "minisweagent"
    assert call["run_id"] == backend._session_id
    assert call["infer"] is True
    assert backend._pending == []
    assert backend._counts["extraction_calls"] == 1
    assert backend._counts["memories_added"] == 1

    backend.maybe_extract(15)  # same bucket
    backend.maybe_extract(19)
    assert len(fake_client.add_calls) == 1

    backend.record([{"role": "user", "content": "m1"}], step=12)
    backend.maybe_extract(20)  # bucket 2
    assert len(fake_client.add_calls) == 2
    assert [m["content"] for m in fake_client.add_calls[1]["messages"]] == ["m1"]


def test_extraction_failure_retains_batch_and_trips_breaker(make_backend, fake_client):
    backend = make_backend(extract_every_n_steps=1, extract_max_consecutive_errors=2)
    backend.start()
    backend.record([{"role": "user", "content": "important"}], step=1)
    fake_client.add_error = Mem0ApiError(504, "poll timeout")
    backend.maybe_extract(1)
    assert backend._counts["extraction_errors"] == 1
    assert backend._pending == [{"role": "user", "content": "important"}]  # retried later

    backend.record([{"role": "user", "content": "second"}], step=2)
    backend.maybe_extract(2)
    assert backend._counts["extraction_errors"] == 2
    assert backend._extract_breaker is True

    fake_client.add_error = None
    backend.maybe_extract(3)  # breaker: periodic ticks disabled
    assert len(fake_client.add_calls) == 2  # only the two failed attempts
    backend._extract("final")  # final flush runs despite the breaker
    assert len(fake_client.add_calls) == 3
    assert [m["content"] for m in fake_client.add_calls[2]["messages"]] == ["important", "second"]


def test_write_then_timeout_add_invalidates_the_recall_cache(make_backend, fake_client):
    """The platform stores the batch but the confirmation poll times out, so
    the add raises after the write landed. The failed counted tick must still
    invalidate the recall cache — otherwise recall keeps serving the memoized
    pre-write payload, and once the breaker trips it stays stale to the
    episode's end."""
    backend = make_backend(extract_every_n_steps=1)
    backend.start()
    backend.set_task("t")
    assert backend.recall_context() is None  # cold cache: the first (empty) search
    assert len(fake_client.search_calls) == 1
    assert backend._search_dirty is False

    backend.record([{"role": "user", "content": "stored-but-unconfirmed"}], step=1)
    storing_add = fake_client.add

    def write_then_timeout(**kwargs):
        storing_add(**kwargs)  # the platform side: the write lands...
        raise Mem0ApiError(504, "poll timeout")  # ...then the poll times out

    fake_client.add = write_then_timeout
    backend.maybe_extract(1)
    assert backend._counts["extraction_errors"] == 1
    assert backend._search_dirty is True  # the write may have landed: never serve the pre-write cache
    recall = backend.recall_context()
    assert len(fake_client.search_calls) == 2  # a fresh search ran
    assert "stored-but-unconfirmed" in recall["content"]  # the just-stored memory is recalled


def test_extraction_event_counts_by_kind(make_backend, fake_client):
    fake_client.add_event = "UPDATE"
    backend = make_backend(extract_every_n_steps=1)
    backend.start()
    backend.record([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}], step=1)
    backend.maybe_extract(1)
    assert backend._counts["memories_updated"] == 2
    assert backend._counts["memories_added"] == 0


def test_recall_render_rank_then_fill(make_backend, fake_client):
    backend = make_backend(max_memories=3, max_total_recall_chars=250)
    backend.start()
    backend.set_task("fix the bug")
    fake_client.memories = {
        f"m{i}": {
            "id": f"m{i}",
            "memory": f"fact number {i} " + "y" * 20,
            "user_id": "minisweagent",
            "score": 0.9,
            "metadata": {},
        }
        for i in range(1, 6)
    }
    recall = backend.recall_context(planned_step=4)
    assert recall is not None
    # The shared policy composes first; the integration's sections follow.
    assert recall["content"].startswith(f"{RECALL_POLICY_DEFAULT}\n\n## Persistent Memory (mem0)")
    # The budget bounds the rendered memory lines only — the header is excluded.
    body = recall["content"].split("## Recalled Memories\n", 1)[1]
    assert len(body) <= 250
    assert recall["n_memories"] == len(recall["memories"])
    assert recall["n_memories"] == 3  # budget/max_memories cut whole lines, 5 matched
    assert recall["chars"] == len(recall["content"])
    assert backend._counts["search_calls"] == 1
    assert fake_client.search_calls[0]["query"] == "fix the bug"
    assert fake_client.search_calls[0]["threshold"] == 0.0
    assert fake_client.search_calls[0]["timeout"] == backend.config.search_timeout


def test_recall_none_cases(make_backend, fake_client):
    backend = make_backend()
    backend.start()
    assert backend.recall_context() is None  # no task yet
    backend.set_task("t")
    assert backend.recall_context() is None  # no memories yet
    backend.record([{"role": "user", "content": "x"}], step=1)
    backend._extract(2)  # a store write dirties the search cache: the next recall re-searches
    fake_client.search_error = Mem0ApiError(500, "boom")
    assert backend.recall_context() is None  # search failure contained
    assert backend._counts["search_errors"] == 1
    assert backend._counts["backend_errors"] == 1
    assert backend.stats()["counts"]["recall_injections"] == 0


def test_recall_inject_off(make_backend, fake_client):
    backend = make_backend(inject_recall=False)
    backend.start()
    backend.set_task("t")
    fake_client.memories["m1"] = {"id": "m1", "memory": "fact", "user_id": "minisweagent"}
    assert backend.recall_context() is None
    assert fake_client.search_calls == []


def test_search_timeout_forwarded_and_fail_closed(make_backend, fake_client):
    """The per-request search_timeout bounds the hosted call; a timed-out
    search fails closed: search_errors counted, recall None, flag stays set
    so the next step retries (never served from an error)."""
    backend = make_backend(search_timeout=7.5)
    backend.start()
    backend.set_task("t")
    fake_client.memories["m1"] = {"id": "m1", "memory": "fact", "user_id": "minisweagent", "score": 0.9}
    assert backend.recall_context() is not None
    assert fake_client.search_calls[0]["timeout"] == 7.5
    backend._mark_store_changed()
    fake_client.search_error = Mem0ApiError(598, "search timed out")
    assert backend.recall_context() is None
    assert backend._counts["search_errors"] == 1
    assert backend._counts["backend_errors"] == 1  # both grains, as before
    assert backend._search_dirty is True  # a failed search is never cached


def test_floor_widens_top_k_and_filters(make_backend, fake_client):
    """With recall_min_score set, the arm requests a wider top_k (the floor
    needs a pool to filter) and drops below-floor/score-less hits BEFORE the
    max_memories slice; without a floor the request stays max_memories-wide."""
    backend = make_backend(recall_min_score=0.1, max_memories=2)
    backend.start()
    backend.set_task("t")
    for i, score in enumerate([0.05, 0.9, 0.8, None]):
        fake_client.memories[f"m{i}"] = {
            "id": f"m{i}",
            "memory": f"fact {i}",
            "user_id": "minisweagent",
            "metadata": {},
        }
        if score is not None:
            fake_client.memories[f"m{i}"]["score"] = score
    recall = backend.recall_context()
    assert fake_client.search_calls[0]["top_k"] == 50  # the widened pool
    assert recall["memories"] and [m["id"] for m in recall["memories"]] == ["m1", "m2"]

    plain = make_backend(instance_id="plain")
    plain.start()
    plain.set_task("t")
    plain.recall_context()
    assert fake_client.search_calls[-1]["top_k"] == 10  # no floor: max_memories


def test_provenance_suffix_from_run_id(make_backend, fake_client):
    """The rendered line names the origin episode via the platform-echoed
    run_id (the per-episode session id minted <instance>-<uuid4hex>)."""
    backend = make_backend()
    backend.start()
    backend.set_task("t")
    other = "pydata__xarray-2905-" + "0" * 32
    for memory_id, run_id in (("own", backend._session_id), ("other", other), ("junk", "run-x")):
        fake_client.memories[memory_id] = {
            "id": memory_id,
            "memory": f"fact {memory_id}",
            "user_id": "minisweagent",
            "run_id": run_id,
            "score": 0.9,
            "metadata": {},
        }
    recall = backend.recall_context()
    content = recall["content"]
    assert "- fact own (from this episode)" in content
    assert "- fact other (from earlier episode pydata__xarray-2905)" in content
    assert "- fact junk (from an earlier episode)" in content  # unparseable run_id
    assert recall["origins"] == [backend._session_id, other, "run-x"]


def test_final_dump_reads_session_id_for_provenance(make_backend, fake_client):
    """The v3 get-all surface omits run_id but carries the same value under
    session_id: the dump keeps provenance by reading it there."""
    backend = make_backend()
    backend.start()
    backend.set_task("t")
    fake_client.memories["m1"] = {
        "id": "m1",
        "memory": "fact",
        "user_id": "minisweagent",
        "run_id": None,  # the get-all row shape
        "session_id": backend._session_id,
        "metadata": {},
    }
    backend.finalize()
    data = json.loads(read_memory_json(backend))
    assert data["final_memories"][0]["run_id"] == backend._session_id


def test_note_recall_counts_and_logs(make_backend):
    backend = make_backend()
    backend.start()
    backend.note_recall({"n_memories": 2, "chars": 42}, step=7)
    backend.note_recall({"n_memories": 0}, step=9)
    assert backend._counts["recall_injections"] == 2
    assert backend._events[-1]["kind"] == "recall"


def test_annotation_hooks_are_noops(make_backend):
    backend = make_backend()
    backend.start()
    assert backend.main_lane_cursor() is None
    assert backend.consume_annotation_duration() == 0.0
    backend.deliver_recall({"content": "x"}, step=1, msg_index=0, cursor=None)  # no-op, no raise


def test_finalize_writes_memory_json_and_closes(make_backend, fake_client):
    backend = make_backend(extract_every_n_steps=1)
    backend.start()
    backend.set_task("task text")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend.maybe_extract(1)
    backend.record([{"role": "user", "content": "tail message"}], step=2)  # only the final flush sees this
    backend.note_recall({"n_memories": 1, "chars": 10}, step=1)
    backend.finalize()
    backend.finalize()  # idempotent

    assert fake_client.closed is True
    data = json.loads(read_memory_json(backend))
    assert data["enabled"] is True
    assert data["available"] is True
    assert data["effective_user_id"] == "minisweagent"
    assert data["settings"]["api_base_url"] == "https://api.mem0.ai"
    assert "api_key" not in json.dumps(data)
    assert data["counts"]["messages_recorded"] == 2
    assert data["counts"]["extraction_calls"] == 2  # periodic + final flush
    assert [m["content"] for m in fake_client.add_calls[1]["messages"]] == ["tail message"]
    assert data["counts"]["memories_added"] == 2
    assert data["counts"]["recall_injections"] == 1
    assert [e["kind"] for e in data["events"]][0] == "start"
    assert any(e["kind"] == "extraction" for e in data["events"])
    assert any(e["kind"] == "finalize_dump" for e in data["events"])
    assert len(data["final_memories"]) == 2
    assert data["final_memories"][0]["memory"].startswith("fact:")
    # End-to-end provenance through the realistic get-all shape: the add
    # stamped the episode's run_id, the get-all row carried it as session_id,
    # and the dump mapped it back.
    assert data["final_memories"][0]["run_id"] == backend._session_id


def test_strict_start_failure_raises(make_backend, monkeypatch):
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    backend = make_backend(api_key="", strict=True)
    with pytest.raises(Exception):
        backend.start()
    assert backend._available is False


def test_finalize_close_error_reraises_under_strict(make_backend, fake_client):
    """A close failure propagates out of _close into finalize's first_error
    (re-raised under strict); the client handle is still nulled."""
    backend = make_backend(strict=True)
    backend.start()

    def raising_close():
        fake_client.closed = True
        raise RuntimeError("close boom")

    fake_client.close = raising_close
    with pytest.raises(RuntimeError, match="close boom"):
        backend.finalize()
    assert fake_client.closed is True
    assert backend._store is None
    assert json.loads(read_memory_json(backend))["available"] is True


def test_finalize_close_error_contained_non_strict(make_backend, fake_client):
    backend = make_backend()
    backend.start()

    def raising_close():
        fake_client.closed = True
        raise RuntimeError("close boom")

    fake_client.close = raising_close
    backend.finalize()  # contained
    assert fake_client.closed is True
    assert backend._store is None


def test_recall_malformed_hit_skipped(make_backend, fake_client, monkeypatch):
    """A malformed search hit (non-dict row, non-string memory) is an
    unrenderable line: the renderer skips it like an empty one, the
    well-formed hits still inject, and nothing is counted — one bad row must
    not fail the whole recall."""
    monkeypatch.setattr(
        fake_client,
        "search",
        lambda **kwargs: [
            {"id": "m1", "memory": "ok fact"},
            "not-a-dict",
            {"id": "m2", "memory": {"unexpected": "shape"}},
            {"id": "m3", "memory": 42},
        ],
    )
    backend = make_backend()
    backend.start()
    backend.set_task("t")
    recall = backend.recall_context()
    assert recall is not None
    assert recall["n_memories"] == 1
    assert "- ok fact" in recall["content"]
    assert backend._counts["backend_errors"] == 0
    assert not any(e.get("op") == "recall" for e in backend._events)


def test_recall_id_less_hit_dropped(make_backend, fake_client, monkeypatch):
    """A hit without a platform id is uncitable (it would fabricate the item
    identity every such row collapses into): dropped at the search intake,
    like the endpoint adapter does — never rendered, never delivered."""
    monkeypatch.setattr(
        fake_client,
        "search",
        lambda **kwargs: [
            {"memory": "id-less but well-formed text"},
            {"id": "", "memory": "empty id"},
            {"id": "m1", "memory": "ok fact"},
        ],
    )
    backend = make_backend()
    backend.start()
    backend.set_task("t")
    recall = backend.recall_context()
    assert recall is not None
    assert recall["n_memories"] == 1
    assert "- ok fact" in recall["content"]
    assert backend._counts["backend_errors"] == 0


def test_api_base_url_is_sanitized_in_artifacts(make_backend):
    """A base URL carrying userinfo/query/fragment persists only in sanitized
    form (credentials never reach memory.json or stats); the client itself is
    still constructed from the real URL."""
    backend = make_backend(base_url="https://user:secret@proxy.invalid:8443/v1?token=x#frag")
    backend.start()
    backend.finalize()
    data = json.loads(read_memory_json(backend))
    assert data["settings"]["api_base_url"] == "https://proxy.invalid:8443/v1"
    # The credential-bearing substrings of the URL, not bare words: the
    # artifact legitimately carries prompt text (the extraction guidelines)
    # whose vocabulary may overlap a leak-pin's word choice.
    assert "user:secret" not in json.dumps(data)
    assert "token=x" not in json.dumps(data)
    assert backend.stats()["api_base_url"] == "https://proxy.invalid:8443/v1"


# ---------------------------------------------------------------------------
# Extraction guidelines (the shared policy layer via the store's guidelines channel)
# ---------------------------------------------------------------------------
def test_extraction_add_carries_the_default_guidelines(make_backend, fake_client):
    """No override: every extraction add carries the shared default guidelines
    (episode context appended) through the add's guidelines channel, and memory.json records
    the conveyed text."""
    backend = make_backend(extract_every_n_steps=10)
    backend.start()
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend.maybe_extract(10)
    conveyed = (
        f"{EXTRACTION_GUIDELINES_DEFAULT}\n\n{_episode_context('test-instance')}"
    )
    assert fake_client.add_calls[0]["guidelines"] == conveyed
    backend.finalize()
    settings = json.loads(read_memory_json(backend))["settings"]
    assert settings["extraction_guidelines"] == conveyed


def test_extraction_guidelines_override_replaces_the_default(make_backend, fake_client):
    """An override replaces the shared default wholesale: the store receives
    the override text, never default + override concatenated. The base's
    episode context still rides along — episode fact, not policy."""
    backend = make_backend(extract_every_n_steps=10, extraction_guidelines="prefer operational facts")
    backend.start()
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend.maybe_extract(10)
    assert (
        fake_client.add_calls[0]["guidelines"]
        == f"prefer operational facts\n\n{_episode_context('test-instance')}"
    )

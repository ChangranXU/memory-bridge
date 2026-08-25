"""The tencentdb arm's schema-v6 trace surface: watermark-resolved L1 rows
become generation changes (native_receipt evidence, partial completeness),
recalls become search + delivery — all against the real capture server with
the scripted gateway client. The protocol machinery itself is pinned
generically in shared-bridge/tests/test_trace.py; here only the tencentdb
adapter shapes."""

import hashlib

from tencentdb_bridge.backend import TencentDBBackend
from tencentdb_bridge.config import TencentDBConfig

from shared_bridge.annotate import text_sha256


def _changes(server):
    return [event["payload"] for event in server.events("memory_change")]


def _ends(server):
    return [event["payload"] for event in server.events("memory_generate_end")]


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def test_session_and_binds_with_tencentdb_adapter(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("fix the bug")
    (session,) = capture_server.events("memory_session")
    payload = session["payload"]
    assert payload["adapter"]["name"] == "tencentdb"
    assert payload["adapter"]["version"] == backend._adapter_meta()["version"]
    assert payload["extensions"]["tencentdb"]["session_id"] == backend._session_id
    assert payload["extensions"]["tencentdb"]["team_id"] == "minisweagent"
    assert payload["extensions"]["tencentdb"]["agent_id"] == "memory-bridge"
    # The namespace is the hashed effective user id (the store scope).
    assert backend._namespace == hashlib.sha256(b"minisweagent").hexdigest()
    binds = capture_server.events("memory_role_bind")
    assert [b["payload"]["logical_role"] for b in binds] == ["main", "memory"]
    backend.finalize()


def test_memory_lane_resolves_from_env(tmp_path, capture_server, fake_client, monkeypatch):
    """The memory lane carries no model URL: its endpoint comes from the
    MEMORY_ANNOTATE_MEMORY_URL env when the config field is blank."""
    monkeypatch.setenv("MEMORY_ANNOTATE_MEMORY_URL", capture_server.annotate_url("MEMORY"))
    config = TencentDBConfig(
        enabled=True, output_dir=str(tmp_path / "inst"), run_root=str(tmp_path)
    )
    backend = TencentDBBackend(config, "test-instance", model_base_url=capture_server.lane_url("MAIN"))
    backend.start()
    assert backend._trace is not None
    backend.set_task("task")
    assert len(capture_server.events("memory_session")) == 1
    backend.finalize()


# ---------------------------------------------------------------------------
# Generation (watermark rows -> changes)
# ---------------------------------------------------------------------------
def test_watermark_rows_become_create_changes(traced_backend, capture_server):
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    (start,) = capture_server.events("memory_generate_start")
    assert start["payload"]["extensions"]["tencentdb"]["extraction_step"] == "2"
    (change,) = _changes(capture_server)
    assert change["action"] == "create" and change["before"] == []
    (ref,) = change["after"]
    assert ref["item_id"] == "a1"
    assert ref["version_id"] == f"a1:{text_sha256('fact: hello')}"
    assert ref["identity_scheme"] == "tencentdb-memorycore-l1-v1"
    assert ref["identity_strength"] == "native_stable"
    assert ref["namespace"] == backend._namespace
    assert ref["content"]["text"] == "fact: hello"
    assert change["evidence"] == "native_receipt"
    assert change["completeness"] == "partial"  # the async pipeline offers no before-image
    assert change["extensions"]["tencentdb"]["version"] == 0  # fresh L1 rows carry version 0
    assert (change["change_index"], change["change_count"]) == (0, 1)
    (end,) = _ends(capture_server)
    assert end["status"] == "completed"
    assert [r["version_id"] for r in end["produced"]] == [ref["version_id"]]
    assert end["change_count"] == 1 and end["error_codes"] == []
    assert end["extensions"]["tencentdb"]["added"] == 1
    assert end["extensions"]["tencentdb"]["task_id"] == "test-instance"
    backend.finalize()


def test_merged_rows_become_update_changes(traced_backend, capture_server, fake_client):
    fake_client.next_version = 1  # the pipeline's dedup merge rewrites the row (fresh = 0)
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    (change,) = _changes(capture_server)
    assert change["action"] == "update"
    assert change["extensions"]["tencentdb"]["version"] == 1
    (end,) = _ends(capture_server)
    assert end["extensions"]["tencentdb"]["updated"] == 1
    backend.finalize()


def _stamp(offset_seconds=0) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def test_empty_watermark_is_a_zero_change_generation(traced_backend, capture_server, fake_client):
    fake_client.auto_produce = False  # the extractor produced nothing this tick
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    assert _changes(capture_server) == []
    (end,) = _ends(capture_server)
    assert end["status"] == "completed" and end["change_count"] == 0 and end["produced"] == []
    backend.finalize()


def test_failed_extraction_ends_failed_and_buffer_retries(traced_backend, capture_server, fake_client):
    fake_client.add_error = RuntimeError("gateway down")
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    (end,) = _ends(capture_server)
    assert end["status"] == "failed"
    assert end["error_codes"] == ["RuntimeError"]
    assert backend._pending  # retained, retried at the next boundary
    fake_client.add_error = None
    backend._extract(4)
    ends = _ends(capture_server)
    assert ends[-1]["status"] == "completed"
    backend.finalize()


def test_id_less_watermark_row_emits_no_change(traced_backend, capture_server, fake_client):
    fake_client.auto_produce = False
    fake_client.rows.append({"content": "id-less", "version": 0, "updated_at": _stamp()})
    backend = traced_backend()
    backend.set_task("task")
    backend.record([{"role": "user", "content": "hello"}], step=1)
    backend._extract(2)
    assert _changes(capture_server) == []
    (end,) = _ends(capture_server)
    assert end["change_count"] == 0 and end["produced"] == []
    backend.finalize()


# ---------------------------------------------------------------------------
# Search / delivery
# ---------------------------------------------------------------------------
def test_search_end_carries_l1_refs_and_scores(traced_backend, capture_server, fake_client):
    fake_client.search_hits = [
        {"id": "a1", "content": "use pytest -k", "score": 0.033, "created_at": None}
    ]
    backend = traced_backend()
    backend.set_task("fix the bug")
    payload = backend.recall_context(planned_step=1)
    assert payload is not None
    cursor = backend.main_lane_cursor()
    backend.deliver_recall(payload, step=2, msg_index=4, cursor=cursor)
    (end,) = capture_server.events("memory_search_end")
    (item,) = end["payload"]["returned"]
    ref = item["memory"]
    assert ref["item_id"] == "a1"
    assert ref["identity_scheme"] == "tencentdb-memorycore-l1-v1"
    assert ref["extensions"]["tencentdb"]["score"] == 0.033
    (delivery,) = capture_server.events("memory_delivery")
    assert delivery["payload"]["status"] == "placed"
    backend.finalize()


def test_persona_ref_in_search_end(traced_backend, capture_server, fake_client):
    fake_client.persona = {"content": "Prefers minimal diffs."}
    fake_client.search_hits = []
    backend = traced_backend()
    backend.set_task("fix the bug")
    payload = backend.recall_context(planned_step=1)
    assert payload is not None and payload["memories"][0]["id"] == "persona"
    (end,) = capture_server.events("memory_search_end")
    (item,) = end["payload"]["returned"]
    ref = item["memory"]
    assert ref["item_id"] == "persona"
    assert ref["identity_scheme"] == "tencentdb-memorycore-l1-v1"
    backend.finalize()

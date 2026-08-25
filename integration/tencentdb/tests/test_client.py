"""Wire-format tests for the MemoryCore gateway client (mock httpx transport)."""

import json

import httpx
import pytest

from tencentdb_bridge.client import ADD_CHUNK_MESSAGES, TencentDBApiError, TencentDBClient

ISOLATION = {"team_id": "minisweagent", "agent_id": "memory-bridge", "user_id": "u1"}


def make_client(handler):
    """A real client over a MockTransport; records every request seen."""
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = TencentDBClient(
        "http://127.0.0.1:8420/", api_key="local", service_id="default", transport=httpx.MockTransport(wrapped)
    )
    return client, requests


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


def envelope(data=None, code=0, message="ok"):
    return httpx.Response(200, json={"code": code, "message": message, "request_id": "req-1", "data": data})


# ---------------------------------------------------------------------------
# Envelope handling
# ---------------------------------------------------------------------------
def test_envelope_code_zero_returns_data():
    client, _ = make_client(lambda req: envelope({"items": [{"id": "a1"}]}))
    assert client.atomic_search("q", limit=5, **ISOLATION) == [{"id": "a1"}]


def test_quota_code_outside_http_range_rides_http_200_but_raises():
    # Envelope codes outside [400, 600) ship with HTTP 200 — the client keys
    # on the envelope code, never on the HTTP status alone.
    client, _ = make_client(lambda req: httpx.Response(200, json={"code": 4291, "message": "quota exceeded", "request_id": "r"}))
    with pytest.raises(TencentDBApiError) as excinfo:
        client.pipeline_status()
    assert excinfo.value.status_code == 4291
    assert "quota" in excinfo.value.reason


def test_envelope_code_in_http_range_mirrors_into_status():
    client, _ = make_client(lambda req: httpx.Response(404, json={"code": 404, "message": "not found", "request_id": "r"}))
    with pytest.raises(TencentDBApiError) as excinfo:
        client.atomic_update("a1", content="x", **ISOLATION)
    assert excinfo.value.status_code == 404


def test_http_error_without_envelope_raises():
    client, _ = make_client(lambda req: httpx.Response(500, json={"detail": "boom"}))
    with pytest.raises(TencentDBApiError) as excinfo:
        client.pipeline_status()
    assert excinfo.value.status_code == 500
    assert "boom" in excinfo.value.reason


def test_transport_error_wrapped_into_api_error():
    # A stalled/dropped gateway connection must surface as the client's one
    # error type, never a raw httpx exception leaking past the integration's
    # error boundaries (the endpoint's 500 mapping, the drain's absorption).
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(handler)[0]
    with pytest.raises(TencentDBApiError) as excinfo:
        client.pipeline_status()
    assert excinfo.value.status_code == 503
    assert "connection refused" in excinfo.value.reason


def test_error_message_extraction_prefers_envelope_message():
    client, _ = make_client(
        lambda req: httpx.Response(200, json={"code": 422, "message": "  isolation failed  ", "request_id": "r"})
    )
    with pytest.raises(TencentDBApiError) as excinfo:
        client.conversation_add([{"role": "user", "content": "hi"}], session_id="s", **ISOLATION)
    assert excinfo.value.reason == "isolation failed"


# ---------------------------------------------------------------------------
# Mandatory headers on every data-plane request
# ---------------------------------------------------------------------------
def test_auth_and_service_headers_on_every_request():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return envelope({})

    client = make_client(handler)[0]
    client.pipeline_status()
    client.conversation_add([{"role": "user", "content": "hi"}], session_id="s", **ISOLATION)
    client.atomic_search("q", limit=5, **ISOLATION)
    client.scenario_ls(**ISOLATION)
    client.core_read(**ISOLATION)
    assert seen
    for request in seen:
        assert request.headers["Authorization"] == "Bearer local"
        assert request.headers["x-tdai-service-id"] == "default"
        assert request.method == "POST"  # the data plane is POST-only


def test_health_is_get_without_envelope():
    client, _ = make_client(lambda req: httpx.Response(200, json={"status": "ok"}) if req.method == "GET" else httpx.Response(500))
    assert client.health()["status"] == "ok"


# ---------------------------------------------------------------------------
# Route paths
# ---------------------------------------------------------------------------
def test_data_plane_paths():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return envelope({"items": [], "entries": [], "total": 0} if "search" in request.url.path or "ls" in request.url.path else {})

    client = make_client(handler)[0]
    client.pipeline_status()
    client.conversation_add([{"role": "user", "content": "hi"}], session_id="s", **ISOLATION)
    client.atomic_search("q", limit=5, **ISOLATION)
    client.atomic_query(**ISOLATION)
    client.atomic_count(**ISOLATION)
    client.scenario_ls(**ISOLATION)
    client.scenario_read("scenes/a.md", **ISOLATION)
    client.core_read(**ISOLATION)
    client.atomic_update("a1", content="c", **ISOLATION)
    client.atomic_delete(["a1"], **ISOLATION)
    paths = [request.url.path for request in seen]
    assert paths == [
        "/v2/pipeline/status",  # the one /v2 exception (standalone-only)
        "/v3/conversation/add",
        "/v3/atomic/search",
        "/v3/atomic/query",
        "/v3/atomic/count",  # /v3-only (count endpoints are not routed on /v2)
        "/v3/scenario/ls",
        "/v3/scenario/read",
        "/v3/core/read",
        "/v3/atomic/update",
        "/v3/atomic/delete",
    ]


# ---------------------------------------------------------------------------
# conversation/add: chunking + isolation quadruple
# ---------------------------------------------------------------------------
def test_conversation_add_chunks_at_100_messages():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(body_of(request))
        return envelope({"accepted_ids": []})

    client = make_client(handler)[0]
    messages = [{"role": "user", "content": f"m{i}"} for i in range(ADD_CHUNK_MESSAGES + 50)]
    client.conversation_add(messages, session_id="sess-1", task_id="pydata__xarray", **ISOLATION)
    assert [len(call["messages"]) for call in calls] == [ADD_CHUNK_MESSAGES, 50]
    for call in calls:
        assert call["session_id"] == "sess-1"
        assert call["team_id"] == "minisweagent"
        assert call["task_id"] == "pydata__xarray"


def test_conversation_add_omits_empty_task_id():
    client, requests = make_client(lambda req: envelope({}))
    client.conversation_add([{"role": "user", "content": "hi"}], session_id="s", **ISOLATION)
    assert "task_id" not in body_of(requests[0])


def test_conversation_add_mid_chunk_failure_marks_confirmed_prefix():
    """Chunk 2 of 3 failing leaves chunk 1 confirmed server-side: the raised
    error carries persisted_messages so a retry drops that prefix instead of
    re-feeding it (wholesale duplicates + double-counted user-rounds)."""
    seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        if seen == 2:
            return httpx.Response(503, json={"code": 503, "message": "boom", "request_id": "r"})
        return envelope({"accepted_ids": []})

    client = make_client(handler)[0]
    messages = [{"role": "user", "content": f"m{i}"} for i in range(2 * ADD_CHUNK_MESSAGES + 10)]
    with pytest.raises(TencentDBApiError) as excinfo:
        client.conversation_add(messages, session_id="sess-1", **ISOLATION)
    assert excinfo.value.persisted_messages == ADD_CHUNK_MESSAGES  # only chunk 1 confirmed
    assert seen == 2  # chunk 3 never sent


def test_conversation_add_first_chunk_failure_marks_nothing_persisted():
    client = make_client(lambda req: httpx.Response(503, json={"code": 503, "message": "boom", "request_id": "r"}))[0]
    messages = [{"role": "user", "content": f"m{i}"} for i in range(ADD_CHUNK_MESSAGES + 1)]
    with pytest.raises(TencentDBApiError) as excinfo:
        client.conversation_add(messages, session_id="sess-1", **ISOLATION)
    assert excinfo.value.persisted_messages == 0  # wholly uncertain: retry the full buffer


def test_atomic_search_body_shape():
    client, requests = make_client(lambda req: envelope({"items": []}))
    client.atomic_search("how to fix", limit=15, task_id="pydata__xarray", **ISOLATION)
    body = body_of(requests[0])
    assert body["query"] == "how to fix"
    assert body["limit"] == 15
    assert body["task_id"] == "pydata__xarray"
    assert "session_id" not in body


def test_atomic_search_truncates_query_to_wire_cap():
    # The recall query is the full task text (often > 2048 chars); the zod
    # schema would 400 it — the client applies the wire cap mechanically.
    client, requests = make_client(lambda req: envelope({"items": []}))
    client.atomic_search("x" * 5000, limit=5, **ISOLATION)
    body = body_of(requests[0])
    assert len(body["query"]) == 2048


def test_atomic_search_query_cap_counts_utf16_units():
    # The zod query cap counts JavaScript's String.length (UTF-16 code
    # units): an astral-heavy query clamps by units, so the gateway never
    # 400s a search a code-point slice would have let through (recall would
    # fail closed on every step for such a task).
    from tencentdb_bridge.client import SEARCH_QUERY_MAX_CHARS, utf16_units

    client, requests = make_client(lambda req: envelope({"items": []}))
    client.atomic_search("😀" * 3000, limit=5, **ISOLATION)  # 3000 code points, 6000 UTF-16 units
    body = body_of(requests[0])
    assert utf16_units(body["query"]) == SEARCH_QUERY_MAX_CHARS
    assert len(body["query"]) == SEARCH_QUERY_MAX_CHARS // 2  # every char is one surrogate pair


def test_atomic_search_empty_query_returns_no_hits():
    client, requests = make_client(lambda req: envelope({"items": []}))
    assert client.atomic_search("   ", limit=5, **ISOLATION) == []
    assert requests == []


def test_atomic_search_drops_non_dict_items():
    client, _ = make_client(lambda req: envelope({"items": [{"id": "a1"}, "junk", 3]}))
    assert client.atomic_search("q", limit=5, **ISOLATION) == [{"id": "a1"}]


# ---------------------------------------------------------------------------
# atomic/query: watermark pagination (limit 100, offset on total)
# ---------------------------------------------------------------------------
def test_atomic_query_paginates_on_total():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(body_of(request))
        if len(calls) == 1:
            return envelope({"items": [{"id": f"a{i}"} for i in range(100)], "total": 150})
        return envelope({"items": [{"id": f"a{i}"} for i in range(100, 150)], "total": 150})

    client = make_client(handler)[0]
    rows = client.atomic_query(time_start="2026-01-01T00:00:00Z", **ISOLATION)
    assert len(rows) == 150
    assert calls[0]["limit"] == 100
    assert calls[0]["offset"] == 0
    assert calls[1]["offset"] == 100
    assert all(call["time_start"] == "2026-01-01T00:00:00Z" for call in calls)


def test_atomic_query_stops_when_page_short():
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"items": [{"id": "a1"}], "total": 1})

    client = make_client(handler)[0]
    assert len(client.atomic_query(**ISOLATION)) == 1


def test_atomic_query_missing_total_pages_until_short_page():
    # No usable total: the first page's own count must NOT read as the full
    # total (that would silently end pagination and advance a watermark past
    # never-fetched rows) — paging continues until a short page.
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(body_of(request))
        if len(calls) == 1:
            return envelope({"items": [{"id": f"a{i}"} for i in range(100)]})
        return envelope({"items": [{"id": f"a{i}"} for i in range(100, 130)]})

    client = make_client(handler)[0]
    rows = client.atomic_query(**ISOLATION)
    assert len(rows) == 130
    assert [call["offset"] for call in calls] == [0, 100]


def test_atomic_query_short_page_under_uncovered_total_keeps_paging():
    # A page shorter than the limit while a usable total says rows remain
    # must not end the walk, and the next offset steps by the page's actual
    # length — a fixed step would overshoot and silently skip rows.
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(body_of(request))
        if len(calls) == 1:
            return envelope({"items": [{"id": f"a{i}"} for i in range(40)], "total": 60})
        return envelope({"items": [{"id": f"a{i}"} for i in range(40, 60)], "total": 60})

    client = make_client(handler)[0]
    rows = client.atomic_query(**ISOLATION)
    assert len(rows) == 60
    assert [call["offset"] for call in calls] == [0, 40]


def test_atomic_query_string_total_is_treated_as_missing():
    # A non-int total (null, string, bool) is unusable, not zero-like.
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"items": [{"id": "a1"}], "total": "many"})

    client = make_client(handler)[0]
    assert client.atomic_query(**ISOLATION) == [{"id": "a1"}]


def test_atomic_count_carries_task_id():
    client, requests = make_client(lambda req: envelope({"total": 3}))
    assert client.atomic_count(task_id="pydata__xarray", **ISOLATION) == 3
    assert body_of(requests[0])["task_id"] == "pydata__xarray"


def test_atomic_count_non_int_total_reads_zero():
    # A degenerate envelope (total missing or the wrong type) is 0, never a
    # crash or a coerced string count.
    client, _ = make_client(lambda req: envelope({"total": "5"}))
    assert client.atomic_count(**ISOLATION) == 0


def test_atomic_delete_non_int_deleted_count_reads_zero():
    client, _ = make_client(lambda req: envelope({"deleted_count": "1"}))
    assert client.atomic_delete(["a1"], **ISOLATION) == 0


# ---------------------------------------------------------------------------
# scenario / core reads: null-field 200 answers are not errors
# ---------------------------------------------------------------------------
def test_scenario_ls_tolerates_missing_summary_entries():
    client, _ = make_client(
        lambda req: envelope({"entries": [{"path": "scenes/a.md", "summary": "s"}, {"path": "scenes/b.md"}], "total": 2})
    )
    entries = client.scenario_ls(**ISOLATION)
    assert [entry.get("summary") for entry in entries] == ["s", None]


def test_scenario_read_missing_file_returns_null_content():
    client, _ = make_client(lambda req: envelope({"path": "x.md", "content": None, "created_at": None}))
    data = client.scenario_read("x.md", **ISOLATION)
    assert data["content"] is None


def test_core_read_not_generated_returns_nulls():
    client, _ = make_client(lambda req: envelope({"content": None, "created_at": None, "updated_at": None}))
    assert client.core_read(**ISOLATION)["content"] is None


# ---------------------------------------------------------------------------
# L1 idle drain polling
# ---------------------------------------------------------------------------
def test_wait_l1_idle_polls_until_idle():
    answers = [{"l1": {"idle": False}}, {"l1": {"idle": False}}, {"l1": {"idle": True}}]

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(answers.pop(0) if answers else {"l1": {"idle": True}})

    client = make_client(handler)[0]
    assert client.wait_l1_idle(5.0, 0.01) is True


def test_wait_l1_idle_times_out_within_budget():
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"l1": {"idle": False, "queued": 1}})

    client = make_client(handler)[0]
    assert client.wait_l1_idle(0.05, 0.02) is False


def test_wait_l1_idle_survives_transient_status_errors():
    # A failed status poll keeps polling within the budget instead of
    # failing the whole drain.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"detail": "transient"})
        return envelope({"l1": {"idle": True}})

    client = make_client(handler)[0]
    assert client.wait_l1_idle(5.0, 0.01) is True


def test_wait_l1_idle_survives_transport_timeouts():
    # A status poll that outlives the client timeout (httpx.ReadTimeout) is
    # absorbed exactly like an envelope error: a transiently slow gateway
    # keeps polling instead of failing the drain with a raw transport error.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return envelope({"l1": {"idle": True}})

    client = make_client(handler)[0]
    assert client.wait_l1_idle(5.0, 0.01) is True

"""Synchronous REST client for the MemoryCore gateway (standalone mode).

Hand-rolled over httpx (house style — the vendored Python SDK is never a
dependency). The data plane is uniformly ``/v3`` (the count endpoints are
mounted on /v3 only) with exactly one /v2 exception: ``POST
/v2/pipeline/status`` (v2-only, standalone-only).

Wire rules verified against the upstream router (v2-router.ts):
- every request carries ``Authorization: Bearer <non-empty>`` and
  ``x-tdai-service-id`` — enforced by ``parseV2Auth`` on every v2/v3 route
  even with gateway auth off;
- responses ride the envelope ``{code, message, request_id, data}`` — envelope
  codes in [400, 600) mirror into the HTTP status, codes outside it (e.g. the
  4291 quota code) ship with HTTP 200, so the client keys on the envelope
  code, never on the HTTP status alone;
- isolation ids (team/agent/user/session/task) ride the JSON body;
- ``scenario/read`` and ``core/read`` answer HTTP 200 with null fields when
  nothing exists — never an error.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

# The arm's fixed team/agent isolation ids (the other two ids are run- and
# episode-scoped): the one home shared by the backend and the endpoint adapter.
TEAM_ID = "minisweagent"
AGENT_ID = "memory-bridge"

# conversation/add accepts at most 100 messages per call (zod cap).
ADD_CHUNK_MESSAGES = 100
# One message's content cap (zod, shared by conversation/add items and
# atomic/update text): a caller over it draws a gateway 400. Zod counts
# JavaScript's String.length — UTF-16 code units — so host-side checks and
# clamps must measure the same unit (utf16_units / clamp_utf16_units below),
# never Python's code-point len: an astral-heavy string passes a len() check
# yet busts the wire cap.
MESSAGE_CONTENT_MAX_CHARS = 8192


def utf16_units(text: str) -> int:
    """Length in UTF-16 code units (surrogatepass keeps a lone surrogate at
    one unit, matching JavaScript)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def clamp_utf16_units(text: str, max_units: int) -> str:
    """Cap ``text`` at ``max_units`` UTF-16 code units, never splitting a
    surrogate pair (an astral char is one Python char)."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > max_units:
            return text[:index]
    return text


# atomic/query's schema max limit; the default is 20, so watermark resolution
# must paginate explicitly or it silently truncates the produced-id list.
QUERY_PAGE_LIMIT = 100
# atomic/search's own schema max limit (a distinct wire cap from
# QUERY_PAGE_LIMIT even though both are 100): callers clamp their fetch here.
SEARCH_LIMIT_MAX = 100
# atomic/search caps the query at 2048 UTF-16 code units (zod counts
# JavaScript's String.length — see MESSAGE_CONTENT_MAX_CHARS); the arm's
# recall query is the full SWE-bench task text, so the wire cap is applied
# here — mechanical, like the add chunking, never a ranking-policy decision.
SEARCH_QUERY_MAX_CHARS = 2048

# Synthetic status for a transport-level failure (connect/read timeout,
# connection dropped): no envelope and no HTTP status exists to key on.
TRANSPORT_ERROR_STATUS = 503

# Drain-policy constants shared by the backend's finalize drain and the
# endpoint's add drain — one home, so tuning one surface cannot silently
# diverge from the other.
# The unconditional wait margin over l1IdleTimeoutSeconds: an armed idle
# timer fires within the timeout; the margin covers the timer-scanner
# cadence (2 s upstream default) plus scheduler jitter.
IDLE_WAIT_MARGIN_SECONDS = 5.0
# Host-vs-container clock skew tolerance for the resolve watermark: the
# window filters on the gateway's own updated_time clock, so a host clock
# running ahead of the container's would strand the window's first rows (the
# open window never narrows — there is no later chance to see them). Prior
# rows sit minutes behind the window start, so the margin cannot re-pull them.
WATERMARK_SKEW_SECONDS = 5.0


class TencentDBApiError(RuntimeError):
    """One failed gateway call: envelope code, HTTP status, or transport
    failure (``TRANSPORT_ERROR_STATUS`` — the gateway was never reached or
    stalled mid-call) + reason. ``persisted_messages`` is set only by the
    chunked ``conversation_add`` on a mid-chunk failure: the count of
    messages whose chunks already returned success (confirmed server-side),
    so a retry can drop that prefix instead of re-feeding it."""

    def __init__(self, status_code: int, reason: str):
        super().__init__(f"TencentDB gateway error {status_code}: {reason}")
        self.status_code = status_code
        self.reason = reason
        self.persisted_messages = 0


def utc_now_iso(*, skew_seconds: float = 0.0) -> str:
    """UTC now in ISO 8601 with a ``Z`` suffix — the watermark clock.
    ``skew_seconds`` shifts the stamp into the past: the watermark filters on
    the gateway's own ``updated_time`` clock, so a host clock running ahead of
    the container's would otherwise strand the window's first rows."""
    return (datetime.now(timezone.utc) - timedelta(seconds=skew_seconds)).isoformat().replace("+00:00", "Z")


def _envelope_error_reason(body: dict, code: int) -> str:
    message = body.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return f"gateway returned business code {code}"


class TencentDBClient:
    """Thin sync client over one MemoryCore gateway container."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str = "local",
        service_id: str = "default",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._endpoint = endpoint.rstrip("/")
        self._headers = {
            # parseV2Auth demands a non-empty Bearer even with auth off; the
            # value names nothing (gateway apiKey is unset on this arm).
            "Authorization": f"Bearer {api_key or 'local'}",
            "x-tdai-service-id": service_id,
        }
        self._client = httpx.Client(
            base_url=self._endpoint,
            headers=self._headers,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, *, json: dict | None = None, timeout: float | None = None) -> dict:
        """One call; returns the envelope's ``data`` dict.

        Errors are keyed on the envelope code: codes in [400, 600) arrive
        mirrored into the HTTP status, everything else rides HTTP 200 — an
        HTTP-only check would misread a quota 4291 as success. Transport-level
        failures (connect error, read timeout) are wrapped into
        ``TencentDBApiError`` too: every caller keys on that one type, so a
        stalled gateway must not leak raw httpx exceptions past the
        integration's error boundaries (the endpoint adapter's 500 mapping,
        the drain loop's transient-error absorption).
        """
        request_timeout = timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT
        try:
            response = self._client.request(method, path, json=json if json is not None else {}, timeout=request_timeout)
        except httpx.RequestError as e:
            raise TencentDBApiError(TRANSPORT_ERROR_STATUS, f"gateway transport error: {e}") from e
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "code" in body:
            code = body.get("code")
            if isinstance(code, (int, float)) and code != 0:
                raise TencentDBApiError(int(code), _envelope_error_reason(body, int(code)))
            data = body.get("data")
            return data if isinstance(data, dict) else {}
        if response.status_code >= 400:
            reason = ""
            if isinstance(body, dict):
                detail = body.get("detail") or body.get("error") or body.get("message")
                if isinstance(detail, str):
                    reason = detail.strip()
            raise TencentDBApiError(
                response.status_code, reason or f"gateway returned HTTP {response.status_code} with no envelope"
            )
        # /health and other non-envelope routes.
        return body if isinstance(body, dict) else {}

    def _post(self, path: str, body: dict, *, timeout: float | None = None) -> dict:
        return self._request("POST", path, json=body, timeout=timeout)

    # ------------------------------------------------------------------
    # Health / pipeline
    # ------------------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health")

    def pipeline_status(self, *, timeout: float | None = None) -> dict:
        """Per-layer queue status: ``{l1, l2, l3}``, each with ``idle``."""
        return self._post("/v2/pipeline/status", {}, timeout=timeout)

    def l1_idle(self, *, timeout: float | None = None) -> bool:
        status = self.pipeline_status(timeout=timeout)
        l1 = status.get("l1")
        return bool(isinstance(l1, dict) and l1.get("idle"))

    def wait_l1_idle(self, budget: float, interval: float, *, timeout: float | None = None) -> bool:
        """Poll ``l1.idle`` until true or the budget runs out (True = idle).

        Every error class is absorbed — envelope errors AND transport errors
        (a status poll that outlives the client timeout): a transiently slow
        gateway keeps polling within the budget instead of failing the drain.
        """
        deadline = time.monotonic() + budget
        while True:
            try:
                if self.l1_idle(timeout=timeout):
                    return True
            except TencentDBApiError:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(interval, remaining))

    # ------------------------------------------------------------------
    # L0: conversation capture (feeds the extraction pipeline)
    # ------------------------------------------------------------------
    def conversation_add(
        self,
        messages: list[dict],
        *,
        team_id: str,
        agent_id: str,
        user_id: str,
        session_id: str,
        task_id: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Chunked ``conversation/add`` (<=100 messages per call, the zod cap).

        Returns the last chunk's data (accepted ids/counts); earlier chunks
        are covered by the pipeline's own capture state. On a mid-chunk
        failure the raised error carries ``persisted_messages`` — the
        confirmed prefix length (earlier chunks' responses came back); only
        the failed chunk onward is an uncertain outcome.
        """
        data: dict = {}
        sent = 0
        try:
            for start in range(0, len(messages), ADD_CHUNK_MESSAGES):
                chunk = messages[start : start + ADD_CHUNK_MESSAGES]
                body: dict[str, Any] = {
                    "session_id": session_id,
                    "team_id": team_id,
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "messages": chunk,
                }
                if task_id:
                    body["task_id"] = task_id
                data = self._post("/v3/conversation/add", body, timeout=timeout)
                sent = start + len(chunk)
        except TencentDBApiError as e:
            e.persisted_messages = sent
            raise
        return data

    # ------------------------------------------------------------------
    # L1: atomic memories
    # ------------------------------------------------------------------
    def atomic_search(
        self,
        query: str,
        *,
        limit: int,
        team_id: str,
        agent_id: str,
        user_id: str,
        task_id: str | None = None,
        timeout: float | None = None,
    ) -> list[dict]:
        """One atomic/search call. (The L0 conversation/search route has no
        client method: its only consumer is the agent's own curl via the
        recall header's guide.)"""
        if not query.strip():
            return []  # the schema demands >=1 char; nothing to rank anyway
        body: dict[str, Any] = {
            "query": clamp_utf16_units(query, SEARCH_QUERY_MAX_CHARS),
            "limit": limit,
            "team_id": team_id,
            "agent_id": agent_id,
            "user_id": user_id,
        }
        if task_id:
            # The search deliberately drops session_id (cross-session
            # recall); task_id is the repo tier.
            body["task_id"] = task_id
        data = self._post("/v3/atomic/search", body, timeout=timeout)
        items = data.get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def atomic_query(
        self,
        *,
        team_id: str,
        agent_id: str,
        user_id: str,
        time_start: str | None = None,
        task_id: str | None = None,
        page_limit: int = 5000,
        timeout: float | None = None,
    ) -> list[dict]:
        """L1 rows filtered on ``updated_time``, paginated on ``total``.

        The schema max limit is 100 and the default 20, so any caller that
        must see every row (the extraction watermark resolution) paginates
        with offset stepping or silently truncates. Offsets step by each
        page's actual length (a fixed step would overshoot a short page and
        skip rows); a usable ``total`` ends the walk at the coverage
        boundary, and without one a short or empty page is the only honest
        end signal. ``page_limit`` stays the runaway guard.
        """
        rows: list[dict] = []
        offset = 0
        while len(rows) < page_limit:
            body: dict[str, Any] = {
                "limit": QUERY_PAGE_LIMIT,
                "offset": offset,
                "team_id": team_id,
                "agent_id": agent_id,
                "user_id": user_id,
            }
            if time_start:
                body["time_start"] = time_start
            if task_id:
                body["task_id"] = task_id
            data = self._post("/v3/atomic/query", body, timeout=timeout)
            items = data.get("items")
            page = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
            rows.extend(page)
            offset += len(page)
            if not page:
                break
            total = data.get("total")
            if isinstance(total, int) and not isinstance(total, bool):
                if len(rows) >= min(total, page_limit):
                    break
            elif len(page) < QUERY_PAGE_LIMIT:
                break
        return rows

    def atomic_count(
        self, *, team_id: str, agent_id: str, user_id: str, task_id: str | None = None, timeout: float | None = None
    ) -> int:
        body: dict[str, Any] = {"team_id": team_id, "agent_id": agent_id, "user_id": user_id}
        if task_id:
            body["task_id"] = task_id
        data = self._post("/v3/atomic/count", body, timeout=timeout)
        total = data.get("total")
        return total if isinstance(total, int) else 0

    def atomic_update(
        self,
        memory_id: str,
        *,
        content: str,
        background: str | None = None,
        team_id: str,
        agent_id: str,
        user_id: str,
        timeout: float | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "id": memory_id,
            "content": content,
            "team_id": team_id,
            "agent_id": agent_id,
            "user_id": user_id,
        }
        if background is not None:
            body["background"] = background
        return self._post("/v3/atomic/update", body, timeout=timeout)

    def atomic_delete(
        self,
        ids: list[str],
        *,
        team_id: str,
        agent_id: str,
        user_id: str,
        timeout: float | None = None,
    ) -> int:
        data = self._post(
            "/v3/atomic/delete",
            {"ids": list(ids), "team_id": team_id, "agent_id": agent_id, "user_id": user_id},
            timeout=timeout,
        )
        deleted = data.get("deleted_count")
        return deleted if isinstance(deleted, int) else 0

    # ------------------------------------------------------------------
    # L2: scenario files / L3: persona (team+agent scope upstream)
    # ------------------------------------------------------------------
    def scenario_ls(self, *, team_id: str, agent_id: str, user_id: str, timeout: float | None = None) -> list[dict]:
        data = self._post("/v3/scenario/ls", {"team_id": team_id, "agent_id": agent_id, "user_id": user_id}, timeout=timeout)
        entries = data.get("entries")
        return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []

    def scenario_read(
        self, path: str, *, team_id: str, agent_id: str, user_id: str, timeout: float | None = None
    ) -> dict:
        # Missing file: HTTP 200 with content=null (upstream contract).
        return self._post(
            "/v3/scenario/read",
            {"path": path, "team_id": team_id, "agent_id": agent_id, "user_id": user_id},
            timeout=timeout,
        )

    def core_read(self, *, team_id: str, agent_id: str, user_id: str, timeout: float | None = None) -> dict:
        # Persona not generated yet: HTTP 200 with content=null.
        return self._post("/v3/core/read", {"team_id": team_id, "agent_id": agent_id, "user_id": user_id}, timeout=timeout)

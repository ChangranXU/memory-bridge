"""Minimal REST client for the mem0 Platform API (https://api.mem0.ai).

Implements exactly the operations the bridge needs — add (with async event
polling so writes are synchronous from the caller's view), search, get,
get_all, update, delete — over httpx, which the shared uv environment already
carries as a litellm dependency. The full ``mem0ai`` SDK is deliberately not
installed: its open-source stack (embedders, vector stores, db drivers) is
irrelevant to the hosted API and risks dependency conflicts with litellm in
the shared environment.

Verified against the live API: auth is ``Authorization: Token <key>``; adds go
to ``POST /v3/memories/add/`` (async unless ``infer=false``; results carry the
extracted text under ``data.memory`` and an ``event`` of ADD/UPDATE/DELETE/
NONE); searches go to ``POST /v3/memories/search/`` with the entity ids inside
``filters``; by-id read/update/delete are the v1 endpoints and answer 404 for
unknown ids; the by-id read echoes ``user_id`` (the endpoint adapter's
ownership check relies on it).
"""

import time

import httpx


class Mem0ApiError(RuntimeError):
    """Platform failure with the HTTP-style status and a safe reason."""

    def __init__(self, status_code: int, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def _error_reason(body, status: int) -> str:
    if isinstance(body, dict):
        for key in ("error", "detail", "message"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(body, list):
        parts = [item for item in body if isinstance(item, str)]
        if parts:
            return "; ".join(parts)
    return f"mem0 API returned HTTP {status}"


def _normalize_result(item: dict) -> dict:
    """Flatten one add result: v3 nests the text under ``data.memory``,
    the v1 quickstart shape carries it directly as ``memory``."""
    data = item.get("data")
    memory = data.get("memory") if isinstance(data, dict) else None
    if memory is None:
        memory = item.get("memory")
    return {"id": item.get("id"), "memory": memory, "event": item.get("event")}


def _results_of(container: dict) -> list[dict]:
    results = container.get("results")
    if not isinstance(results, list):
        return []
    return [_normalize_result(item) for item in results if isinstance(item, dict)]


def _shape_of(body) -> str:
    """A safe description of an unexpected response body for error reasons:
    a dict's sorted key names, anything else's type name — never the content
    itself (a drifted add body may carry user messages)."""
    return f"keys {sorted(body)}" if isinstance(body, dict) else type(body).__name__


def _request_json(client: httpx.Client, method: str, path: str, *, json=None, params=None, timeout: float | None = None):
    """One mem0 REST call's shared plumbing (the platform client and the OSS
    server store): issue the request, decode the body as JSON (None when
    absent or not JSON), and map an error status to Mem0ApiError.

    A per-call timeout overrides the client-wide default (httpx semantics —
    an explicit None would DISABLE the timeout, so it is never forwarded):
    the recall search uses it to bound one call by the configured
    search_timeout while everything else keeps the client-wide budget.
    """
    override = {} if timeout is None else {"timeout": timeout}
    response = client.request(method, path, json=json, params=params, **override)
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400:
        raise Mem0ApiError(response.status_code, _error_reason(body, response.status_code))
    return body


class Mem0PlatformClient:
    """Synchronous mem0 Platform client; one httpx connection pool."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.mem0.ai",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Token {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, *, json=None, params=None, timeout: float | None = None) -> dict:
        # The platform surface only ever consumes dict bodies; a non-dict
        # decode (e.g. an empty 200) coerces to {}.
        body = _request_json(self._client, method, path, json=json, params=params, timeout=timeout)
        return body if isinstance(body, dict) else {}

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------
    def ping(self) -> dict:
        """Key/endpoint validation; returns the org/project identity."""
        return self._request("GET", "/v1/ping/")

    def add(
        self,
        *,
        messages: list[dict],
        user_id: str,
        run_id: str | None = None,
        infer: bool = True,
        metadata: dict | None = None,
        custom_instructions: str | None = None,
        poll_budget: float = 60.0,
        poll_interval: float = 1.0,
    ) -> list[dict]:
        """Add messages and return once the platform finished processing them.

        Only ``user_id`` scopes the write (never ``agent_id``): the platform's
        attribution splitting would stamp assistant-message facts with
        ``agent_id`` instead, and a ``user_id``-filtered search would miss them.

        ``custom_instructions`` is the platform's advisory, per-request
        extraction-guidelines field (it overrides the project-level setting
        for this call only); the base extraction prompt stays platform-owned.
        Omitted when empty so callers without guidelines keep the exact
        request body they had before the field existed.
        """
        body: dict = {"messages": messages, "user_id": user_id, "infer": infer}
        if run_id:
            body["run_id"] = run_id
        if metadata:
            body["metadata"] = metadata
        if custom_instructions and custom_instructions.strip():
            body["custom_instructions"] = custom_instructions.strip()
        response = self._request("POST", "/v3/memories/add/", json=body)
        results = response.get("results")
        # A present results list is the sync answer (an empty one is a
        # legitimate no-op extraction); only when it is absent does the async
        # event_id path apply — an empty list WITH an event_id still polls,
        # because the queued event's receipts are then the authoritative ones.
        if isinstance(results, list) and (results or not response.get("event_id")):
            return [_normalize_result(item) for item in results if isinstance(item, dict)]
        event_id = response.get("event_id")
        if not event_id:
            raise Mem0ApiError(502, f"mem0 add returned neither results nor event_id: {sorted(response)}")
        return self._wait_for_event(event_id, poll_budget=poll_budget, poll_interval=poll_interval)

    def _wait_for_event(self, event_id: str, *, poll_budget: float, poll_interval: float) -> list[dict]:
        deadline = time.monotonic() + poll_budget
        while True:
            event = self._request("GET", f"/v1/event/{event_id}/")
            status = event.get("status")
            if status == "SUCCEEDED":
                # The receipts live in the payload's results list (some event
                # envelopes carry them top-level). An empty list is a
                # legitimate no-op extraction; a missing or non-list results is
                # drift — fail closed like a shapeless sync add, never coerce
                # to "stored nothing" (the backend clears its retained batch
                # on any non-raising add).
                for container in (event.get("payload"), event):
                    results = container.get("results") if isinstance(container, dict) else None
                    if isinstance(results, list):
                        return [_normalize_result(item) for item in results if isinstance(item, dict)]
                raise Mem0ApiError(
                    502, f"mem0 add event {event_id} succeeded with an unrecognizable payload: {_shape_of(event)}"
                )
            if status == "FAILED":
                raise Mem0ApiError(502, f"mem0 add event {event_id} failed: {event.get('error') or 'no reason given'}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Mem0ApiError(
                    504, f"mem0 add event {event_id} still {status or 'PENDING'} after {poll_budget:.0f}s"
                )
            time.sleep(min(poll_interval, remaining))

    def search(
        self,
        *,
        query: str,
        user_id: str,
        top_k: int = 10,
        threshold: float = 0.0,
        timeout: float | None = None,
    ) -> list[dict]:
        """The one native search both retrieval surfaces (the arm's recall and
        the standardized endpoint) share, so their semantics cannot drift.

        ``threshold`` is always sent explicitly: the platform's server-side
        default differs across API versions (0.3 on v2, 0.1 on the v3 surface
        this client calls), so omitting it silently applies a relevance cutoff
        whose value drifts; 0.0 disables the cutoff. Relevance doors belong to
        the caller (the shared host-side floor), never to an implicit default.
        """
        body: dict = {"query": query, "filters": {"user_id": user_id}, "top_k": top_k, "threshold": threshold}
        # The raw body, not _request's dict coercion: a shapeless 200 is
        # drift, not "no memories" — fail closed like a drifted add (the
        # recall path caches an empty answer as authoritative, so a drifted
        # envelope would blind recall until the next dirty tick with no
        # counter moving).
        response = _request_json(self._client, "POST", "/v3/memories/search/", json=body, timeout=timeout)
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise Mem0ApiError(502, f"mem0 platform search returned an unrecognizable response: {_shape_of(response)}")
        return [item for item in response["results"] if isinstance(item, dict)]

    def get_all(self, *, user_id: str, page_size: int = 100, page: int = 1) -> dict:
        """One page of the v3 get-all envelope. Pagination is the caller's
        loop: the envelope carries Django-style ``count``/``next``/``previous``
        (verified against the live API), and ``next`` null ends the walk."""
        return self._request(
            "POST",
            "/v3/memories/",
            json={"filters": {"user_id": user_id}},
            params={"page": page, "page_size": page_size},
        )

    def get(self, memory_id: str) -> dict:
        # The raw body, not _request's dict coercion: a drifted 200 must not
        # read as an empty row — the endpoint's ownership check would then
        # misreport drift as a plain 404.
        body = _request_json(self._client, "GET", f"/v1/memories/{memory_id}/")
        if not isinstance(body, dict):
            raise Mem0ApiError(502, f"mem0 platform get returned an unrecognizable response: {_shape_of(body)}")
        return body

    def update(self, memory_id: str, *, text: str | None = None, metadata: dict | None = None) -> dict:
        body = {key: value for key, value in (("text", text), ("metadata", metadata)) if value is not None}
        return self._request("PUT", f"/v1/memories/{memory_id}/", json=body)

    def delete(self, memory_id: str) -> dict:
        return self._request("DELETE", f"/v1/memories/{memory_id}/")

"""Server mode: a per-run self-hosted mem0 OSS server container as a Mem0Store.

Wire shape (verified against the vendored server's ``main.py`` — pin in
``integration/mem0/VENDORING.md``): NO ``/v1`` prefix and
``redirect_slashes=False`` (a trailing slash is a 404); adds are SYNCHRONOUS
(``POST /memories`` returns ``{"results": [...]}`` — no event polling);
search is ``POST /search`` with ``{query, filters, top_k, threshold}``;
scoped get-all is ``GET /memories?user_id=...&top_k=N`` (query params, capped
server-side at 1000); the readiness probe is ``GET /auth/setup-status`` (the
API server has no ``/health`` — that path is the dashboard's).

Auth: the arm runs the container with ``AUTH_DISABLED=true``, so NO auth
header is sent when ``server_api_key`` is empty. A presented credential still
fails loudly (``server/auth.py:verify_auth``): the store's ``X-API-Key`` goes
down the DB api-key path and 401s; only a Bearer token would reach the JWT
path and 500 without a configured ``JWT_SECRET``. Missing-id mapping: ``GET /memories/{id}`` answers 200
``null`` for unknown ids (PUT/DELETE already 404 via the server's ValueError
handler) — this store maps the null to the protocol's 404 convention.
"""

import httpx

from mem0_bridge.client import Mem0ApiError, _request_json, _results_of, _shape_of
from mem0_bridge.stores import SERVER_LISTING_CAP, Receipt


class ServerStore:
    def __init__(
        self,
        *,
        server_url: str,
        server_api_key: str = "",
        timeout: float = 30.0,
        add_timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ):
        # No auth header at all without a key (see the module docstring).
        headers = {"X-API-Key": server_api_key} if server_api_key else {}
        # add_timeout >> timeout: an infer=true add is one extraction LLM
        # round-trip plus embedder calls INSIDE the request — far past the 30 s
        # client default for reasoning-style models (the add would ReadTimeout
        # while the server keeps working, then the retained-batch retry re-pays
        # the LLM call).
        self._add_timeout = add_timeout
        self._client = httpx.Client(
            base_url=server_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def _request(self, method: str, path: str, *, json=None, params=None, timeout: float | None = None):
        # The raw body passes through untouched: GET /memories/{id} answering
        # 200 null for an unknown id depends on the None surviving.
        return _request_json(self._client, method, path, json=json, params=params, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------
    def health(self) -> dict:
        body = self._request("GET", "/auth/setup-status")
        # A drifted 200 fails closed like every other op: the server image is
        # pinned by tag, so a shape change means the pin broke — better a loud
        # startup failure than a run against a drifted server.
        if not isinstance(body, dict):
            raise Mem0ApiError(502, f"mem0 server health returned an unrecognizable response: {_shape_of(body)}")
        return body

    def add(
        self,
        *,
        messages: list[dict],
        user_id: str,
        run_id: str | None = None,
        infer: bool = True,
        metadata: dict | None = None,
        guidelines: str | None = None,
    ) -> list[Receipt]:
        # ``prompt`` is the OSS per-call extraction-guidelines channel (it lands
        # in the same advisory slot as the platform's custom_instructions).
        body: dict = {"messages": messages, "user_id": user_id, "infer": infer}
        if run_id:
            body["run_id"] = run_id
        if metadata:
            body["metadata"] = metadata
        if guidelines and guidelines.strip():
            body["prompt"] = guidelines.strip()
        response = self._request("POST", "/memories", json=body, timeout=self._add_timeout)
        # Fail closed on a shapeless 200: coercing one to [] would report
        # "success, zero memories" and the backend would clear the retained
        # batch — silent message loss (the platform client raises for the
        # same drift; an empty results LIST stays a legitimate no-op add).
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise Mem0ApiError(502, f"mem0 server add returned an unrecognizable response: {_shape_of(response)}")
        return _results_of(response)

    def search(
        self,
        *,
        query: str,
        user_id: str,
        top_k: int,
        threshold: float,
        timeout: float | None = None,
    ) -> list[dict]:
        body: dict = {"query": query, "filters": {"user_id": user_id}, "top_k": top_k, "threshold": threshold}
        response = self._request("POST", "/search", json=body, timeout=timeout)
        # Fail closed on a shapeless 200, same as add: coercing drift to []
        # reads as "no memories", and the recall path CACHES an empty answer
        # as authoritative — silent blindness until the next dirty tick.
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise Mem0ApiError(502, f"mem0 server search returned an unrecognizable response: {_shape_of(response)}")
        return [item for item in response["results"] if isinstance(item, dict)]

    def get(self, memory_id: str) -> dict:
        body = self._request("GET", f"/memories/{memory_id}")
        if body is None:
            # The server's GET answers 200 null for unknown ids — map it to the
            # protocol's missing-id convention (PUT/DELETE already 404).
            raise Mem0ApiError(404, f"memory {memory_id} not found")
        if not isinstance(body, dict):
            # Drift must not masquerade as an empty row: the endpoint's
            # ownership check would misreport it as a plain 404.
            raise Mem0ApiError(502, f"mem0 server get returned an unrecognizable response: {_shape_of(body)}")
        return body

    def get_all(self, *, user_id: str, limit: int) -> list[dict]:
        # Query-param scoped get-all; the entity filter is hard-required by the
        # engine and the server caps top_k at its hard listing ceiling — clamp
        # explicitly.
        response = self._request(
            "GET", "/memories", params={"user_id": user_id, "top_k": max(1, min(limit, SERVER_LISTING_CAP))}
        )
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list):
            # Fail closed like add/search: a drifted envelope coerced to []
            # would silently truncate the final dump with no counter moving.
            raise Mem0ApiError(502, f"mem0 server get-all returned an unrecognizable response: {_shape_of(response)}")
        return [item for item in results if isinstance(item, dict)][:limit]

    def update(self, memory_id: str, *, text: str | None = None, metadata: dict | None = None) -> dict:
        body = {key: value for key, value in (("text", text), ("metadata", metadata)) if value is not None}
        self._request("PUT", f"/memories/{memory_id}", json=body)
        # The OSS PUT answers a bare {"message": ...} — the echo the contract
        # needs is a follow-up read.
        return self.get(memory_id)

    def delete(self, memory_id: str) -> dict:
        body = self._request("DELETE", f"/memories/{memory_id}")
        return body if isinstance(body, dict) else {}

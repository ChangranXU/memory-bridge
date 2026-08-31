"""Standardized-endpoint adapter over a mem0 store (any mode).

Maps the shared ``MemoryEndpoint`` contract onto the ``Mem0Store`` protocol.
Writes are synchronous (every mode's add returns only after persistence), and
``user_id`` is the sole retrieval-isolation boundary: adds are scoped with
exactly one ``user_id`` and searches filter on it. mem0 memory ids are
store-global, so update/delete verify ownership with a read first — a memory
stored under a different ``user_id`` answers 404, exactly like an unknown id.

The search threshold/timeout ride the constructor (resolved from the same
config the backend reads), never a store-side default: parity between the two
retrieval surfaces is a constructor contract, pinned in the tests. The
extraction guidelines ride the constructor the same way, so the endpoint's
infer-adds extract under the arm's policy instead of the engine/project
default.
"""

from mem0_bridge.client import Mem0ApiError
from mem0_bridge.stores import Mem0Store
from shared_bridge.annotate import normalize_score
from shared_bridge.endpoint import (
    AddRequest,
    AddResponse,
    DeleteResponse,
    MemoryEndpoint,
    MemoryEndpointError,
    MemoryRecord,
    SearchRequest,
    SearchResponse,
    UpdateRequest,
    UpdateResponse,
)


class Mem0Endpoint(MemoryEndpoint):
    def __init__(
        self,
        store: Mem0Store,
        default_user_id: str = "minisweagent",
        *,
        search_threshold: float = 0.0,
        search_timeout: float | None = None,
        extraction_guidelines: str | None = None,
    ):
        self._store = store
        self._default_user_id = default_user_id
        self._search_threshold = search_threshold
        self._search_timeout = search_timeout
        self._extraction_guidelines = extraction_guidelines

    def add(self, request: AddRequest) -> AddResponse:
        try:
            results = self._store.add(
                messages=[{"role": m.role, "content": m.content} for m in request.messages],
                user_id=request.user_id,
                run_id=request.session_id,
                infer=request.infer,
                metadata=request.metadata,
                guidelines=self._extraction_guidelines,
            )
        except Mem0ApiError as e:
            raise MemoryEndpointError(500, f"mem0 add failed: {e.reason}") from e
        return AddResponse(
            success=True,
            request_id=request.request_id,
            user_id=request.user_id,
            session_id=request.session_id,
            memory_ids=[item["id"] for item in results if item.get("id")],
        )

    def search(self, request: SearchRequest) -> SearchResponse:
        try:
            hits = self._store.search(
                query=request.query,
                user_id=request.user_id,
                top_k=request.top_k,
                threshold=self._search_threshold,
                timeout=self._search_timeout,
            )
        except Mem0ApiError as e:
            raise MemoryEndpointError(500, f"mem0 search failed: {e.reason}") from e
        data = [
            self._record(hit)
            for hit in hits
            if hit.get("id") and isinstance(hit.get("memory"), str) and hit["memory"].strip()
        ]
        return SearchResponse(data=data[: request.top_k])

    def update(self, memory_id: str, request: UpdateRequest, *, user_id: str | None = None) -> UpdateResponse:
        if request.text is None and request.metadata is None:
            raise MemoryEndpointError(400, "update requires text or metadata")
        try:
            self._scoped(memory_id, user_id)
            memory = self._store.update(memory_id, text=request.text, metadata=request.metadata)
        except Mem0ApiError as e:
            raise MemoryEndpointError(self._status(e), f"mem0 update failed: {e.reason}") from e
        if not memory.get("id") or not isinstance(memory.get("memory"), str) or not memory["memory"]:
            # The documented response echoes the updated memory; a shapeless
            # one is an integration failure with a clear reason, never a raw
            # KeyError escaping past the Mem0ApiError guard.
            raise MemoryEndpointError(500, "mem0 update returned an unusable response")
        return UpdateResponse(success=True, memory=self._record(memory))

    def delete(self, memory_id: str, *, user_id: str | None = None) -> DeleteResponse:
        try:
            self._scoped(memory_id, user_id)
            self._store.delete(memory_id)
        except Mem0ApiError as e:
            raise MemoryEndpointError(self._status(e), f"mem0 delete failed: {e.reason}") from e
        return DeleteResponse(success=True, memory_id=memory_id)

    # ------------------------------------------------------------------
    def _scoped(self, memory_id: str, user_id: str | None) -> dict:
        """Read the memory first so a foreign user_id looks exactly like an
        unknown id (mem0 ids are store-global; 404 upholds isolation)."""
        user_id = user_id or self._default_user_id
        memory = self._store.get(memory_id)
        # A stored user_id of None means the memory was written outside this
        # contract (adds here always carry exactly one): foreign like any
        # other user_id, so fail closed instead of skipping the check.
        if memory.get("user_id") != user_id:
            raise MemoryEndpointError(404, f"memory {memory_id} not found for user {user_id}")
        return memory

    @staticmethod
    def _status(error: Mem0ApiError) -> int:
        # The store layer deliberately preserves a 400 (the engine rejecting
        # the request itself, e.g. library mode's "no text content to
        # update"): pass it through as the contract's caller bug instead of
        # collapsing it into an integration-failure 500.
        return error.status_code if error.status_code in (400, 404) else 500

    @staticmethod
    def _record(hit: dict) -> MemoryRecord:
        return MemoryRecord(
            id=str(hit["id"]),
            content=hit["memory"],
            # An unusable score is dropped, never fatal to the whole search.
            score=normalize_score(hit.get("score")),
            user_id=hit.get("user_id"),
            metadata=hit.get("metadata"),
            created_at=hit.get("created_at"),
            updated_at=hit.get("updated_at"),
        )

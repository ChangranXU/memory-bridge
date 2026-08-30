"""CURE implementation of the standardized memory endpoint contract.

Maps the shared add/search/update/delete actions onto a ``CUREMemorySystem``:

- ``add`` binds (or reuses) the session for ``(user_id, session_id)``, records
  the messages, and — with ``infer=true`` — runs CURE's extraction pipeline;
  ``infer=false`` stores each message verbatim as its own approved memory row.
  The response returns only after the rows are persisted and searchable.
  A session-less add (``session_id`` omitted) always mints a fresh session and
  the response carries the MINTED id, not the request's — the one deliberate
  deviation from the contract's byte-for-byte echo rule.
- ``search`` runs CURE's approved-memory search inside the exact ``user_id``
  scope (the sole retrieval-isolation boundary).
- ``update`` replaces a row's value via CURE's supersede-by-replace rule;
  a metadata-only update is a 400 (CURE rows carry no arbitrary metadata).
- ``delete`` marks the row ``deleted``, addressed by id.

CURE memory ids are integer row ids, so endpoint-side ``memory_id`` strings
must parse as integers; anything else is a 400, and a missing or terminal
(``deleted``/``superseded``/…) row a 404 — terminal rows are never
re-matched, so one logical deletion counts once and history markers survive.
"""

import logging

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

from cure_memory.models import INACTIVE_REVIEW_STATUSES, Memory
from cure_memory.system import CUREMemorySystem

logger = logging.getLogger("cure_memory_bridge.endpoint")


class CureMemoryEndpoint(MemoryEndpoint):
    def __init__(self, system: CUREMemorySystem, default_user_id: str = "minisweagent"):
        self._system = system
        self._default_user_id = default_user_id

    def add(self, request: AddRequest) -> AddResponse:
        system = self._system
        if not (system.current_user_id == request.user_id and system.current_session_id == request.session_id):
            system.start_session(request.user_id, session_id=request.session_id)
        session_id = system.current_session_id
        memory_ids: list[str] = []
        if request.infer:
            for message in request.messages:
                metadata = dict(request.metadata or {})
                if message.timestamp is not None:
                    metadata["timestamp"] = message.timestamp
                system.record_message(message.role, message.content, metadata=metadata)
            result = system.extract_runtime_memories()
            if result.errors:
                raise MemoryEndpointError(500, f"extraction failed: {'; '.join(result.errors)}")
            memory_ids = [str(memory.id) for memory in result.candidates if memory.id is not None]
        else:
            for index, message in enumerate(request.messages):
                memory = system.memory_add(
                    request.user_id,
                    memory_type="fact",
                    key=f"{session_id}:{request.request_id}:{index}",
                    value=message.content,
                    source_type="explicit_user",
                )
                if memory.id is not None:
                    memory_ids.append(str(memory.id))
        return AddResponse(
            success=True,
            request_id=request.request_id,
            user_id=request.user_id,
            session_id=session_id,
            memory_ids=memory_ids,
        )

    def search(self, request: SearchRequest) -> SearchResponse:
        memories = self._system.memory_search(request.user_id, query=request.query)
        return SearchResponse(data=[self._record(memory) for memory in memories[: request.top_k]])

    def update(self, memory_id: str, request: UpdateRequest, *, user_id: str | None = None) -> UpdateResponse:
        if request.text is None:
            raise MemoryEndpointError(400, "text is required: this integration stores no standalone metadata")
        row_id = self._parse_id(memory_id)
        try:
            memory = self._system.memory_replace(user_id or self._default_user_id, row_id, request.text)
        except ValueError as e:
            raise MemoryEndpointError(404, f"memory not found: {memory_id}") from e
        return UpdateResponse(success=True, memory=self._record(memory))

    def delete(self, memory_id: str, *, user_id: str | None = None) -> DeleteResponse:
        row_id = self._parse_id(memory_id)
        store = self._system.store
        row = next(
            (m for m in store.list_memories(user_id or self._default_user_id, review_status=None) if m.id == row_id),
            None,
        )
        if row is None or row.review_status in INACTIVE_REVIEW_STATUSES:
            # Terminal rows (deleted/superseded/...) are never re-matched:
            # one logical deletion counts once and history markers survive.
            raise MemoryEndpointError(404, f"memory not found: {memory_id}")
        row.review_status = "deleted"
        store.update_memory(row)
        return DeleteResponse(success=True, memory_id=memory_id)

    @staticmethod
    def _parse_id(memory_id: str) -> int:
        try:
            return int(memory_id)
        except (TypeError, ValueError) as e:
            raise MemoryEndpointError(400, f"memory_id must be an integer row id: {memory_id!r}") from e

    @staticmethod
    def _record(memory: Memory) -> MemoryRecord:
        return MemoryRecord(
            id=str(memory.id),
            content=f"{memory.key}: {memory.value}",
            user_id=memory.user_id,
            metadata={
                "memory_type": memory.memory_type,
                "confidence": memory.confidence,
                "review_status": memory.review_status,
            },
            created_at=memory.created_at,
            updated_at=memory.updated_at,
        )

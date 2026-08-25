"""Standardized memory endpoint contract: add / search / update / delete.

One wire contract for memory actions, shared by every memory integration.
The shapes reconcile two public contracts:

- the Agent Memory Leaderboard synchronous Add/Search contract
  (https://agentmemories.ai/api-guide): Add returns success only after the
  write is persisted and immediately searchable, and echoes ``request_id`` /
  ``user_id`` / ``session_id`` byte-for-byte; ``user_id`` is the sole
  retrieval-isolation field; Search returns ``{"data": [{id, content,
  score?, created_at?}, ...]}`` in relevance order, capped at ``top_k``.
- a hosted memory platform's v1 memory CRUD API (its published
  ``docs/openapi.json``): ``POST /v1/memories/`` (add),
  ``POST /v1/memories/search/`` (search, ``query`` required, ``top_k``
  default 10), ``PUT /v1/memories/{id}`` with ``{text, metadata}`` (update),
  ``DELETE /v1/memories/{id}`` (delete).

Rules every implementation must follow:

1. Writes are synchronous: success is returned only after persistence.
2. ``user_id`` is the sole retrieval-isolation boundary: search must only
   ever return records stored under the exact same ``user_id``.
3. Search results carry at least ``id`` and ``content``; undeclared extra
   fields are ignored by callers.
4. Unknown memory ids raise ``MemoryEndpointError(status_code=404)``;
   contract violations raise ``MemoryEndpointError(status_code=400)``.
"""

from abc import ABC, abstractmethod
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    """One conversation turn to ingest; ``timestamp`` is Unix milliseconds."""

    model_config = ConfigDict(extra="ignore")

    role: str = Field(min_length=1)
    content: str = Field(min_length=1)
    timestamp: int | None = None


class AddRequest(BaseModel):
    """Ingest one (chunk of a) source session. ``infer=false`` stores the
    messages verbatim; ``true`` lets the integration extract memories first."""

    model_config = ConfigDict(extra="ignore")

    request_id: str = Field(default_factory=lambda: uuid4().hex)
    messages: list[Message] = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str | None = None
    infer: bool = True
    metadata: dict[str, Any] | None = None


class AddResponse(BaseModel):
    """Returned only after the write is persisted and immediately searchable;
    the echoed ids match the request byte-for-byte."""

    success: bool
    request_id: str
    user_id: str
    session_id: str | None = None
    memory_ids: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    """One retrieval query against exactly one ``user_id`` scope. ``options``
    carries answer choices for choice questions and is context only."""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1, le=1000)
    options: list[str] | None = None


class MemoryRecord(BaseModel):
    """One memory row on the wire (the platform API calls the text field
    ``memory``; the Leaderboard contract calls it ``content`` — standardized
    here)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    score: float | None = None
    user_id: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str | None = None  # ISO 8601
    updated_at: str | None = None


class SearchResponse(BaseModel):
    data: list[MemoryRecord] = Field(default_factory=list)


class UpdateRequest(BaseModel):
    """``PUT /v1/memories/{id}`` update: replace the memory text and/or metadata."""

    model_config = ConfigDict(extra="ignore")

    text: str | None = Field(default=None, min_length=1)
    metadata: dict[str, Any] | None = None


class UpdateResponse(BaseModel):
    success: bool
    memory: MemoryRecord


class DeleteResponse(BaseModel):
    success: bool
    memory_id: str


class MemoryEndpointError(RuntimeError):
    """Endpoint failure with an HTTP-style status code and a safe reason."""

    def __init__(self, status_code: int, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


class MemoryEndpoint(ABC):
    """The standardized memory actions every integration implements.

    ``user_id`` on update/delete scopes the lookup for integrations whose
    stores are user-partitioned; when omitted, an integration default applies.
    """

    @abstractmethod
    def add(self, request: AddRequest) -> AddResponse:
        """Persist ``request.messages`` (synchronously) and echo the ids."""

    @abstractmethod
    def search(self, request: SearchRequest) -> SearchResponse:
        """Return at most ``request.top_k`` records for ``request.user_id``,
        in relevance order (empty list when nothing matches)."""

    @abstractmethod
    def update(self, memory_id: str, request: UpdateRequest, *, user_id: str | None = None) -> UpdateResponse:
        """Replace the text (and/or metadata) of one memory; 404 if unknown."""

    @abstractmethod
    def delete(self, memory_id: str, *, user_id: str | None = None) -> DeleteResponse:
        """Delete one memory; 404 if unknown."""

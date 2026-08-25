"""Shared memory-bridge components: agent hooks, annotation transport, and the
standardized memory endpoint contract (add / search / update / delete)."""

from shared_bridge.config import MemoryConfig
from shared_bridge.endpoint import (
    AddRequest,
    AddResponse,
    DeleteResponse,
    MemoryEndpoint,
    MemoryEndpointError,
    MemoryRecord,
    Message,
    SearchRequest,
    SearchResponse,
    UpdateRequest,
    UpdateResponse,
)

__all__ = [
    "AddRequest",
    "AddResponse",
    "DeleteResponse",
    "MemoryAgent",
    "MemoryAgentConfig",
    "MemoryConfig",
    "MemoryEndpoint",
    "MemoryEndpointError",
    "MemoryRecord",
    "Message",
    "SearchRequest",
    "SearchResponse",
    "UpdateRequest",
    "UpdateResponse",
]


def __getattr__(name: str):
    """Lazy agent import: ``shared_bridge.agent`` needs minisweagent, which
    this package deliberately does not depend on (it is provided by the
    environment that runs the agent) — the endpoint contract must stay
    importable without it."""
    if name in ("MemoryAgent", "MemoryAgentConfig"):
        from shared_bridge.agent import MemoryAgent, MemoryAgentConfig

        return {"MemoryAgent": MemoryAgent, "MemoryAgentConfig": MemoryAgentConfig}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

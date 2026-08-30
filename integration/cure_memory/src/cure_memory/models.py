"""
Core product data models for CURE Memory.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# Review statuses that end a row's lifecycle: the upsert's active-row match
# and the deletion match both ignore them (re-matching a terminal row would
# inflate counters and overwrite the row's lifecycle marker for nothing).
INACTIVE_REVIEW_STATUSES = frozenset({"deleted", "rejected", "archived", "superseded"})


@dataclass
class SessionMessage:
    id: Optional[int] = None
    session_id: str = ""
    user_id: str = ""
    role: str = ""
    content: str = ""
    created_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


@dataclass
class Memory:
    id: Optional[int] = None
    user_id: str = ""
    project_id: Optional[str] = None
    scope: str = "user"
    memory_type: str = "fact"
    key: str = ""
    value: str = ""
    description: str = ""
    confidence: float = 0.0
    review_status: str = "candidate"
    source_type: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    sensitivity: str = "private"
    needs_verification: bool = False
    supersedes: List[int] = field(default_factory=list)
    superseded_by: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


@dataclass
class Rejection:
    reason: str
    snippet: str
    source: Dict[str, Any]


@dataclass
class ExtractionResult:
    candidates: List[Memory] = field(default_factory=list)
    approved: List[Memory] = field(default_factory=list)
    pending_review: List[Memory] = field(default_factory=list)
    rejected: List[Rejection] = field(default_factory=list)
    deleted: List[Memory] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    # The upsert's effective row per candidate, populated by the system's
    # write path (extract_runtime_memories): the newly saved row, or the
    # EXISTING row on the identical-content no-op — so a caller can report the
    # persisted id even when the candidate itself was never written. Stays
    # empty when the extraction errored (checkpoint held, nothing upserted).
    # The candidates list itself keeps the decision's own objects: the trace
    # audit keys on their id=None no-op shape.
    persisted: List[Memory] = field(default_factory=list)

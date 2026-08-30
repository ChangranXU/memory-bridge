"""
Standalone runtime API for the CURE Memory product.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional
from uuid import uuid4

from .extractor import BasicMemoryExtractor
from .models import INACTIVE_REVIEW_STATUSES, ExtractionResult, Memory, SessionMessage
from .store import SQLiteMemoryStore


class CUREMemorySystem:
    """
    Product-facing memory runtime.

    This class is independent of the legacy V1/V2 harness experiments. It owns
    session logging, memory extraction, and the memory store lifecycle.
    """

    def __init__(
        self,
        db_path: str,
        llm_client: Optional[Any] = None,
        policy_guidelines: Optional[str] = None,
    ):
        self.store = SQLiteMemoryStore(db_path)
        self.extractor = BasicMemoryExtractor(llm_client=llm_client, policy_guidelines=policy_guidelines)
        self.current_session_id: Optional[str] = None
        self.current_user_id: Optional[str] = None
        self.current_project_id: Optional[str] = None
        # Keyed by (user_id, session_id), not the session id alone: the
        # endpoint surface lets two users reuse one session id, and a shared
        # checkpoint would let one user's extraction skip (starve) the other's
        # un-extracted messages.
        self._last_extracted_message_id_by_session = {}

    def start_session(
        self,
        user_id: str,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        self.current_user_id = user_id
        self.current_project_id = project_id
        self.current_session_id = session_id or f"session_{uuid4().hex}"
        self._last_extracted_message_id_by_session.setdefault(
            (user_id, self.current_session_id),
            0,
        )
        return self.current_session_id

    def record_message(self, role: str, content: str, metadata: Optional[dict] = None) -> SessionMessage:
        self._require_session()
        message = SessionMessage(
            session_id=self.current_session_id,
            user_id=self.current_user_id,
            role=role,
            content=content,
            metadata=metadata or {},
        )
        self.store.save_message(message)
        return message

    def extract_runtime_memories(self) -> ExtractionResult:
        self._require_session()
        checkpoint_key = (self.current_user_id, self.current_session_id)
        after_id = self._last_extracted_message_id_by_session.get(checkpoint_key, 0)
        # user_id scopes the read (the isolation boundary): under a shared
        # session id, one user's extraction never ingests another's messages.
        messages = self.store.list_messages(self.current_session_id, self.current_user_id, after_id=after_id)
        existing = self.store.list_memories(
            user_id=self.current_user_id,
            project_id=self.current_project_id,
            review_status=None,
        )
        result = self.extractor.extract(
            messages,
            existing=existing,
            project_id=self.current_project_id,
        )
        if result.errors:
            return result

        # One atomic unit for the deletion batch (the same crash discipline
        # the supersede sequences have): a crash mid-batch must not persist
        # half the deletions while the checkpoint below holds for the retry.
        with self.store.atomic():
            for memory in result.deleted:
                memory.review_status = "deleted"
                self.store.update_memory(memory)

        for memory in result.candidates:
            result.persisted.append(self._upsert_memory(memory))

        if messages:
            self._last_extracted_message_id_by_session[checkpoint_key] = max(
                message.id for message in messages if message.id is not None
            )
        return result

    def has_unextracted_messages(self) -> bool:
        """Whether the active session holds messages past the extraction
        checkpoint — a tick with none has nothing to decide and is not a call."""
        self._require_session()
        checkpoint_key = (self.current_user_id, self.current_session_id)
        after_id = self._last_extracted_message_id_by_session.get(checkpoint_key, 0)
        return self.store.has_messages(self.current_session_id, self.current_user_id, after_id=after_id)

    def memory_add(
        self,
        user_id: str,
        memory_type: str,
        key: str,
        value: str,
        confidence: float = 0.95,
        source_type: str = "manual",
        project_id: Optional[str] = None,
        review_status: str = "approved",
    ) -> Memory:
        memory = Memory(
            user_id=user_id,
            project_id=project_id,
            scope="user" if project_id is None else "project",
            memory_type=memory_type,
            key=key,
            value=value,
            description=f"{memory_type}:{key}",
            confidence=confidence,
            review_status=review_status,
            source_type=source_type,
            sources=[{"source_type": source_type, "timestamp": datetime.now().isoformat()}],
            evidence=[value],
        )
        # The upsert returns the effective row: the newly saved memory, or the
        # existing row when the add dedupes (so an idempotent retry still
        # reports the persisted row's id).
        return self._upsert_memory(memory)

    def memory_replace(
        self,
        user_id: str,
        memory_id: int,
        value: str,
        confidence: float = 0.95,
    ) -> Memory:
        all_memories = self.store.list_memories(user_id=user_id, review_status=None)
        # Terminal rows are never re-matched: replacing a deleted (or already
        # superseded) row would resurrect its content as a fresh approved row
        # and overwrite the old row's history marker, so they read as missing.
        old = next(
            (
                memory
                for memory in all_memories
                if memory.id == memory_id and memory.review_status not in INACTIVE_REVIEW_STATUSES
            ),
            None,
        )
        if old is None:
            raise ValueError(f"Memory not found: {memory_id}")
        replacement = Memory(
            user_id=old.user_id,
            project_id=old.project_id,
            scope=old.scope,
            memory_type=old.memory_type,
            key=old.key,
            value=value,
            description=old.description,
            confidence=confidence,
            review_status="approved",
            source_type="manual_replace",
            sources=old.sources,
            evidence=[value],
            supersedes=[old.id],
        )
        old.review_status = "superseded"
        # One atomic unit: a crash between the two writes must not persist the
        # replacement while the old row stays approved (two live rows, one key).
        with self.store.atomic():
            self.store.save_memory(replacement)
            old.superseded_by = replacement.id
            self.store.update_memory(old)
        return replacement

    def memory_search(
        self,
        user_id: str,
        query: Optional[str] = None,
        project_id: Optional[str] = None,
        review_status: Optional[str] = "approved",
    ) -> List[Memory]:
        memories = self.store.list_memories(
            user_id=user_id,
            project_id=project_id,
            review_status=review_status,
        )
        if not query:
            return memories
        terms = [term for term in query.lower().split() if term]
        scored = []
        for memory in memories:
            text = f"{memory.memory_type} {memory.key} {memory.value}".lower()
            score = sum(1 for term in terms if term in text)
            if score > 0:
                scored.append((score, memory))
        scored.sort(key=lambda item: item[0], reverse=True)
        results = []
        for score, memory in scored:
            # A transient search-time annotation (the bridge's relevance floor
            # reads it): list_memories deserializes fresh rows on every call
            # and the recall path never re-persists them, so the score never
            # lands in the store.
            memory.metadata["score"] = score
            results.append(memory)
        return results

    def close(self) -> None:
        self.store.close()

    def _upsert_memory(self, memory: Memory) -> Memory:
        existing = self.store.list_memories(
            user_id=memory.user_id,
            project_id=memory.project_id,
            review_status=None,
            memory_type=memory.memory_type,
            key=memory.key,
        )
        active = [item for item in existing if item.review_status not in INACTIVE_REVIEW_STATUSES]
        if memory.project_id is None:
            # Layer guard, post-query by design (list_memories' SQL also backs
            # memory_search's visibility lattice): a general candidate never
            # supersedes a repo-bound row — the more specific memory must not
            # be destroyed by the more abstract one.
            active = [item for item in active if item.project_id is None]
        if active and active[0].value == memory.value and active[0].review_status == memory.review_status:
            # The identical-content no-op fires only when the matching row
            # covers the candidate's whole lattice: a repo-bound candidate
            # no-ops against an identical GENERAL row (visible to every
            # repository, its own included — a duplicate repo-bound copy
            # would only double the recall line). The mirror direction never
            # reaches here: the layer guard above already stripped repo-bound
            # rows for a general candidate, and an identical repo-bound row
            # must not absorb it — that row is invisible to the other
            # repositories the general lesson is meant for, so the general
            # row is stored (within that one repo both then render).
            return active[0]
        if memory.project_id is not None:
            # The mirror guard: a repo-bound candidate supersedes only its own
            # repository's rows. The general (NULL) rows are shared by every
            # repository of the run — one repo's refinement must not destroy
            # them run-wide; the new repo-bound row coexists with the general
            # one and overlays it in that repo's recall.
            active = [item for item in active if item.project_id == memory.project_id]
        # One atomic unit per candidate: a crash mid-sequence must not persist
        # half the supersede — old rows terminal with no successor saved, or
        # the successor live with the old rows still approved.
        with self.store.atomic():
            for item in active:
                item.review_status = "superseded"
                self.store.update_memory(item)
                if item.id is not None:
                    memory.supersedes.append(item.id)
            self.store.save_memory(memory)
            for item in active:
                item.superseded_by = memory.id
                self.store.update_memory(item)
        return memory

    def _require_session(self) -> None:
        if not self.current_session_id or not self.current_user_id:
            raise ValueError("No active CURE memory session")

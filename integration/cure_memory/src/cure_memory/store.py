"""
SQLite persistence for the CURE Memory product.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import sqlite3
from typing import List, Optional

from .models import Memory, SessionMessage


class SQLiteMemoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._atomic_depth = 0
        self._create_tables()

    def _create_tables(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                project_id TEXT,
                scope TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                description TEXT,
                confidence REAL NOT NULL,
                review_status TEXT NOT NULL,
                source_type TEXT,
                sources TEXT,
                evidence TEXT,
                sensitivity TEXT,
                needs_verification INTEGER DEFAULT 0,
                supersedes TEXT,
                superseded_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_lookup
            ON memories(user_id, project_id, review_status, memory_type, key)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_session
            ON session_messages(session_id, id)
        """)
        self.conn.commit()

    def _commit(self) -> None:
        # Inside an atomic() block the per-write commit is deferred to the
        # block's single commit, so a multi-write sequence stays all-or-nothing.
        if self._atomic_depth == 0:
            self.conn.commit()

    def _write(self, sql: str, params: tuple) -> sqlite3.Cursor:
        """One DML statement under the store's commit discipline.

        A failed execute outside atomic() leaves sqlite's implicit transaction
        open, and the next atomic()'s explicit BEGIN then fails with "cannot
        start a transaction within a transaction" — every later extraction
        erroring with the wrong root cause until some write commits. Roll the
        implicit transaction back at the point of failure instead. Inside
        atomic() the block's own except performs the rollback.
        """
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql, params)
        except BaseException:
            if self._atomic_depth == 0:
                self.conn.rollback()
            raise
        self._commit()
        return cursor

    @contextmanager
    def atomic(self):
        """One all-or-nothing unit over multiple save/update calls.

        The multi-write paths (system.py ``memory_replace`` /
        ``_upsert_memory`` supersede sequences, the extraction deletion
        batch) would otherwise issue one commit per row; a crash between
        them would persist half the unit — the replacement live while the
        old row stays approved (two live rows for one key), the old row
        superseded with no successor saved, or half a deletion batch with
        the extraction checkpoint held for the retry. Inside this block the
        per-call commits become no-ops and the unit commits once at the end,
        rolling back on any error. Nested blocks join the outer one (no
        current caller nests).
        """
        if self._atomic_depth > 0:
            yield
            return
        self.conn.execute("BEGIN")
        self._atomic_depth += 1
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()
        finally:
            self._atomic_depth -= 1

    def save_message(self, message: SessionMessage) -> int:
        cursor = self._write("""
            INSERT INTO session_messages (
                session_id, user_id, role, content, created_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            message.session_id,
            message.user_id,
            message.role,
            message.content,
            message.created_at,
            json.dumps(message.metadata),
        ))
        message.id = cursor.lastrowid
        return message.id

    def has_messages(self, session_id: str, user_id: str, after_id: int = 0) -> bool:
        """Cheap existence probe (LIMIT 1) for the extraction readiness guard —
        no row materialization, unlike ``list_messages``. The user filter is
        the isolation boundary: one user's probe must never see (or be starved
        by) another user's messages under a shared session id."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT 1 FROM session_messages WHERE session_id = ? AND user_id = ? AND id > ? LIMIT 1",
            (session_id, user_id, after_id),
        )
        return cursor.fetchone() is not None

    def list_messages(self, session_id: str, user_id: str, after_id: int = 0) -> List[SessionMessage]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM session_messages
            WHERE session_id = ? AND user_id = ? AND id > ?
            ORDER BY id ASC
        """, (session_id, user_id, after_id))
        return [self._row_to_message(row) for row in cursor.fetchall()]

    def save_memory(self, memory: Memory) -> int:
        cursor = self._write("""
            INSERT INTO memories (
                user_id, project_id, scope, memory_type, key, value, description,
                confidence, review_status, source_type, sources, evidence,
                sensitivity, needs_verification, supersedes, superseded_by,
                created_at, updated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, self._memory_values(memory))
        memory.id = cursor.lastrowid
        return memory.id

    def update_memory(self, memory: Memory) -> None:
        if memory.id is None:
            raise ValueError("Cannot update memory without id")
        memory.updated_at = datetime.now().isoformat()
        self._write("""
            UPDATE memories
            SET user_id = ?, project_id = ?, scope = ?, memory_type = ?,
                key = ?, value = ?, description = ?, confidence = ?,
                review_status = ?, source_type = ?, sources = ?, evidence = ?,
                sensitivity = ?, needs_verification = ?, supersedes = ?,
                superseded_by = ?, created_at = ?, updated_at = ?,
                metadata = ?
            WHERE id = ?
        """, (*self._memory_values(memory), memory.id))

    def list_memories(
        self,
        user_id: str,
        project_id: Optional[str] = None,
        review_status: Optional[str] = "approved",
        memory_type: Optional[str] = None,
        key: Optional[str] = None,
    ) -> List[Memory]:
        query = "SELECT * FROM memories WHERE user_id = ?"
        params = [user_id]
        if project_id is not None:
            query += " AND (project_id = ? OR project_id IS NULL)"
            params.append(project_id)
        if review_status is not None:
            query += " AND review_status = ?"
            params.append(review_status)
        if memory_type is not None:
            query += " AND memory_type = ?"
            params.append(memory_type)
        if key is not None:
            query += " AND key = ?"
            params.append(key)
        query += " ORDER BY updated_at DESC, id DESC"

        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return [self._row_to_memory(row) for row in cursor.fetchall()]

    def close(self) -> None:
        self.conn.close()

    def _memory_values(self, memory: Memory) -> tuple:
        return (
            memory.user_id,
            memory.project_id,
            memory.scope,
            memory.memory_type,
            memory.key,
            memory.value,
            memory.description,
            memory.confidence,
            memory.review_status,
            memory.source_type,
            json.dumps(memory.sources),
            json.dumps(memory.evidence),
            memory.sensitivity,
            1 if memory.needs_verification else 0,
            json.dumps(memory.supersedes),
            memory.superseded_by,
            memory.created_at,
            memory.updated_at,
            json.dumps(memory.metadata),
        )

    def _row_to_message(self, row) -> SessionMessage:
        return SessionMessage(
            id=row["id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            role=row["role"],
            content=row["content"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def _row_to_memory(self, row) -> Memory:
        return Memory(
            id=row["id"],
            user_id=row["user_id"],
            project_id=row["project_id"],
            scope=row["scope"],
            memory_type=row["memory_type"],
            key=row["key"],
            value=row["value"],
            description=row["description"] or "",
            confidence=row["confidence"],
            review_status=row["review_status"],
            source_type=row["source_type"] or "",
            sources=json.loads(row["sources"] or "[]"),
            evidence=json.loads(row["evidence"] or "[]"),
            sensitivity=row["sensitivity"] or "private",
            needs_verification=bool(row["needs_verification"]),
            supersedes=json.loads(row["supersedes"] or "[]"),
            superseded_by=row["superseded_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )


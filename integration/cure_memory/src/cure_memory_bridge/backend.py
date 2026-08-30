"""Host-side CURE memory backend for the automatic-extraction arm.

Owns the CURE policy for one SWE-bench episode on top of the shared lifecycle
skeleton (``shared_bridge.backend.BaseMemoryBackend``): the CUREMemorySystem
startup (origin-checked import, SQLite lifecycle, decision client), the
extraction success/soft-failure predicates, and the recall rendering. The
shared skeleton owns the event log and the memory.json artifact. Nothing in
this file may raise into the agent loop unless ``strict: true`` (and
``note_recall`` never raises at all — pure observability must not mask a model
exception).

When the model lanes run through a trajectory-proxy, the shared base annotates
the run's memory protocol (schema v6): one trace session per episode, CURE
extractions as generation operations audited by the exact replay implemented
here (``_classify``), and CURE recalls as search + main delivery. This file
keeps only the CURE-side adapter hooks — the protocol machinery (session,
generation, search, delivery, failure classification) lives in the base.
Tracing is pure observability: every annotation failure disables tracing and
leaves native behavior byte-identical.
"""

import hashlib
import inspect
import logging
import os
import sys
from pathlib import Path

from shared_bridge.annotate import canonical_json_sha256, inline_text_ref, sanitize_url
from shared_bridge.backend import (
    BaseMemoryBackend,
    _BackendUnavailable,
    _config_or_env,
    _finite,
    _new_session_id,
    _OperationTrace,
    _repo_of,
)

from cure_memory_bridge.config import CureMemoryConfig

logger = logging.getLogger("cure_memory_bridge.backend")

# CURE review statuses that count as not-live for CURE's own first-active-row
# rule (system.py:_upsert_memory). Local copy of
# cure_memory.models.INACTIVE_REVIEW_STATUSES — cure_memory is imported lazily
# and origin-checked, never at module level.
_INACTIVE_STATUSES = frozenset({"deleted", "rejected", "archived", "superseded"})
_IDENTITY_SCHEME = "cure-sqlite-row-version-v1"
_CANDIDATE_SCHEME = "cure-decision-candidate-v1"

try:
    from importlib.metadata import version as _pkg_version

    _BRIDGE_VERSION = _pkg_version("cure-memory-bridge")
except Exception:  # source tree without installed metadata
    _BRIDGE_VERSION = "0.1.0"


class _RowView:
    """Working-view copy of one CURE Memory row (the audit replays status
    transitions on these, never on the system's live objects)."""

    __slots__ = (
        "id",
        "user_id",
        "project_id",
        "scope",
        "memory_type",
        "key",
        "value",
        "confidence",
        "review_status",
        "needs_verification",
        "supersedes",
        "superseded_by",
        "created_at",
        "updated_at",
    )

    def __init__(self, memory):
        self.id = memory.id
        self.user_id = memory.user_id
        self.project_id = memory.project_id
        self.scope = memory.scope
        self.memory_type = memory.memory_type
        self.key = memory.key
        self.value = memory.value
        self.confidence = _finite(memory.confidence)
        self.review_status = memory.review_status
        self.needs_verification = bool(memory.needs_verification)
        self.supersedes = list(memory.supersedes or [])
        self.superseded_by = memory.superseded_by
        self.created_at = memory.created_at
        self.updated_at = memory.updated_at


class CureMemoryBackend(BaseMemoryBackend):
    """Drives CUREMemorySystem's extraction lifecycle for one SWE-bench episode."""

    _COUNTERS = (
        "memories_candidates",
        "memories_approved",
        "memories_pending",
        "memories_deleted",
        "memories_rejected",
        "memories_rejected_sensitive",
    )

    # The local extraction prompt is fully editable, so the shared extraction
    # guidelines have a channel: they append into the policy prompt.
    _CONVEYS_EXTRACTION_GUIDELINES = True

    def __init__(self, config: CureMemoryConfig, instance_id: str, model_base_url: str = ""):
        super().__init__(config, instance_id, model_base_url)
        self._system = None
        self._db_path: Path | None = None
        self._cure_system_path: str | None = None
        self._import_error: str | None = None
        self._SystemClass = None
        self._ClientClass = None
        self._recall_prompt: str = ""
        self._recall_policy_header: str = ""
        self._recall_section_header: str = ""

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    def _import_cure(self) -> bool:
        """Locate the intended cure_memory source; origin-checked lazy import.

        Trust order: explicit config/env candidates, then the source-tree
        candidate (the integration's own ``src/``), then (only when nothing
        explicit was supplied) a plain import. An already-cached package
        resolving outside an explicit candidate is a hard origin mismatch —
        never fall through to it.
        """
        explicit_candidates = []
        if self.config.cure_repo_path:
            explicit_candidates.append(Path(self.config.cure_repo_path))
        if env_path := os.environ.get("CURE_MEMORY_REPO"):
            explicit_candidates.append(Path(env_path))
        candidates = [
            *explicit_candidates,
            Path(__file__).resolve().parents[1],
        ]

        for index, candidate in enumerate(candidates):
            is_explicit = index < len(explicit_candidates)
            if not (candidate / "cure_memory" / "system.py").is_file():
                continue
            resolved_candidate = candidate.resolve()
            path_str = str(resolved_candidate)
            inserted = False
            if path_str not in sys.path:
                sys.path.insert(0, path_str)
                inserted = True
            try:
                import cure_memory.system
                from cure_memory.extractor import ChatGPTMemoryDecisionClient
                from cure_memory.prompts import (
                    MEMORY_RECALL_POLICY_HEADER,
                    MEMORY_RECALL_PROMPT,
                    MEMORY_RECALL_SECTION_HEADER,
                )
                from cure_memory.system import CUREMemorySystem
            except ImportError:
                if inserted:
                    sys.path.remove(path_str)
                # A half-broken candidate (the package imports, a submodule
                # raises) leaves its package object cached in sys.modules: the
                # next candidate's import would resolve submodules against the
                # CACHED package's __path__ — this candidate's tree — and the
                # documented fall-through would never actually try the next
                # tree. Evict the partial modules so it does.
                for name in [
                    name
                    for name in sys.modules
                    if name == "cure_memory" or name.startswith("cure_memory.")
                ]:
                    del sys.modules[name]
                continue
            origin = Path(cure_memory.system.__file__).resolve()
            if not origin.is_relative_to(resolved_candidate):
                if inserted:
                    sys.path.remove(path_str)
                if is_explicit:
                    self._import_error = (
                        f"cure_memory origin mismatch: {origin} is not under "
                        f"the configured candidate {resolved_candidate}"
                    )
                    logger.error(self._import_error)
                    return False
                continue
            self._SystemClass = CUREMemorySystem
            self._ClientClass = ChatGPTMemoryDecisionClient
            self._recall_prompt = MEMORY_RECALL_PROMPT
            self._recall_policy_header = MEMORY_RECALL_POLICY_HEADER
            self._recall_section_header = MEMORY_RECALL_SECTION_HEADER
            self._cure_system_path = str(origin)
            return True

        if explicit_candidates:
            return False
        try:
            import cure_memory.system
            from cure_memory.extractor import ChatGPTMemoryDecisionClient
            from cure_memory.prompts import (
                MEMORY_RECALL_POLICY_HEADER,
                MEMORY_RECALL_PROMPT,
                MEMORY_RECALL_SECTION_HEADER,
            )
            from cure_memory.system import CUREMemorySystem
        except ImportError:
            return False
        self._SystemClass = CUREMemorySystem
        self._ClientClass = ChatGPTMemoryDecisionClient
        self._recall_prompt = MEMORY_RECALL_PROMPT
        self._recall_policy_header = MEMORY_RECALL_POLICY_HEADER
        self._recall_section_header = MEMORY_RECALL_SECTION_HEADER
        self._cure_system_path = str(Path(cure_memory.system.__file__).resolve())
        return True

    def _derive_db_path(self) -> Path:
        if self.config.db_path:
            return Path(self.config.db_path).resolve()
        output_dir = Path(self.config.output_dir)
        if self.config.scope == "instance":
            return (output_dir / "cure_memory.sqlite3").resolve()
        return (output_dir.parent / "cure_memory.sqlite3").resolve()

    def _resolve_settings(self) -> dict:
        """Config value wins over $EXTRACT_*; unavailable when any is missing.

        Never construct the client with a missing field: CURE's ``or``-chain env
        fallbacks would otherwise engage third-party defaults and could leak
        $OPENAI_API_KEY (passing "" does not disable them).
        """
        model = _config_or_env(self.config.extract_model, "EXTRACT_MODEL")
        base_url = _config_or_env(self.config.extract_base_url, "EXTRACT_BASE_URL")
        api_key = _config_or_env(self.config.extract_api_key, "EXTRACT_API_KEY")
        if not model or not base_url or not api_key:
            raise _BackendUnavailable(
                "incomplete extraction settings: need model/base_url/api_key via "
                "agent.memory.extract_* or the EXTRACT_MODEL/EXTRACT_BASE_URL/EXTRACT_API_KEY env"
            )
        return {"model": model, "base_url": base_url, "api_key": api_key}

    def _make_llm_client(self, settings: dict):
        """Isolated decision-client constructor (the test seam)."""
        return self._ClientClass(
            model=settings["model"],
            base_url=settings["base_url"],
            api_key=settings["api_key"],
            max_completion_tokens=self.config.extract_max_tokens,
            reasoning_effort=self.config.extract_reasoning_effort or None,
            response_format={"type": "json_object"},
            timeout=self.config.extract_timeout,
            max_retries=self.config.extract_max_retries,
        )

    def _project_id(self) -> str | None:
        """The applicability layer key: per-instance isolation under
        ``scope="instance"``; under ``scope="run"`` the episode's repo, so
        repo-bound rows stay inside their repository while general rows
        (``project_id=NULL``) flow to every episode of the run."""
        if self.config.scope == "instance":
            return self.instance_id
        return _repo_of(self.instance_id)

    def _initial_settings(self) -> dict:
        return {
            "extract_model": "",
            "extract_base_url": "",
            "extract_max_tokens": self.config.extract_max_tokens,
            "extract_reasoning_effort": self.config.extract_reasoning_effort,
            "extract_timeout": self.config.extract_timeout,
            "extract_max_retries": self.config.extract_max_retries,
            **self._core_initial_settings(),
        }

    def _startup(self, settings: dict) -> None:
        self._settings["extract_model"] = settings["model"]
        # Only the safe form is persisted: 16-hex trajectory hash, no
        # userinfo/query/fragment — never the bearer URL (PLAN §6.2).
        self._settings["extract_base_url"] = sanitize_url(settings["base_url"])
        if not self._import_cure():
            raise _BackendUnavailable(self._import_error or "cure_memory could not be imported from an allowed source")
        if not hasattr(self._SystemClass, "has_unextracted_messages"):
            # An external checkout (cure_repo_path / $CURE_MEMORY_REPO) predating
            # the readiness probe would otherwise fail every extraction tick with
            # AttributeError until the breaker trips — be loudly unavailable at
            # start instead of silently never extracting.
            raise _BackendUnavailable(
                f"cure_memory at {self._cure_system_path} predates has_unextracted_messages(); refresh the checkout"
            )
        if "policy_guidelines" not in inspect.signature(self._SystemClass.__init__).parameters:
            # The same guard for the extraction-guidelines channel: a checkout
            # new enough to carry the readiness probe but predating the ctor
            # kwarg would otherwise die at construction with a raw TypeError.
            raise _BackendUnavailable(
                f"cure_memory at {self._cure_system_path} predates the policy_guidelines "
                "constructor kwarg; refresh the checkout"
            )
        self._db_path = self._derive_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        client = self._make_llm_client(settings)
        # Assign immediately: if a later startup step fails, the open
        # connection must be closable (assigning at the end would leak it).
        self._system = self._SystemClass(
            str(self._db_path), llm_client=client, policy_guidelines=self._extraction_guidelines()
        )
        self._system.store.conn.execute("PRAGMA journal_mode=WAL")
        self._system.store.conn.execute("PRAGMA busy_timeout=5000")
        self._session_id = _new_session_id(self.instance_id)
        self._system.start_session(
            self.config.user_id,
            project_id=self._project_id(),
            session_id=self._session_id,
        )

    def _reset_extras(self) -> None:
        # Derived per-start state must not survive a re-start: a failed
        # re-start writes the initial artifact literal (db_path null), never
        # the previous episode's values. The handle too: the base closed it
        # just above, and a failed re-start must not leave a stale closed
        # handle behind.
        self._system = None
        self._db_path = None
        self._cure_system_path = None
        self._import_error = None

    def _start_event_extras(self) -> dict:
        return {"db_path": str(self._db_path)}

    def _close(self) -> None:
        # The closed handle is deliberately kept non-null (the audit/tests
        # inspect it); sqlite's close is idempotent, so the base's re-start
        # close and finalize's close compose safely. Errors propagate to the
        # base's call sites.
        if self._system is not None:
            self._system.close()

    # ------------------------------------------------------------------
    # Trace adapter hooks (the protocol machinery lives in the base)
    # ------------------------------------------------------------------
    def _adapter_meta(self) -> dict:
        return {"name": "cure", "version": _BRIDGE_VERSION}

    def _trace_namespace(self) -> str:
        # The run root is timestamped, so the resolved store path names this
        # arm's shared store; the hash keeps the local path out of artifacts.
        return hashlib.sha256(str(self._db_path).encode()).hexdigest()

    def _memory_lane_url_source(self, settings: dict) -> str:
        # The memory lane is the extraction lane: its model base URL derives
        # the lane's annotate endpoint.
        return settings["base_url"]

    def _trace_context(self) -> dict:
        return self._cure_context()

    def _cure_context(self) -> dict:
        """Native CURE session data, kept strictly under extensions.cure."""
        try:
            from importlib.metadata import version as _pkg_version

            cure_version = _pkg_version("cure-memory")
        except Exception:
            cure_version = None
        context = {
            "session_id": self._session_id,
            "scope": self.config.scope,
            "user_id": self.config.user_id,
            "project_id": self._project_id(),
            "cure_system_path": self._cure_system_path,
        }
        if cure_version is not None:
            context["cure_version"] = cure_version
        return context

    def _memory_ref(self, obj) -> dict:
        return self._row_ref(obj)

    def _snapshot_memory_state(self) -> dict | None:
        return self._snapshot_rows()

    def _attribute_changes(self, operation: _OperationTrace, result, after: dict | None):
        """CURE's exact replay audit; the exception path (``result is None``,
        reached only with both snapshots present) falls back to the row-level
        diff."""
        if result is None:
            return self._diff_changes(operation.before, after), [], []
        return self._classify(operation, result, after)

    def _generation_end_context(self, step, result, audit: dict) -> dict:
        """The extensions.cure generation-end literal: checkpoint/mutation-audit
        lines plus the native result counts (absent on the exception path)."""
        context = {
            "session_id": self._session_id,
            "extraction_step": str(step),
            "checkpoint": audit.get("checkpoint"),
            "mutation_audit": "clean" if audit.get("clean") else "drift",
        }
        if audit.get("unexplained"):
            context["unexplained"] = audit["unexplained"]
        if result is not None:
            rejected_by_reason: dict[str, int] = {}
            for rejection in result.rejected:
                rejected_by_reason[rejection.reason] = rejected_by_reason.get(rejection.reason, 0) + 1
            context.update(
                {
                    "candidates": len(result.candidates),
                    "approved": len(result.approved),
                    "pending_review": len(result.pending_review),
                    "deleted": len(result.deleted),
                    "rejected": len(result.rejected),
                    "rejected_by_reason": rejected_by_reason,
                }
            )
        return context

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def _store_message(self, role: str, text: str, step: int):
        stored = self._system.record_message(role=role, content=text, metadata={"step": step})
        # The base tracks the pending generation input from the returned id.
        return stored.id

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    def _perform_extraction(self, step) -> None:
        """One extraction attempt; CURE's checkpoint semantics provide the retry.

        Traced as one core generation operation: start (with the pending
        normalized inputs) before work, before/after snapshots around the
        unchanged native call, audited change events in deterministic chunks,
        and one generation end last. Any annotation failure leaves the native
        path untouched.
        """
        if self._system is None or not self._system.has_unextracted_messages():
            # Readiness guard first, BEFORE any counting (base contract): a
            # tick with no unextracted messages is not a counted call — it
            # would make no EXTRACT call yet inflate extraction_calls, and
            # post a vacuous generation op when traced.
            return
        self._counts["extraction_calls"] += 1
        try:
            operation = self._generation_begin(step)
        except Exception:
            logger.exception("annotation generation-begin failed; extraction continues untraced")
            operation = None
        try:
            result = self._system.extract_runtime_memories()
        except Exception as e:
            try:
                self._generation_finish_exception(operation, step, e)
            except Exception:
                logger.exception("annotation generation-end failed; extraction continues untraced")
            raise  # hard failure: the base shell counts, registers, and gates
        rejected_by_reason: dict[str, int] = {}
        for rejection in result.rejected:
            rejected_by_reason[rejection.reason] = rejected_by_reason.get(rejection.reason, 0) + 1
        self._counts["memories_candidates"] += len(result.candidates)
        self._counts["memories_approved"] += len(result.approved)
        self._counts["memories_pending"] += len(result.pending_review)
        self._counts["memories_deleted"] += len(result.deleted)
        self._counts["memories_rejected"] += len(result.rejected)
        self._counts["memories_rejected_sensitive"] += rejected_by_reason.get("sensitive_information", 0)
        self._log_event(
            "extraction",
            step=step,
            candidates=len(result.candidates),
            approved=len(result.approved),
            pending=len(result.pending_review),
            deleted=len(result.deleted),
            rejected=len(result.rejected),
            rejected_by_reason=rejected_by_reason,
            errors=list(result.errors),
        )
        if result.errors:
            # Soft failure: counted and registered here (no event duplication —
            # the extraction event above already carries the errors), never
            # raised — CURE held the checkpoint and retries next tick.
            self._counts["extraction_errors"] += 1
            self._register_extraction_failure(step, "; ".join(result.errors), log_event=False)
        else:
            self._consecutive_errors = 0
            # CURE advanced its checkpoint over exactly these messages; the
            # retained input refs must clear only under that same rule.
            if self._trace is not None:
                self._trace.pending_inputs.clear()
        try:
            self._generation_finish(operation, step, result, result.errors)
        except Exception:
            logger.exception("annotation generation-end failed; extraction continues untraced")

    # ------------------------------------------------------------------
    # Generation audit (the CURE-side attribution replay)
    # ------------------------------------------------------------------
    def _snapshot_rows(self) -> dict | None:
        """All rows in the extraction-visible project scope (same scope and
        review_status=None as extract_runtime_memories), or None on failure."""
        try:
            rows = self._system.memory_search(
                self.config.user_id,
                query=None,
                project_id=self._project_id(),
                review_status=None,
            )
            return {row.id: _RowView(row) for row in rows}
        except Exception:
            logger.warning("memory audit snapshot failed", exc_info=True)
            return None

    def _classify(self, operation: _OperationTrace, result, after: dict | None):
        """Replay one extraction's observable effects into core change payloads.

        Starts a working row view from the before snapshot, applies CURE
        deletions first, then replays result.candidates in native decision
        order using CURE's own first-active-row rule, and finally cross-checks
        the view against the after snapshot. Anything not attributable is an
        unexplained concurrent change — a validation error, never ignored.
        """
        if operation.before is None:
            # The before snapshot failed, so full attribution is impossible:
            # replaying onto an empty view would fabricate drift for every
            # pre-existing row. Emit only the result's own products.
            if result.errors:
                return [], [self._candidate_ref(candidate, index) for index, candidate in enumerate(result.candidates)], []
            changes = []
            produced = []
            unexplained = []
            for candidate in result.candidates:
                if candidate.id is not None:
                    produced.append(self._row_ref(candidate))
                    continue
                # No row id: CURE's no-op branch, attributed against the after
                # snapshot (the failed before snapshot left no replay view).
                self._attribute_noop(operation, after, candidate, changes, produced, unexplained)
            return changes, produced, unexplained
        view = dict(operation.before)
        changes: list[dict] = []
        produced: list[dict] = []
        unexplained: list[str] = []
        if not result.errors:
            # CURE guarantees no writes when errors are present (checkpoint held).
            for memory in result.deleted:
                row = view.get(memory.id)
                if row is None:
                    unexplained.append(f"deletion matched row {memory.id} outside the audit snapshot")
                    continue
                changes.append(self._change_payload(operation, "delete", [row], []))
                row.review_status = "deleted"
            for candidate in result.candidates:
                if candidate.id is not None and not candidate.supersedes:
                    changes.append(self._change_payload(operation, "create", [], [candidate]))
                    view[candidate.id] = _RowView(candidate)
                    produced.append(self._row_ref(candidate))
                elif candidate.id is not None:
                    superseded = []
                    for old_id in candidate.supersedes:
                        old = view.get(old_id)
                        if old is None:
                            unexplained.append(f"candidate supersedes row {old_id} outside the audit snapshot")
                            continue
                        superseded.append(old)
                    changes.append(
                        self._change_payload(operation, "update", superseded, [candidate], supersede_new=candidate)
                    )
                    for old in superseded:
                        old.review_status = "superseded"
                        old.superseded_by = candidate.id
                    view[candidate.id] = _RowView(candidate)
                    produced.append(self._row_ref(candidate))
                else:
                    # No row id: CURE's no-op branch, attributed against the
                    # replay view (CURE's exact first-active-row rule).
                    self._attribute_noop(operation, view, candidate, changes, produced, unexplained)
        else:
            produced = [self._candidate_ref(candidate, index) for index, candidate in enumerate(result.candidates)]
        if after is not None:
            unexplained.extend(self._audit_drift(view, after))
        return changes, produced, unexplained

    def _attribute_noop(self, operation: _OperationTrace, view: dict | None, candidate, changes, produced, unexplained) -> None:
        """CURE's no-op branch: the dedup matched a row the extraction left
        untouched, so attribute the no-op to the view's exact first-active
        match (same value and review status) instead of silently dropping the
        candidate. Anything else is an operation-local candidate ref — plus an
        unexplained drift note when there is a view to cross-check against."""
        active = self._first_active_row(view, candidate) if view is not None else None
        if active is not None and active.value == candidate.value and active.review_status == candidate.review_status:
            changes.append(self._change_payload(operation, "noop", [active], [active]))
            produced.append(self._row_ref(active))
            return
        produced.append(self._candidate_ref(candidate, len(produced)))
        if view is not None:
            unexplained.append(f"no-op candidate {candidate.key!r} has no matching first active row")

    @staticmethod
    def _first_active_row(view: dict, candidate) -> _RowView | None:
        """CURE's first-active-row rule over the working view: query by
        user_id, apply the project predicate only when the candidate's project
        is non-null (a general candidate never matches repo-bound rows — the
        layer guard of system.py:_upsert_memory, mirrored exactly), no
        review-status predicate, then filter type/key, order updated_at DESC,
        id DESC, and take the first still-active row."""
        rows = [row for row in view.values() if row.user_id == candidate.user_id]
        if candidate.project_id is not None:
            rows = [row for row in rows if row.project_id == candidate.project_id or row.project_id is None]
        else:
            rows = [row for row in rows if row.project_id is None]
        rows = [row for row in rows if row.memory_type == candidate.memory_type and row.key == candidate.key]
        rows = [row for row in rows if row.review_status not in _INACTIVE_STATUSES]
        rows.sort(key=lambda row: (row.updated_at, row.id if row.id is not None else -1), reverse=True)
        return rows[0] if rows else None

    @staticmethod
    def _audit_drift(view: dict, after: dict) -> list[str]:
        unexplained = []
        for row_id, after_row in after.items():
            view_row = view.get(row_id)
            if view_row is None:
                unexplained.append(f"row {row_id} appeared outside attribution")
            elif view_row.review_status != after_row.review_status or view_row.superseded_by != after_row.superseded_by:
                unexplained.append(
                    f"row {row_id} changed outside attribution "
                    f"({view_row.review_status} -> {after_row.review_status})"
                )
        for row_id in view:
            if row_id not in after:
                unexplained.append(f"row {row_id} vanished outside attribution")
        return unexplained

    def _diff_changes(self, before: dict, after: dict) -> list[dict]:
        """Exception fallback: row-level before/after transitions without a
        result to attribute them to (completeness is stamped partial)."""
        transitions = []
        covered_new_ids = {
            row.superseded_by
            for row_id, row in after.items()
            if row_id in before
            and before[row_id].review_status != "superseded"
            and row.review_status == "superseded"
            and row.superseded_by is not None
        }
        for row_id, after_row in after.items():
            before_row = before.get(row_id)
            if before_row is None:
                if row_id not in covered_new_ids:
                    transitions.append(("create", [], [after_row], None))
                continue
            if before_row.review_status == after_row.review_status:
                continue
            if after_row.review_status == "deleted":
                transitions.append(("delete", [before_row], [], None))
            elif after_row.review_status == "superseded":
                new_row = after.get(after_row.superseded_by)
                transitions.append(("update", [before_row], [new_row] if new_row else [], new_row))
            else:
                transitions.append(("update", [before_row], [after_row], None))
        return [
            self._change_payload(None, action, before_rows, after_rows, supersede_new=new_row)
            for action, before_rows, after_rows, new_row in transitions
        ]

    def _row_ref(self, row) -> dict:
        """One CURE row version as a portable native-stable MemoryRef."""
        semantic = {
            "user_id": row.user_id,
            "project_id": row.project_id,
            "scope": row.scope,
            "memory_type": row.memory_type,
            "key": row.key,
            "value": row.value,
        }
        digest = canonical_json_sha256(semantic)
        item_digest = canonical_json_sha256({key: value for key, value in semantic.items() if key != "value"})
        cure = {
            "store_id": row.id,
            "user_id": row.user_id,
            "project_id": row.project_id,
            "scope": row.scope,
            "memory_type": row.memory_type,
            "key": row.key,
            "confidence": _finite(getattr(row, "confidence", 0.0)),
            "review_status": row.review_status,
            "needs_verification": bool(getattr(row, "needs_verification", False)),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        if getattr(row, "supersedes", None):
            cure["supersedes"] = list(row.supersedes)
        if getattr(row, "superseded_by", None) is not None:
            cure["superseded_by"] = row.superseded_by
        return {
            # store_id alone could collide with a reused row id after a
            # different-value rewrite; the semantic digest disambiguates.
            "version_id": f"{row.id}:{digest}",
            "identity_strength": "native_stable",
            "identity_scheme": _IDENTITY_SCHEME,
            "item_id": item_digest,
            "namespace": self._namespace,
            "content": inline_text_ref(row.value),
            "extensions": {"cure": cure},
        }

    def _candidate_ref(self, candidate, index: int) -> dict:
        """Operation-local ref for a candidate that never became a row version
        (result.errors prevented all writes)."""
        return {
            "version_id": f"candidate-{index}",
            "identity_strength": "operation_local",
            "identity_scheme": _CANDIDATE_SCHEME,
            "namespace": self._namespace,
            "content": inline_text_ref(candidate.value),
            "extensions": {
                "cure": {
                    "user_id": candidate.user_id,
                    "project_id": candidate.project_id,
                    "scope": candidate.scope,
                    "memory_type": candidate.memory_type,
                    "key": candidate.key,
                    "confidence": _finite(getattr(candidate, "confidence", 0.0)),
                    "review_status": candidate.review_status,
                }
            },
        }

    def _change_payload(self, operation: _OperationTrace | None, action, before_rows, after_rows, supersede_new=None):
        """CURE's change-evidence literal: observed diffs cited by store row id."""
        return super()._change_payload(
            operation,
            action,
            before_rows,
            after_rows,
            supersede_new=supersede_new,
            evidence="observed_diff",
            extensions={
                "cure": {
                    "before_store_ids": [row.id for row in before_rows],
                    "after_store_ids": [row.id for row in after_rows if row.id is not None],
                }
            },
        )

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------
    def _search(self) -> list:
        """CURE's approved-memory search against the current recall query."""
        if self._system is None:
            return []
        try:
            return self._system.memory_search(
                self.config.user_id,
                query=self._recall_query(),
                project_id=self._project_id(),
                review_status="approved",
            )
        except Exception:
            # The private counting the base contract describes; the recall
            # envelope keeps counting backend_errors on top (both grains).
            self._counts["search_errors"] += 1
            raise

    def _recall_sections(self) -> str:
        # Shared policy first (the base composes it), then the CURE policy
        # block with its verify-against-source lines, then the section title.
        return "\n".join(
            [self._recall_policy_header, self._recall_prompt, "", self._recall_section_header]
        )

    def _hit_origin(self, memory) -> str | None:
        # The extraction path stamps the episode's session id into sources[0]
        # at candidate construction and the store round-trips it (an update
        # re-stamps the superseding episode's, so the origin names the episode
        # that created the memory's CURRENT version).
        sources = getattr(memory, "sources", None)
        if not sources or not isinstance(sources[0], dict):
            return None
        origin = sources[0].get("session_id")
        return origin if isinstance(origin, str) and origin else None

    def _hit_score(self, memory) -> float | None:
        # The native search stamps its term-overlap score (an unbounded
        # term-count scale) into metadata as a transient search-time
        # annotation; a query-less listing carries no score.
        score = getattr(memory, "metadata", {}).get("score")
        return score if isinstance(score, (int, float)) and not isinstance(score, bool) else None

    def _matched_precision(self) -> str:
        # The native search is a local full scan with no internal truncation
        # (every row whose score clears the scorer's zero bar is returned),
        # so len(hits) IS the true match count — never a lower bound.
        return "exact"

    def _render_line(self, memory) -> str:
        # The layer tag derives from project_id — the field the recall lattice,
        # the upsert guard, and the audit replay all key on — never from the
        # cosmetic scope label (general = repo-independent lesson, repo =
        # bound to one repository). It augments the type tag so the model can
        # weigh applicability alongside the episode-provenance suffix.
        layer = "general" if memory.project_id is None else "repo"
        verification = " needs_verification" if memory.needs_verification else ""
        return f"- [{memory.memory_type}:{layer}{verification}] {memory.key}: {memory.value}"

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------
    def _final_dump(self) -> list[dict]:
        if self._system is None:
            return []
        # Scoped to the episode's own visibility lattice (its repo's rows plus
        # the general layer — the set _snapshot_rows audits): a per-episode
        # artifact tells the episode's story; the run-wide store is one sqlite
        # query away via db_path. project_id rides along so a scope="project"
        # row names its repository without opening the store.
        return [
            {
                "id": memory.id,
                "key": memory.key,
                "value": memory.value,
                "memory_type": memory.memory_type,
                "confidence": memory.confidence,
                "review_status": memory.review_status,
                "scope": memory.scope,
                "project_id": memory.project_id,
            }
            for memory in self._system.memory_search(
                self.config.user_id,
                query=None,
                project_id=self._project_id(),
                review_status=None,
            )
        ]

    def _memory_json_fields(self) -> dict:
        return {
            "project_id": self._project_id(),
            # One sentinel for both artifacts: null until the path is derived
            # (a failed start leaves it unset), same as stats().
            "db_path": str(self._db_path) if self._db_path else None,
            "cure_system_path": self._cure_system_path,
        }

    def _stats_extras(self) -> dict:
        return {"db_path": str(self._db_path) if self._db_path else None}

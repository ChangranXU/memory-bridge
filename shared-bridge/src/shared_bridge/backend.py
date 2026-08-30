"""The memory-backend lifecycle skeleton for the automatic-extraction arm.

``BaseMemoryBackend`` owns the control flow every integration runs —
``start`` -> ``set_task`` -> ``record`` -> ``maybe_extract`` ->
``recall_context``/``note_recall``/``deliver_recall`` -> ``finalize``, plus
``stats`` and the memory.json artifact — and expresses every legitimate
integration divergence as an explicit hook or override, never a unified
policy. Nothing in this module names or knows a specific integration.

The base also owns the schema-v6 memory-protocol annotation of the common
memory actions (session / generation / search / delivery and their failure
semantics). Integrations plug in through the adapter hooks
(``_adapter_meta`` / ``_memory_ref`` / ``_trace_namespace`` required, the rest
defaulted); every ``extensions.<name>`` key comes from ``_adapter_meta``, so
the shared layer never names an integration. Tracing is pure observability:
every annotation failure degrades to untraced native work, never to a
behavior change.

Failure discipline: nothing raises into the agent loop unless
``config.strict`` (fail-closed otherwise), and ``note_recall`` never raises at
all — pure observability must not mask a model exception.
"""

import json
import logging
import math
import os
import time
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from uuid import uuid4

from shared_bridge.annotate import (
    Annotator,
    PostResult,
    endpoints_compatible,
    inline_text_ref,
    resolve_lane_url,
    sanitize_url,
    text_sha256,
)
from shared_bridge.config import MemoryConfig
from shared_bridge.prompts import (
    EXTRACTION_GUIDELINES_DEFAULT,
    QUERY_REWRITE_PROMPT,
    RECALL_POLICY_DEFAULT,
    extraction_episode_context,
    query_rewrite_user_message,
)
from shared_bridge.side_model import RewrittenQuery, SideModelConfig, StructuredCall, call_structured

logger = logging.getLogger("shared_bridge.backend")

TRUNCATION_MARKER = "\n... [truncated]"

# The recall line-truncation suffix (per-memory cap and total-budget
# truncate-to-fit alike): deliberately neutral — the recall header already
# carries the agent-facing guides, so the suffix does not advertise tools.
RECALL_LINE_TRUNCATION = " ...[truncated]"
# Truncate-to-fit floor (the native recall budget's minimum-truncated-line
# constant): a line that does not fit the remaining
# total budget is truncated into it only when at least this much room
# remains; below the floor the line is skipped and the walk continues.
_MIN_TRUNCATED_RECALL_LINE_CHARS = 40

# Counter keys every backend carries; integrations declare extras via _COUNTERS.
_CORE_COUNTERS = (
    "messages_recorded",
    "extraction_calls",
    "extraction_errors",
    "recall_injections",
    "backend_errors",
    "search_errors",  # failed native searches (transport or timeout), counted privately by _search
    "recall_cache_hits",  # injected payloads served from the dirty-flag cache (no search ran); counted at delivery like recall_injections
    "rewrite_calls",  # query-rewrite attempts (the QUERY lane's calls)
    "rewrite_successes",  # rewrites that replaced the recall query
    "rewrite_failures",  # fail-closed rewrites (the old query kept, the flag untouched)
)


class _BackendUnavailable(RuntimeError):
    """Expected-unavailability start failure (settings/import), logged without traceback."""


def _truncate(text: str, limit: int) -> str:
    """Cap ``len(result)`` at ``limit``, truncation marker included."""
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:limit]
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _new_session_id(instance_id: str) -> str:
    """Unique per episode: on a shared run store, reusing a session id would
    make the new episode reprocess all old messages of that session."""
    return f"{instance_id}-{uuid4().hex}"


def _origin_instance(origin: str) -> str | None:
    """The instance id inside a ``<instance_id>-<uuid4hex>`` session id, or
    None when the origin does not parse (the store keeps no session→instance
    registry to resolve it through)."""
    head, sep, tail = origin.rpartition("-")
    if not sep or not head or len(tail) != 32:
        return None
    return head if all(char in "0123456789abcdef" for char in tail) else None


def _repo_of(instance_id: str) -> str:
    """The repository key for a SWE-bench instance id (`<owner>__<repo>-<number>`
    → `<owner>__<repo>`). An id with no trailing `-<digits>` keeps the full id
    (conservative: that instance keeps its own project scope; general memories
    still flow)."""
    repo, sep, number = instance_id.rpartition("-")
    if sep and repo and number.isdigit():
        return repo
    return instance_id


def _config_or_env(value: str, env_name: str) -> str:
    """Config value wins over the env fallback; the result is stripped."""
    return (value or os.environ.get(env_name, "")).strip()


_MAX_EVENTS_PER_POST = 256  # the recorder's per-request event cap
# Per-POST byte budget under the recorder's 1 MiB annotation body cap (a
# larger body is a definitive 413). Events are sized with the Annotator's
# exact serialization (json.dumps defaults: ASCII-only output, so the string
# length is the byte length); the margin covers the {"events": [...]} wrapper
# and separators.
_MAX_POST_BYTES = 1024 * 1024 - 2048


def _chunk_events(events: list[dict]) -> list[list[dict]]:
    """Split events into POST batches under both recorder caps (event count
    and body bytes). A single event over the byte budget still forms its own
    batch: the endpoint's 413 classifies it."""
    chunks: list[list[dict]] = []
    chunk: list[dict] = []
    size = 0
    for event in events:
        event_size = len(json.dumps(event))
        if chunk and (len(chunk) >= _MAX_EVENTS_PER_POST or size + event_size > _MAX_POST_BYTES):
            chunks.append(chunk)
            chunk, size = [], 0
        chunk.append(event)
        size += event_size
    if chunk:
        chunks.append(chunk)
    return chunks


class _TraceState:
    """Per-episode annotation wiring; ``backend._trace is None`` means untraced."""

    def __init__(self, annotator: Annotator, main_url: str, memory_url: str):
        self.annotator = annotator
        self.main_url = main_url
        self.memory_url = memory_url
        self.trace_session_id: str | None = None
        self.session_open = False
        self.memory_lane_enabled = True  # a recovery 409 turns this off permanently
        self.delivery_enabled = True  # an unconfirmed/rejected delivery turns this off
        self.pending_inputs: list[dict] = []  # recorded messages not yet checkpointed
        self.pending_search: _OperationTrace | None = None  # completed search awaiting its delivery


class _OperationTrace:
    """One extraction's tracing context (cursor, snapshots, disable flags)."""

    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        self.cursor: int | None = None
        self.disabled = False  # definitive rejection (409/413): post nothing more
        self.start_accepted = False  # the recorder accepted the start post
        self.before: dict | None = None  # snapshot key -> native row, None = snapshot unavailable
        self.input_count = 0  # pending inputs posted in the start event


def _sanitize_error_code(code: object) -> str:
    """Map a free-form error string onto the recorder's name charset.

    The recorder requires each code to fullmatch
    [A-Za-z0-9][A-Za-z0-9._-]{0,127}, but native errors are free-form text, so
    characters outside the charset fold to "_", leading characters that
    cannot open a name drop, and an empty result falls back to "error".
    """
    mapped = "".join(
        char if char.isascii() and (char.isalnum() or char in "._-") else "_" for char in str(code)
    )
    return mapped.lstrip("._-")[:128] or "error"


def _finite(value, default=0.0) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else default


class BaseMemoryBackend(ABC):
    """The shared lifecycle skeleton; integrations plug in via hooks.

    Startup (``start`` template, in order): close any previous episode's
    handle (a re-start must not leak it), reset (including
    ``_reset_extras``), ``_resolve_settings`` (raise ``_BackendUnavailable``
    for expected unavailability), ``_startup`` (fill computed settings keys,
    construct/start the integration system, mint ``self._session_id`` via
    ``_new_session_id``), the start event (with ``_start_event_extras``), then
    ``_on_started`` (the base's trace setup).

    Work: the record loop normalizes (``_message_text`` + truncation), filters
    (``_should_store``), maps (``_normalize_role``), and stores
    (``_store_message``) per message, tracking traced pending inputs from its
    return. ``maybe_extract`` runs the high-water bucket schedule; ``_extract``
    is the thin shell around ``_perform_extraction``. ``recall_context`` runs
    one shared rank-then-fill loop over ``_search`` hits, rendered by
    ``_recall_header``/``_render_line``.

    Teardown (``finalize``): final flush, ``_final_dump``, memory.json, then
    ``_close`` — mechanics only, errors propagate to the base's call sites,
    which own containment and logging. The first error is re-raised under
    ``strict`` after cleanup. After finalize the work surface
    (``record``/``maybe_extract``/``recall_context``) is dormant: a silent
    no-op, never a counted error — the handle is closed and the observability
    lanes are done (fail-closed counts real backend failures, not lifecycle
    misuse).

    Counters: the base zeroes the core set plus the integration's declared
    ``_COUNTERS`` on every start. Artifact shape: common memory.json fields
    plus ``_memory_json_fields``; ``stats`` likewise takes ``_stats_extras``.
    """

    _COUNTERS: tuple = ()  # integration-declared extra counters

    # Capability declaration (class-level, like _COUNTERS): whether this
    # integration has a channel to hand extraction prompt rules to its
    # extraction engine — a local extractor composes them into its policy
    # prompt; a hosted platform receives an advisory instructions field. An
    # integration declaring False has no channel: the guidelines resolve to
    # "" (nothing composed, nothing sent) and a configured override draws one
    # start-time warning instead of silently vanishing.
    _CONVEYS_EXTRACTION_GUIDELINES: bool = False

    def __init__(self, config: MemoryConfig, instance_id: str, model_base_url: str = ""):
        self.config = config
        self.instance_id = instance_id
        self._model_base_url = model_base_url  # main-lane model URL (annotate URLs derive from it)
        self._available = False
        self._session_id: str | None = None
        self._task: str | None = None
        self._current_query: str | None = None  # the recall query (the task text until a rewrite replaces it)
        self._query_source = "task"  # "task" | "rewritten" — the search start's query provenance
        # Dirty-flag search cache: the search result can only change when the
        # query is replaced or the store is written, so a clean flag reuses the
        # memoized payload (None = a cached EMPTY answer) instead of searching
        # again. The anchor memoizes the payload's own accepted search token
        # (None = the payload's search was not fully recorded), so clean-step
        # deliveries never cite a search the payload did not come from.
        self._search_dirty = True  # a cold cache must search, never reuse "nothing"
        self._cached_recall: dict | None = None
        self._cached_anchor: _OperationTrace | None = None
        self._events: list[dict] = []
        self._counts: dict = {}
        self._settings: dict = {}
        self._last_extract_bucket = 0
        self._consecutive_errors = 0
        self._extract_breaker = False
        self._finalized = False
        self._trace: _TraceState | None = None
        self._namespace = ""
        # Native search/rewrite seconds pending consumption by the agent's
        # wall-clock exemption (backend-owned: must work with annotate=False).
        self._io_duration = 0.0
        # A bounded view of the most recent recorded messages, rewriter input
        # only: each entry is already truncated to max_message_chars at record
        # time, so the buffer is bounded by 6 x max_message_chars.
        self._recent_messages: deque[tuple[str, str]] = deque(maxlen=6)
        self._last_rewrite_bucket = 0
        self._rewrite_settings: dict | None = None  # resolved rewriter connection (None = disabled)
        self._rewrite_consecutive_errors = 0
        self._rewrite_breaker = False

    def effective_user_id(self) -> str:
        """The store-side user id — the run-isolation tier shared by every
        integration whose store persists across episodes: ``scope=run``
        shares the configured user id across the run root, ``scope=instance``
        suffixes the instance id. Integrations with a richer scoping lattice
        override this."""
        if self.config.scope == "instance":
            return f"{self.config.user_id}:{self.instance_id}"
        return self.config.user_id

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    def start(self) -> None:
        # A re-start must not leak the previous episode's handle: close it
        # before the reset (on a first start every ``_close`` is a no-op).
        # Contained regardless of strict: an old handle's teardown failure
        # must not block or mask the new start.
        try:
            self._close()
        except Exception:
            logger.exception("failed to close the previous memory backend handle")
        # Reset first: any failed start — including a re-start — must write the
        # full initial settings literal (computed keys blank unless the failure
        # came after they were filled) and, pre-mint, session_id: null.
        self._available = False
        self._session_id = None
        self._current_query = None
        self._query_source = "task"
        self._search_dirty = True
        self._cached_recall = None
        self._cached_anchor = None
        self._settings = self._initial_settings()
        self._counts = {name: 0 for name in (*_CORE_COUNTERS, *self._COUNTERS)}
        self._events = []
        self._last_extract_bucket = 0
        self._consecutive_errors = 0
        self._extract_breaker = False
        self._finalized = False
        self._trace = None
        self._namespace = ""
        self._io_duration = 0.0
        self._recent_messages.clear()
        self._last_rewrite_bucket = 0
        self._rewrite_settings = None
        self._rewrite_consecutive_errors = 0
        self._rewrite_breaker = False
        self._reset_extras()

        try:
            settings = self._resolve_settings()
            self._startup(settings)
            # The rewriter's fail-closed settings check (the same one EXTRACT
            # uses): with rewriting enabled, a permanently-doomed connection
            # fails the episode start instead of failing closed at every
            # boundary while silently keeping the task text.
            self._rewrite_settings = self._resolve_rewrite_settings()
            if self._rewrite_settings is not None:
                self._settings["rewrite_model"] = self._rewrite_settings["model"]
                # Only the safe form is persisted (rule 4) — the lane URL
                # carries the bearer trajectory ID.
                self._settings["rewrite_base_url"] = sanitize_url(self._rewrite_settings["base_url"])
            self._available = True
            if not self._CONVEYS_EXTRACTION_GUIDELINES and self.config.extraction_guidelines.strip():
                logger.warning(
                    "agent.memory.extraction_guidelines is set but this integration's extraction "
                    "engine accepts no custom prompt rules; the override is ignored"
                )
            self._log_event("start", session_id=self._session_id, **self._start_event_extras())
            self._on_started(settings)
        except Exception as e:
            self._available = False
            if isinstance(e, _BackendUnavailable):
                logger.error("memory backend unavailable: %s", e)
            else:
                logger.exception("failed to start memory backend")
            self._log_event("error", op="start", error=str(e))
            # Containment is load-bearing: a close error here must not mask the
            # primary start error or skip the memory.json write.
            try:
                self._close()
            except Exception:
                logger.exception("failed to close the partially started memory backend")
            try:
                self._write_memory_json()
            except Exception:
                logger.exception("failed to write memory.json after start failure")
            if self.config.strict:
                raise

    @abstractmethod
    def _initial_settings(self) -> dict:
        """The handwritten settings literal for memory.json, with the computed
        keys blank (``_startup`` fills them on success)."""

    def _core_initial_settings(self) -> dict:
        """The initial-settings keys every integration carries verbatim from
        the shared config; ``_initial_settings`` adds the integration-owned
        keys around them."""
        return {
            "extract_every_n_steps": self.config.extract_every_n_steps,
            "extract_max_consecutive_errors": self.config.extract_max_consecutive_errors,
            # The conveyed text ("" when this integration has no channel), so an
            # artifact always records the policy its extractions ran under.
            "extraction_guidelines": self._extraction_guidelines(),
            "max_message_chars": self.config.max_message_chars,
            "inject_recall": self.config.inject_recall,
            "max_memories": self.config.max_memories,
            "max_chars_per_memory": self.config.max_chars_per_memory,
            "max_total_recall_chars": self.config.max_total_recall_chars,
            "search_timeout": self.config.search_timeout,
            "recall_min_score": self.config.recall_min_score,
            "rewrite_every_n_steps": self.config.rewrite_every_n_steps,
            "rewrite_max_consecutive_errors": self.config.rewrite_max_consecutive_errors,
            "rewrite_model": "",  # filled at start when rewriting is enabled
            "rewrite_base_url": "",  # filled sanitized at start (rule 4)
            "rewrite_timeout": self.config.rewrite_timeout,
            "rewrite_max_tokens": self.config.rewrite_max_tokens,
            "strict": self.config.strict,
        }

    @abstractmethod
    def _resolve_settings(self) -> dict:
        """The resolved connection settings, or raise ``_BackendUnavailable``
        with an integration-owned message when unusable."""

    def _resolve_rewrite_settings(self) -> dict | None:
        """The rewriter connection, or None when rewriting is disabled.

        With ``rewrite_every_n_steps > 0`` all three fields must resolve
        (config wins over the ``MEMORY_QUERY_*`` env fallbacks, which the
        driver fills from the QUERY proxy lane) — the same fail-closed
        settings check EXTRACT uses."""
        if self.config.rewrite_every_n_steps <= 0:
            return None
        model = _config_or_env(self.config.rewrite_model, "MEMORY_QUERY_MODEL")
        base_url = _config_or_env(self.config.rewrite_base_url, "MEMORY_QUERY_MODEL_URL")
        api_key = _config_or_env(self.config.rewrite_api_key, "MEMORY_QUERY_API_KEY")
        if not model or not base_url or not api_key:
            raise _BackendUnavailable(
                "incomplete rewrite settings with rewrite_every_n_steps > 0: need model/base_url/api_key "
                "via agent.memory.rewrite_* or the MEMORY_QUERY_MODEL/MEMORY_QUERY_MODEL_URL/"
                "MEMORY_QUERY_API_KEY env"
            )
        return {"model": model, "base_url": base_url, "api_key": api_key}

    @abstractmethod
    def _startup(self, settings: dict) -> None:
        """Fill computed settings keys, construct/start the integration system,
        and mint ``self._session_id`` via ``_new_session_id``."""

    def _reset_extras(self) -> None:
        """Reset integration-owned per-start state (default: nothing)."""

    def _start_event_extras(self) -> dict:
        """Extra fields for the start event (default: none)."""
        return {}

    def _on_started(self, settings: dict) -> None:
        """Runs after the start event: the base's trace setup."""
        self._trace_setup(settings)

    def set_task(self, task: str) -> None:
        """Store the episode task text (the initial recall query).

        Opens the trace session when traced: a portable ``trace_session_id``,
        one ``memory_session`` with the exact task on the main endpoint, then
        both logical role bindings — main on the main endpoint, memory on the
        memory endpoint — before any operation annotation.
        """
        self._task = task
        self._current_query = task
        # A fresh episode with a cold cache must search, never reuse "nothing"
        # (the run-scope store is already full when instance 2 begins).
        self._query_source = "task"
        self._cached_recall = None
        self._cached_anchor = None
        self._search_dirty = True
        trace = self._trace
        if trace is None or not self._available:
            return
        try:
            adapter = self._adapter_meta()
            trace.trace_session_id = str(uuid4())
            session = self._event(
                "memory_session",
                {
                    "trace_session_id": trace.trace_session_id,
                    "session_kind": "episode",
                    "adapter": adapter,
                    "instance_id": self.instance_id,
                    "task": inline_text_ref(task),
                    "extensions": {adapter["name"]: self._trace_context()},
                },
            )
            bind_main = self._event(
                "memory_role_bind",
                {"trace_session_id": trace.trace_session_id, "logical_role": "main", "adapter": adapter},
            )
            bind_memory = self._event(
                "memory_role_bind",
                {"trace_session_id": trace.trace_session_id, "logical_role": "memory", "adapter": adapter},
            )
            result = trace.annotator.post(trace.main_url, [session, bind_main])
            if result.ok:
                result = trace.annotator.post(trace.memory_url, [bind_memory])
            if result.ok:
                trace.session_open = True
            else:
                logger.warning("memory annotation session was not accepted; tracing disabled for this episode")
                self._trace = None
        except Exception:
            logger.exception("memory annotation session failed; tracing disabled")
            self._trace = None

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record(self, messages: list[dict], step: int) -> None:
        """Normalize and store trajectory messages for extraction visibility."""
        # Dormant after finalize (the teardown policy above) — no counting,
        # no events, no handle access.
        if not self._available or self._finalized:
            return
        for message in messages:
            if not isinstance(message, dict):
                continue
            # Defense in depth: the transient recall marker is appended directly
            # to agent.messages and bypasses add_messages; never record it.
            if message.get("extra", {}).get("transient_recall"):
                continue
            try:
                text = _truncate(self._message_text(message), self.config.max_message_chars)
                if not self._should_store(text):
                    continue
                role = self._normalize_role(str(message.get("role") or "user"))
                message_id = self._store_message(role, text, step)
                self._counts["messages_recorded"] += 1
                self._recent_messages.append((role, text))  # the rewriter's bounded progress view
                trace = self._trace
                if trace is not None and trace.session_open and trace.memory_lane_enabled:
                    # Pending generation input: retained across extraction
                    # failures, cleared only when a successful extraction
                    # advances the integration's own checkpoint rule.
                    trace.pending_inputs.append(
                        {
                            "message_id": message_id if message_id is not None else self._counts["messages_recorded"],
                            "role": role,
                            "step": step,
                            "content": text,
                        }
                    )
            except Exception as e:
                self._counts["backend_errors"] += 1
                self._log_event("error", op="record", step=step, error=str(e))
                logger.exception("failed to record trajectory message")
                if self.config.strict:
                    raise

    @staticmethod
    def _message_text(message: dict) -> str:
        """Extraction-visible text: content plus parsed tool-call actions.

        Stock tool-call assistant turns have ``content=None``; coercing them to
        "" would make nearly every assistant action extraction-invisible, so
        parsed ``extra.actions`` are appended as a compact Actions: block. Raw
        ``extra.response`` data is never copied.
        """
        content = message.get("content") or ""
        parts = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    else:
                        # short placeholder only — never raw image/binary data
                        parts.append(f"[{block.get('type', 'unknown')}]")
                else:
                    parts.append(str(block))
        else:
            parts.append(str(content))
        actions = message.get("extra", {}).get("actions")
        if actions:
            parts.append("Actions:\n" + json.dumps(actions, ensure_ascii=False, default=str))
        return "\n".join(part for part in parts if part)

    def _normalize_role(self, role: str) -> str:
        """Map a trajectory role onto a storable role (default: identity)."""
        return role

    def _should_store(self, text: str) -> bool:
        """Whether the normalized text is worth storing (default: always)."""
        return True

    @abstractmethod
    def _store_message(self, role: str, text: str, step: int):
        """Persist or buffer one normalized message. The base's availability
        guard is the only preflight — no private handle check belongs here.
        Returns the native message id, or None: the base then numbers the
        pending generation input synthetically (the recorded-message count)."""

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------
    def maybe_extract(self, step: int) -> None:
        """Periodic tick with a high-water bucket mark.

        Fires when ``step // N`` exceeds the last attempted bucket, so a
        boundary missed by a format/flow exception is serviced on the next
        clean step instead of waiting for the next multiple.
        """
        if not self._available or self._finalized:
            return
        interval = self.config.extract_every_n_steps
        if interval <= 0 or self._extract_breaker:
            return
        bucket = step // interval
        if bucket > self._last_extract_bucket:
            self._last_extract_bucket = bucket
            self._extract(step)

    def _extract(self, step) -> None:
        """One extraction attempt. The final flush (``finalize``) calls this
        directly and bypasses the breaker by design — only ``maybe_extract``
        consults it."""
        if not self._available:
            return
        calls_before = self._counts["extraction_calls"]
        errors_before = self._counts["extraction_errors"]
        try:
            self._perform_extraction(step)
        except Exception as e:
            self._counts["extraction_errors"] += 1
            # A counted tick that FAILED may still have written: a hosted
            # write-then-poll-timeout stores server-side before raising, and a
            # local mid-write failure leaves partial rows. Invalidate the
            # recall cache conservatively — otherwise the next recall keeps
            # serving the memoized pre-write payload, and with repeated
            # failures (the breaker) it stays stale to the episode's end. The
            # calls half of the predicate is load-bearing here too: a raise
            # from the readiness guard precedes any possible write.
            if self._counts["extraction_calls"] > calls_before:
                self._mark_store_changed()
            self._register_extraction_failure(step, str(e))
            if self.config.strict:
                raise
            return
        # A store write may have changed what the next recall search returns,
        # so every counted extract tick marks the search cache dirty — failed
        # ones above, clean ones here. The calls half of the predicate is
        # load-bearing: both integrations' readiness guards return BEFORE
        # counting, so an errors-only rule is vacuously true on skipped ticks
        # and would buy a pointless search per boundary. An attempted, clean
        # extraction always marks — including an empty-candidate success (the
        # accepted over-invalidation is ~extraction_calls+1 searches per
        # episode, exactly the win).
        if self._counts["extraction_calls"] > calls_before and self._counts["extraction_errors"] == errors_before:
            self._mark_store_changed()

    def _mark_store_changed(self) -> None:
        """Mark the recall-search cache dirty: the next ``recall_context``
        re-searches instead of serving the cached payload. The base calls this
        after every counted extract tick — clean (a store write may have
        changed the search result) and failed alike (a write may have landed
        before the failure, e.g. a hosted write-then-poll-timeout);
        integrations may additionally call it after native writes outside the
        extract path."""
        self._search_dirty = True

    @abstractmethod
    def _perform_extraction(self, step) -> None:
        """Integration-owned extraction work. Contract:

        - Readiness guard first, BEFORE any counting (e.g. missing handle or
          empty buffer): an unready tick is not a counted call — and posts no
          traced operation either.
        - ``extraction_calls += 1`` after the guard.
        - Traced episodes: ``_generation_begin(step)`` after counting (its
          failure is contained on the integration side: log and continue with
          ``operation=None``); a native hard failure closes the operation with
          ``_generation_finish_exception`` (contained) before re-raising; a
          completed native call closes it with ``_generation_finish`` (also
          contained), passing the native soft-error list (empty when clean).
        - Hard failure: re-raise WITHOUT counting or consulting strict — the
          ``_extract`` shell counts (``extraction_errors``), registers the
          failure (``log_event=True``), and applies the strict gate.
        - Soft failure (an error result, no exception): handled inside — count
          ``extraction_errors``, register with ``log_event=False``, never raise.
        - Success predicates and buffer/checkpoint management stay
          integration-owned; a success resets ``self._consecutive_errors`` and
          clears the trace's pending inputs under the integration's own
          checkpoint rule.
        """

    def _register_extraction_failure(self, step, error: str, *, log_event: bool = True) -> None:
        self._consecutive_errors += 1
        if log_event:
            self._log_event("error", op="extract", step=step, error=error)
        limit = self.config.extract_max_consecutive_errors
        if limit > 0 and self._consecutive_errors >= limit and not self._extract_breaker:
            self._extract_breaker = True
            logger.error(
                "extraction breaker tripped after %d consecutive errors; "
                "periodic ticks are disabled for this episode (final flush still runs)",
                self._consecutive_errors,
            )
            self._log_event(
                "error",
                op="extract_breaker",
                step=step,
                error=f"breaker tripped after {self._consecutive_errors} consecutive extraction errors",
            )

    # ------------------------------------------------------------------
    # Query rewrite
    # ------------------------------------------------------------------
    def maybe_rewrite(self, step: int) -> None:
        """Periodic query-rewrite tick with the same high-water bucket pattern
        as ``maybe_extract``. The agent calls it from ``step()`` (after that
        step's ``query()``), so its duration is consumed at the next
        ``query()`` — and no rewrite happens before the first step of an
        episode (there is no progress to read; the query is the task text)."""
        if not self._available or self._finalized:
            return
        interval = self.config.rewrite_every_n_steps
        if interval <= 0 or self._rewrite_breaker:
            return
        bucket = step // interval
        if bucket > self._last_rewrite_bucket:
            self._last_rewrite_bucket = bucket
            try:
                self._rewrite_query(step)
            except Exception as e:
                # The same fail-closed shell as _extract: an unexpected rewrite
                # error keeps the old query and never reaches the agent loop.
                self._counts["rewrite_failures"] += 1
                self._log_event("rewrite", step=step, error=str(e))
                logger.exception("query rewrite failed")
                self._register_rewrite_failure(step)
                if self.config.strict:
                    raise

    def _rewrite_query(self, step: int) -> None:
        """One rewrite attempt; never raises (the agent loop is never exposed).

        On success the recall query is replaced and the search cache marked
        dirty; on ANY failure (transport, timeout, envelope violation, empty
        content) the old query stays and the flag is left untouched.
        """
        settings = self._rewrite_settings
        if settings is None:
            return  # rewriting disabled (the start-time guard resolved it)
        self._counts["rewrite_calls"] += 1
        cfg = SideModelConfig(
            model=settings["model"],
            base_url=settings["base_url"],
            api_key=settings["api_key"],
            timeout=self.config.rewrite_timeout,
            max_tokens=self.config.rewrite_max_tokens,
        )
        call = StructuredCall(model=RewrittenQuery, messages=self._rewrite_messages())
        started = time.monotonic()
        try:
            result = call_structured(cfg, call)
        finally:
            # The rewrite is a model call on a side lane: its seconds join the
            # same backend-owned wall-clock exemption as the recall search.
            self._io_duration += time.monotonic() - started
        if result.error is not None or result.value is None:
            self._counts["rewrite_failures"] += 1
            self._log_event("rewrite", step=step, error=result.error or "no_value")
            self._register_rewrite_failure(step)
            return
        self._current_query = result.value.query
        self._query_source = "rewritten"
        self._counts["rewrite_successes"] += 1
        self._rewrite_consecutive_errors = 0
        self._log_event("rewrite", step=step, query=result.value.query)
        self._mark_store_changed()

    def _register_rewrite_failure(self, step) -> None:
        """The rewriter's breaker, mirroring the extraction one: a permanently
        dead QUERY lane must not keep paying ``rewrite_timeout`` every
        boundary to the episode's end. The failure event itself is already
        logged by the caller (both rewrite failure grains are)."""
        self._rewrite_consecutive_errors += 1
        limit = self.config.rewrite_max_consecutive_errors
        if limit > 0 and self._rewrite_consecutive_errors >= limit and not self._rewrite_breaker:
            self._rewrite_breaker = True
            logger.error(
                "rewrite breaker tripped after %d consecutive errors; "
                "rewrites are disabled for this episode (the recall query stays as-is)",
                self._rewrite_consecutive_errors,
            )
            self._log_event(
                "error",
                op="rewrite_breaker",
                step=step,
                error=f"breaker tripped after {self._rewrite_consecutive_errors} consecutive rewrite errors",
            )

    def _rewrite_messages(self) -> list[dict]:
        """The rewriter input: the task text plus the ring buffer. The task
        rides the same recording cap and truncation marker as ``record()``
        (``_task``/``_current_query``/``memory_session`` stay full-length —
        only this prompt view is capped)."""
        task = _truncate(self._task or "", self.config.max_message_chars)
        recent = "\n\n".join(f"[{role}]\n{text}" for role, text in self._recent_messages)
        return [
            {"role": "system", "content": QUERY_REWRITE_PROMPT},
            {"role": "user", "content": query_rewrite_user_message(task, recent)},
        ]

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------
    def recall_context(self, planned_step=None) -> dict | None:
        """Render the transient recall payload, or None when nothing is injected.

        A dirty-flag cache fronts the search: the result can only change when
        the query is replaced or the store is written, so a clean flag reuses
        the memoized payload (an injected copy carrying ``cached: True`` —
        ``note_recall`` counts it as ``recall_cache_hits`` at delivery, the
        same point ``recall_injections`` counts, so the pair stays comparable)
        or the memoized empty answer (None) instead of searching again. The flag is set by ``start``/``set_task``,
        a successful rewrite, and every counted extract tick
        (``_mark_store_changed`` — clean or failed: a failed extraction may
        have written, e.g. a hosted write-then-poll-timeout); a failed search is never cached — the flag
        stays set so the next step retries.

        The shared loop order is: relevance floor (``recall_min_score`` over
        ``_hit_score`` — hits without a score drop too) -> ``max_memories``
        slice -> rank-then-fill against the two budget knobs. A floor applied
        after the slice could only under-fill, so filtering always comes first.
        Rank-then-fill walks the lines in ranking order: each line is first
        truncated to ``max_chars_per_memory`` (0 = off), then delivered whole
        when it fits the remaining ``max_total_recall_chars`` budget (0 = no
        total bound), truncated to fit when at least
        ``_MIN_TRUNCATED_RECALL_LINE_CHARS`` remain, or skipped when less
        remains — and the walk CONTINUES in every case, so one oversized
        memory does not starve the smaller lines ranked below it (a recorded
        divergence from native's break-on-exhaustion). A ``_hit_budget_exempt``
        line delivers in full outside both budgets (the native scope rule:
        the budget governs memory lines only) but still occupies a
        ``max_memories`` slot. The
        budget counts the rendered memory LINES only — the header is excluded,
        so growing the policy text never shrinks the memory budget (the
        payload's ``chars`` keeps meaning "what was placed", header included).
        An empty hit set — or a budget too small for any selected line —
        injects nothing at all rather than spamming a bare header on every
        model call. ``n_memories`` counts delivered lines, not the selected
        candidate count.

        The payload's ``memories`` carries the integration's native hit objects
        so a later ``deliver_recall`` can cite the same refs; the element type
        is integration-defined (part of the cross-method contract). ``origins``
        carries the per-hit ``_hit_origin`` values the rendered lines'
        provenance suffixes were derived from.
        """
        # Dormant after finalize, BEFORE the tracing hooks: a stray recall
        # must not post a fabricated search operation (or drop an undelivered
        # search anchor) for a search that will never run.
        if not self._available or self._finalized:
            return None
        if not self.config.inject_recall or self._task is None:
            return None
        if not self._search_dirty:
            # Clean cache: serve the memoized answer — no search runs, no
            # search events post, and a later delivery cites the search the
            # payload was rendered from (the memoized anchor).
            if self._cached_recall is not None:
                return {**self._cached_recall, "cached": True}
            return None
        token = None
        try:
            token = self._recall_search(planned_step)
        except Exception:
            # A failing tracing hook must not block native recall.
            logger.exception("annotation search-begin failed; recall continues untraced")
        try:
            # The native search can be a hosted call: its seconds accrue to the
            # backend-owned accumulator the agent's wall-clock preflight exempts.
            started = time.monotonic()
            try:
                hits = self._search()
            finally:
                self._io_duration += time.monotonic() - started
            matched = len(hits)
            floor = self.config.recall_min_score
            if floor is not None:
                hits = [hit for hit in hits if (score := self._hit_score(hit)) is not None and score >= floor]
            selected = hits[: self.config.max_memories]
            if not selected:
                self._recall_rendered_contained(token, status="completed", rendered=[], matched=matched, selected=0)
                # An empty answer is a valid answer: cache it like any other
                # (the store and query are unchanged, so re-searching next
                # step would just repeat it).
                self._cached_recall = None
                self._cached_anchor = None
                self._search_dirty = False
                return None
            body = ""
            budget = 0  # chars the non-exempt lines consumed of max_total_recall_chars
            rendered: list = []
            origins: list = []
            for hit in selected:
                line = self._render_line(hit)
                if not line:
                    continue
                origin = self._hit_origin(hit)
                line = f"{line}{self._origin_suffix(origin)}"
                if self._hit_budget_exempt(hit):
                    # Exempt lines render in full and consume no budget (the
                    # native scope rule: the budget governs memory lines
                    # only) — but still occupy a max_memories slot.
                    body = f"{body}\n{line}" if body else line
                    rendered.append(hit)
                    origins.append(origin)
                    continue
                per_memory = self.config.max_chars_per_memory
                if per_memory and len(line) > per_memory:
                    # Per-memory cap FIRST (the native order), suffix-bearing:
                    # the provenance suffix is part of the line, so it is cut
                    # together with its content, never left standing alone. The
                    # configured cap is honored exactly (no silent floor): a cap
                    # too small to fit the marker takes a plain cut instead.
                    if per_memory > len(RECALL_LINE_TRUNCATION):
                        line = line[: per_memory - len(RECALL_LINE_TRUNCATION)].rstrip() + RECALL_LINE_TRUNCATION
                    else:
                        line = line[:per_memory]
                # The "\n" separators count (native): one whenever the body
                # already holds a line — a budget-EXEMPT one included, so the
                # separator joining an exempt line to the first budgeted line
                # never pushes the placed chars one past the total.
                cost = len(line) + (1 if body else 0)
                total = self.config.max_total_recall_chars
                if total and budget + cost > total:
                    remaining = total - budget - (1 if body else 0)
                    if remaining < _MIN_TRUNCATED_RECALL_LINE_CHARS:
                        # Too little room to truncate into: skip the line and
                        # keep walking — one oversized memory must not starve
                        # the smaller below-rank lines (nor get the empty
                        # result cached).
                        continue
                    # Truncate to fit the remaining budget (the native
                    # recall-budget shape) — but keep WALKING afterwards:
                    # a recorded divergence from native's break-on-exhaustion.
                    line = line[: remaining - len(RECALL_LINE_TRUNCATION)].rstrip() + RECALL_LINE_TRUNCATION
                    cost = len(line) + (1 if body else 0)
                body = f"{body}\n{line}" if body else line
                budget += cost
                rendered.append(hit)
                origins.append(origin)
            if not rendered:
                self._recall_rendered_contained(token, status="completed", rendered=[], matched=matched, selected=len(selected))
                self._cached_recall = None
                self._cached_anchor = None
                self._search_dirty = False
                return None
            content = f"{self._recall_header()}\n{body}"
            self._recall_rendered_contained(token, status="completed", rendered=rendered, matched=matched, selected=len(selected))
            payload = {
                "content": content,
                "n_memories": len(rendered),
                "chars": len(content),
                "memories": rendered,
                "origins": origins,
            }
            # Memoize the payload together with its delivery anchor — but only
            # when THIS search armed it: a payload whose own search was not
            # fully recorded gets a None anchor, so its cached deliveries never
            # cite an older search whose returned set may differ.
            trace = self._trace
            anchor = token if trace is not None and token is not None and trace.pending_search is token else None
            self._cached_recall = payload
            self._cached_anchor = anchor
            self._search_dirty = False
            return payload
        except Exception as e:
            # A failed search is never cached: recall returns None for this
            # step and the flag stays set so the next step retries.
            self._recall_rendered_contained(token, status="failed", rendered=[], matched=0, selected=0, error=e)
            self._counts["backend_errors"] += 1
            self._log_event("error", op="recall", error=str(e))
            logger.exception("memory recall failed")
            if self.config.strict:
                raise
            return None

    @abstractmethod
    def _search(self) -> list:
        """The native recall search over stored memories for
        ``self._recall_query()``. Private readiness guard first (return [] when
        the handle is gone), then private counters, then the call; failures
        re-raise after private error counting (``search_errors`` — the base
        owns ``backend_errors`` and the op=recall event)."""

    def _recall_query(self) -> str | None:
        """The current recall query: the task text until the rewrite tick
        replaces it. Integrations' ``_search()`` read the query from this hook
        so query strategy can vary without forking the integration."""
        return self._current_query if self._current_query is not None else self._task

    def _hit_score(self, hit) -> float | None:
        """The hit's relevance score on the integration's scale (default None:
        the integration exposes no score — a configured floor drops the hit)."""
        return None

    def _hit_origin(self, hit) -> str | None:
        """The origin episode's session id for one hit (default None: the
        integration exposes no origin signal, so the line gets no suffix)."""
        return None

    def _hit_budget_exempt(self, hit) -> bool:
        """Whether the hit's line renders outside both recall budgets (default
        False). The native scope rule as a seam: the budget governs memory
        lines only, so an integration's non-memory layer (e.g. a persona
        pseudo-hit) delivers in full — never truncated by either knob and
        excluded from the ``max_total_recall_chars`` accounting. Exemption
        does NOT bypass the ``max_memories`` slice: the line still occupies
        one delivered-line slot."""
        return False

    def _matched_precision(self) -> str:
        """Precision of the search-end ``matched_count``: whether the raw hit
        count is the TRUE match total (``"exact"``) or a floor on it
        (``"lower_bound"``). The base cannot know whether the native search
        truncated internally — a top-k/limit-bounded hosted API makes the
        total match count unknowable — so the conservative default is
        ``"lower_bound"``; an integration whose native search is an unbounded
        full scan (every match returned) overrides with ``"exact"``."""
        return "lower_bound"

    def _origin_suffix(self, origin: str | None) -> str:
        """The provenance suffix for one rendered line. The comparison against
        the current episode's session id is base-owned (``_new_session_id``
        mints ``<instance_id>-<uuid4hex>``, so the instance parses straight
        out); an origin that does not parse names no episode."""
        if not origin:
            return ""
        if origin == self._session_id:
            return " (from this episode)"
        instance = _origin_instance(origin)
        if instance is not None:
            return f" (from earlier episode {instance})"
        return " (from an earlier episode)"

    def _extraction_guidelines(self) -> str:
        """The extraction guidelines this integration conveys to its extraction
        engine: the policy layer (the run's override replacing the shared
        default wholesale — an override IS the policy, never default+override)
        plus the base-composed episode context naming this instance and its
        repository key, which survives an override because it is episode fact,
        not policy. "" when the integration declared no channel
        (``_CONVEYS_EXTRACTION_GUIDELINES``). Pure string work: resolution
        cannot fail at extraction time."""
        if not self._CONVEYS_EXTRACTION_GUIDELINES:
            return ""
        policy = self.config.extraction_guidelines.strip() or EXTRACTION_GUIDELINES_DEFAULT
        return f"{policy}\n\n{extraction_episode_context(self.instance_id, _repo_of(self.instance_id))}"

    def _recall_policy(self) -> str:
        """The shared recall-policy preamble (do-not-respond + provenance
        trust). Integrations compose it into their header; they never replace
        it."""
        return RECALL_POLICY_DEFAULT

    def _recall_header(self) -> str:
        """The rendered block's header: the shared policy preamble plus the
        integration's section text. Integrations compose by implementing
        ``_recall_sections`` — a wholesale override would drop the policy's
        "do not respond" sentence, which is the point of the shared policy."""
        return f"{self._recall_policy()}\n\n{self._recall_sections()}"

    @abstractmethod
    def _recall_sections(self) -> str:
        """The integration's recall-header section text (titles + lead-ins);
        the strings live in the integration's prompts module."""

    @abstractmethod
    def _render_line(self, hit) -> str:
        """One hit as one whole memory line; "" skips the hit."""

    def _recall_search(self, planned_step):
        """Begin a traced search: post memory_search_start with the exact
        recall query and return the operation token (None = untraced).
        Contained by the base: a raise here never blocks native recall."""
        trace = self._trace
        if trace is None or not trace.session_open or not trace.memory_lane_enabled:
            return None
        trace.pending_search = None  # a new search supersedes any undelivered one
        operation = _OperationTrace(str(uuid4()))
        payload = {
            "trace_session_id": trace.trace_session_id,
            "operation_id": operation.operation_id,
            "requested_by": "main",
            "handled_by": "memory",
            "query": inline_text_ref(self._recall_query() or ""),
            "extensions": {
                self._adapter_meta()["name"]: {
                    "session_id": self._session_id,
                    "planned_step": str(planned_step),
                    # Query provenance without any schema change: "task" (the
                    # episode task text) or "rewritten" (the rewrite tick).
                    "query_source": self._query_source,
                }
            },
        }
        if isinstance(planned_step, int):
            payload["main_step"] = planned_step
        result = trace.annotator.post(trace.memory_url, [self._event("memory_search_start", payload)])
        self._bind_start_result(operation, result, planned_step, "search_start")
        return operation

    def _recall_rendered(self, token, *, status: str, rendered: list, matched: int, selected: int, error=None) -> None:
        """Close the traced search on every recall exit and arm the delivery
        anchor. Contained by the base: a raise here never reaches the native
        recall path, so a tracing failure can neither drop a computed recall
        nor post a second, corrupting search_end for a search that already
        completed."""
        result = self._search_finish(token, status=status, rendered=rendered, matched=matched, selected=selected, error=error)
        # Only a completed, non-empty search whose start AND end posts both
        # landed may anchor a later delivery: citing a search the recorder
        # never fully recorded would dangle (a non-None result already implies
        # a live trace and an enabled token).
        if (
            status == "completed"
            and rendered
            and result is not None
            and result.ok
            and token.start_accepted
        ):
            self._trace.pending_search = token
        elif token is not None and not token.disabled and status == "completed" and rendered:
            # A search that DID deliver could not anchor: its start or end
            # post did not land, so the trajectory will show neither this
            # search nor the deliveries citing it while memory.json counts
            # them — the under-recording the annotation events exist to flag.
            # (The 413/409 starts that disable the operation log their own
            # reason and are excluded here.)
            self._log_event(
                "annotation", op="search", operation_id=token.operation_id, reason="annotation_search_unconfirmed"
            )

    def _search_finish(self, operation, *, status: str, rendered: list, matched: int, selected: int, error=None) -> PostResult | None:
        """Post memory_search_end with the exact ordered rendered refs.

        Returns the post outcome so the caller can tell a landed end event
        from a rejected/unconfirmed one; None means nothing was posted."""
        if operation is None or operation.disabled:
            return None
        trace = self._trace
        if trace is None:
            return None
        binding = None
        if operation.cursor is not None:
            binding = {"after_role_call_index": operation.cursor}
        payload = {
            "trace_session_id": trace.trace_session_id,
            "operation_id": operation.operation_id,
            "requested_by": "main",
            "handled_by": "memory",
            "status": status,
            "returned": [
                {"ordinal": index, "memory": self._memory_ref(memory)}
                for index, memory in enumerate(rendered)
            ],
            "error_codes": [] if error is None else [type(error).__name__],
            "extensions": {
                self._adapter_meta()["name"]: {
                    "session_id": self._session_id,
                    "matched": matched,
                    "selected": selected,
                    "rendered": len(rendered),
                    "budget_dropped": selected - len(rendered),
                }
            },
        }
        if status == "completed":
            # The portable match count, kept apart from `returned` so analysis
            # can tell "the system found little" from "policy dropped the
            # rest". Only a completed search has one: the failed path's
            # matched is a placeholder 0, not a real count.
            payload["matched_count"] = {"value": matched, "precision": self._matched_precision()}
        end = self._event("memory_search_end", payload, binding=binding)
        return trace.annotator.post(trace.memory_url, [end])

    def _recall_rendered_contained(self, token, **kwargs) -> None:
        """Fire ``_recall_rendered`` under the annotation failure discipline:
        a raising hook is logged and dropped, never routed into the native
        failure envelope (which would count a backend error for a search that
        natively succeeded)."""
        try:
            self._recall_rendered(token, **kwargs)
        except Exception:
            logger.exception("annotation search-end failed; recall continues untraced")

    # ------------------------------------------------------------------
    # Trajectory annotation (schema v6 memory protocol)
    # ------------------------------------------------------------------
    @abstractmethod
    def _adapter_meta(self) -> dict:
        """``{"name", "version"}`` for the session/bind events; ``name`` also
        keys every traced payload's ``extensions.<name>`` block."""

    @abstractmethod
    def _memory_ref(self, obj) -> dict:
        """One native row/hit as a portable MemoryRef (version_id /
        identity_strength / identity_scheme / item_id / namespace / content /
        extensions)."""

    @abstractmethod
    def _trace_namespace(self) -> str:
        """The store-scope namespace stamped on every traced MemoryRef."""

    def _memory_lane_url_source(self, settings: dict) -> str:
        """The memory lane's model base URL for annotate-URL derivation
        (default: "" — the lane carries no model traffic, so its endpoint must
        come from explicit config/env)."""
        return ""

    def _trace_context(self) -> dict:
        """The session event's ``extensions.<name>`` payload (default: empty)."""
        return {}

    def _snapshot_memory_state(self) -> dict | None:
        """The extraction-visible state for the generation change audit, or
        None when unavailable (default: no snapshots — ``_attribute_changes``
        must then work from native receipts alone)."""
        return None

    def _attribute_changes(self, operation: _OperationTrace, result, after: dict | None):
        """``(changes, produced, unexplained)`` for one generation.

        ``result`` is the integration's native extraction result (None on the
        exception path, where only the snapshot diff is knowable). The default
        is a generic observed diff of the two snapshots — create/delete by
        key, update by content digest — for integrations whose native result
        cannot attribute changes; integrations with native receipts or a
        replay audit override it.
        """
        before = operation.before
        if before is None or after is None:
            return [], [], []
        changes: list[dict] = []
        produced: list[dict] = []
        for key, after_row in after.items():
            before_row = before.get(key)
            if before_row is None:
                changes.append(self._change_payload(operation, "create", [], [after_row]))
                produced.append(self._memory_ref(after_row))
            elif (
                self._memory_ref(before_row)["content"].get("sha256")
                != self._memory_ref(after_row)["content"].get("sha256")
            ):
                changes.append(self._change_payload(operation, "update", [before_row], [after_row]))
                produced.append(self._memory_ref(after_row))
        for key, before_row in before.items():
            if key not in after:
                changes.append(self._change_payload(operation, "delete", [before_row], []))
        return changes, produced, []

    def _generation_end_context(self, step, result, audit: dict) -> dict:
        """The generation end event's ``extensions.<name>`` payload — the one
        event family the base cannot derive (native counts/errors ride
        ``result``; ``audit`` carries clean/unexplained/checkpoint)."""
        return {}

    def _trace_setup(self, settings: dict) -> None:
        """Resolve both lanes' annotate endpoints and build the annotator.

        Resolution per lane: explicit config, then the MEMORY_ANNOTATE_* env
        override, then derivation from the lane's effective model base URL
        (only the memory lane may resolve from the explicit URL alone — a
        hosted-extraction lane carries no model traffic). Normal provider
        URLs resolve to nothing; any mismatch disables tracing with one clear
        log entry. Never raises: observability must not change native
        behavior, even under memory strict=true.
        """
        try:
            if not self.config.annotate:
                return
            main_url = resolve_lane_url(
                self.config.annotate_main_url,
                os.environ.get("MEMORY_ANNOTATE_MAIN_URL", ""),
                self._model_base_url,
            )
            memory_url = resolve_lane_url(
                self.config.annotate_memory_url,
                os.environ.get("MEMORY_ANNOTATE_MEMORY_URL", ""),
                self._memory_lane_url_source(settings),
                # The memory lane of a hosted-extraction integration carries no
                # model traffic, so only its explicit URL can resolve it. The
                # main lane always carries the benchmark model: an explicit
                # URL with no model URL to check against is a stale export.
                allow_no_model_url=True,
            )
            if main_url is None and memory_url is None:
                return  # provider URLs carry no trajectory scope: untraced, no noise
            if main_url is None or memory_url is None:
                lane = "main" if main_url is None else "memory"
                logger.warning(
                    "memory annotation disabled: the %s lane has no trajectory-scoped annotate endpoint", lane
                )
                return
            error = endpoints_compatible(main_url, memory_url)
            if error:
                logger.warning("memory annotation disabled: %s", error)
                return
            self._trace = _TraceState(
                Annotator(
                    timeout=self.config.annotate_timeout,
                    retries=self.config.annotate_retries,
                    max_consecutive_errors=self.config.annotate_max_consecutive_errors,
                    # The breaker's open transition is the one moment a dead
                    # transport becomes knowable; without this record a
                    # transport-dead session would show zero annotation
                    # events while its trajectory records nothing at all.
                    on_breaker=lambda: self._log_event(
                        "annotation", op="transport", reason="annotation_transport_disabled"
                    ),
                ),
                main_url,
                memory_url,
            )
            # The namespace names this arm's shared store scope (the
            # integration hashes its store locator into it).
            self._namespace = self._trace_namespace()
            logger.info(
                "memory annotation enabled (main=%s memory=%s)",
                sanitize_url(main_url),
                sanitize_url(memory_url),
            )
        except Exception:
            logger.exception("memory annotation setup failed; tracing disabled")
            self._trace = None

    @staticmethod
    def _event(event_type: str, payload: dict, binding: dict | None = None) -> dict:
        event = {"type": event_type, "annotation_id": str(uuid4()), "payload": payload}
        if binding is not None:
            event["binding"] = binding
        return event

    # ------------------------------------------------------------------
    # Generation tracing
    # ------------------------------------------------------------------
    def _generation_begin(self, step) -> _OperationTrace | None:
        """Post memory_generate_start with the pending normalized inputs and
        snapshot the extraction-visible state before any work runs.

        A long retained backlog (extraction failures retain pending inputs,
        or a final-only flush) can push one start event over the recorder's
        1 MiB body cap; rather than let the 413 take the whole extraction
        untraced, an oversize start degrades every input to its digest (the
        schema's unavailable-ref form — identity kept, bulk dropped)."""
        trace = self._trace
        if trace is None or not trace.session_open or not trace.memory_lane_enabled:
            return None
        operation = _OperationTrace(str(uuid4()))
        payload = {
            "trace_session_id": trace.trace_session_id,
            "operation_id": operation.operation_id,
            "requested_by": "main",
            "handled_by": "memory",
            "trigger": "final" if step == "final" else "step",
            "inputs": [
                {
                    "input_id": str(item["message_id"]),
                    "kind": "message",
                    "role": item["role"],
                    "source_step": item["step"],
                    "content": inline_text_ref(item["content"]),
                }
                for item in trace.pending_inputs
            ],
            "extensions": {
                self._adapter_meta()["name"]: {"session_id": self._session_id, "extraction_step": str(step)}
            },
        }
        if isinstance(step, int):
            payload["main_step"] = step
        operation.input_count = len(payload["inputs"])
        event = self._event("memory_generate_start", payload)
        if len(json.dumps({"events": [event]})) > _MAX_POST_BYTES:
            for item in payload["inputs"]:
                item["content"] = {
                    "availability": "unavailable",
                    "reason": "oversize",
                    "sha256": item["content"]["sha256"],
                }
            event = self._event("memory_generate_start", payload)
            self._log_event(
                "annotation",
                op="generate_start",
                step=step,
                operation_id=operation.operation_id,
                reason="annotation_inputs_degraded",
            )
        result = trace.annotator.post(trace.memory_url, [event])
        self._bind_start_result(operation, result, step, "generate_start")
        if not operation.disabled:
            operation.before = self._snapshot_memory_state()
        return operation

    def _bind_start_result(self, operation: _OperationTrace, result: PostResult, step, op_name: str) -> None:
        """Classify an operation-start post: bind at the returned cursor, or
        disable/recover/leave ambiguous. Shared by generation and search."""
        operation.start_accepted = result.ok
        if result.ok and result.cursor is not None:
            # The start response's lane cursor: only decision calls after this
            # point may bind to this operation — never an older call.
            operation.cursor = result.cursor
        elif result.status == 413:
            # A pathological final flush can approach the 1 MiB body cap. The
            # rejection is definitive: post nothing for this operation, run the
            # native work unchanged, and leave a credential-free record.
            operation.disabled = True
            self._log_event(
                "annotation",
                op=op_name,
                step=step,
                operation_id=operation.operation_id,
                reason="annotation_start_oversize",
            )
            logger.warning(
                "annotation_start_oversize: %s start for operation %s (step %s) was rejected "
                "with 413; this operation continues untraced",
                op_name,
                operation.operation_id,
                step,
            )
        elif result.status == 409:
            # Recovery probe answered: an earlier interval is still open. Stop
            # memory-lane tracing for the session; native work is unaffected.
            operation.disabled = True
            self._trace.memory_lane_enabled = False
            self._trace.pending_inputs.clear()
            self._log_event(
                "annotation",
                op=op_name,
                step=step,
                operation_id=operation.operation_id,
                reason="annotation_recovery_conflict",
            )
            logger.warning(
                "annotation_recovery_conflict: a previous memory interval is still open; memory-lane "
                "tracing is disabled for this session (operation %s, step %s)",
                operation.operation_id,
                step,
            )
        else:
            # Ambiguous (transport/5xx/malformed): later events post without a
            # binding and let the recorder's not_requested/conflict rules decide.
            pass

    def _generation_finish(self, operation: _OperationTrace | None, step, result, errors: list) -> None:
        """Close an operation whose native extraction returned (clean or with
        soft errors): snapshot the after state, attribute the changes via the
        integration hook, and post the audited change series plus the end
        event. ``errors`` (the native soft-error list, empty when clean) owns
        the failed/completed status split and the checkpoint line."""
        if operation is None or operation.disabled:
            return
        after = self._snapshot_memory_state()
        changes, produced, unexplained = self._attribute_changes(operation, result, after)
        if unexplained:
            # Drift downgrades every change of the operation to partial; the
            # hook's own completeness default stands otherwise.
            for payload in changes:
                payload["completeness"] = "partial"
        audit = {
            "clean": not unexplained,
            "unexplained": unexplained[:10],
            "checkpoint": "held" if errors else ("advanced" if operation.input_count else "unchanged"),
        }
        self._post_generation(
            operation,
            step,
            changes=changes,
            produced=produced,
            status="failed" if errors else "completed",
            error_codes=[_sanitize_error_code(code) for code in errors],
            state_evidence="unknown" if after is None or operation.before is None else ("partial" if unexplained else "complete"),
            audit=audit,
            result=result,
        )

    def _generation_finish_exception(self, operation: _OperationTrace | None, step, exc: Exception) -> None:
        """Close an operation whose native extraction raised: nothing is
        attributable to a result, so the observable before/after diff (when
        both snapshots exist) posts as partial evidence."""
        if operation is None or operation.disabled:
            return
        after = self._snapshot_memory_state()
        if after is not None and operation.before is not None:
            changes, _, _ = self._attribute_changes(operation, None, after)
        else:
            changes = []
        for payload in changes:
            payload["completeness"] = "partial"
        self._post_generation(
            operation,
            step,
            changes=changes,
            produced=[],
            status="partial" if changes else "failed",
            error_codes=[type(exc).__name__],
            # Same both-snapshots rule as the success path: without a before
            # there is no observable diff, and zero changes under a "partial"
            # label would claim evidence this path does not have.
            state_evidence="unknown" if after is None or operation.before is None else "partial",
            audit={"clean": not changes, "unexplained": [], "checkpoint": "held"},
            result=None,
        )

    def _change_payload(self, operation: _OperationTrace | None, action, before_rows, after_rows, *, supersede_new=None,
                        evidence: str = "observed_diff", extensions: dict | None = None, completeness: str = "complete"):
        """One memory_change payload over native rows/hits (refs via
        ``_memory_ref``); index/count are stamped at post time.
        ``supersede_new`` adds a supersedes relationship from every before ref
        to the new version. The evidence/extensions defaults are the generic
        observed diff's literal — integrations with native receipts pass theirs
        explicitly. An override must accept at least the positional
        ``(operation, action, before_rows, after_rows)`` call: that is the form
        the base's generic ``_attribute_changes`` uses."""
        before_refs = [self._memory_ref(row) for row in before_rows]
        after_refs = [self._memory_ref(row) for row in after_rows]
        relationships = []
        if supersede_new is not None:
            new_version_id = self._memory_ref(supersede_new)["version_id"]
            relationships = [
                {"type": "supersedes", "from_version_id": ref["version_id"], "to_version_id": new_version_id}
                for ref in before_refs
            ]
        return {
            "trace_session_id": self._trace.trace_session_id if self._trace else "",
            "operation_id": operation.operation_id if operation else "",
            "change_id": str(uuid4()),
            "action": action,
            "before": before_refs,
            "after": after_refs,
            "relationships": relationships,
            "evidence": evidence,
            "completeness": completeness,
            "change_index": 0,  # stamped at post time
            "change_count": 0,  # stamped at post time
            "extensions": extensions if extensions is not None else {},
        }

    def _post_generation(self, operation, step, *, changes, produced, status, error_codes, state_evidence, audit, result):
        """Post change chunks first, memory_generate_end last; every bindable
        event binds at the start cursor so extraction-call retries all fall
        inside one interval. Ambiguous starts post without a binding.

        A chunk post is atomic at the recorder, so a definitive mid-operation
        rejection (4xx) records nothing of that chunk and the operation's open
        interval can never close honestly: the operation is abandoned without
        an end event. An ambiguous failure (transport/5xx) keeps posting — the
        chunk may have landed, and the recorder's change-series check owns the
        gap report."""
        trace = self._trace
        if trace is None:
            return
        binding = None
        if operation.cursor is not None:
            binding = {"after_role_call_index": operation.cursor}
        for index, payload in enumerate(changes):
            payload["trace_session_id"] = trace.trace_session_id
            payload["operation_id"] = operation.operation_id
            payload["change_index"] = index
            payload["change_count"] = len(changes)
        events = [self._event("memory_change", payload, binding=binding) for payload in changes]
        for chunk in _chunk_events(events):
            posted = trace.annotator.post(trace.memory_url, chunk)
            if posted.ok:
                continue
            if posted.status is not None and posted.status < 500:
                self._abandon_operation(operation, step, posted.status)
                return
            # Ambiguous (transport/5xx after retries): keep posting.
        end = self._event(
            "memory_generate_end",
            {
                "trace_session_id": trace.trace_session_id,
                "operation_id": operation.operation_id,
                "requested_by": "main",
                "handled_by": "memory",
                "status": status,
                "produced": produced,
                "change_count": len(changes),
                "state_evidence": state_evidence,
                "error_codes": error_codes,
                "extensions": {self._adapter_meta()["name"]: self._generation_end_context(step, result, audit)},
            },
            binding=binding,
        )
        trace.annotator.post(trace.memory_url, [end])

    def _abandon_operation(self, operation: _OperationTrace, step, status: int) -> None:
        """Abandon an operation whose change post was definitively rejected
        and disable memory-lane tracing for the session: the dangling open
        interval would conflict every later operation start anyway (the start
        side's recovery-conflict path), so this is the same end state reached
        early, with the cause recorded. Native work is unaffected."""
        operation.disabled = True
        self._trace.memory_lane_enabled = False
        self._trace.pending_inputs.clear()
        self._log_event(
            "annotation",
            op="change",
            step=step,
            operation_id=operation.operation_id,
            reason="annotation_change_rejected",
            status=status,
        )
        logger.warning(
            "annotation_change_rejected: a change post for operation %s (step %s) was rejected with "
            "%s; the operation is abandoned and memory-lane tracing is disabled for this session",
            operation.operation_id,
            step,
            status,
        )

    # ------------------------------------------------------------------
    # Delivery tracing
    # ------------------------------------------------------------------
    def main_lane_cursor(self) -> int | None:
        """Cursor-only read of the main lane, or None when untraced/unconfirmed.

        The cursor read is the delivery's opener, so once an unconfirmed
        delivery has disabled delivery tracing no later read is attempted at
        all — it would only POST to a lane whose deliveries are already off.
        """
        trace = self._trace
        if trace is None or not trace.session_open or not trace.delivery_enabled:
            return None
        try:
            result = trace.annotator.post(trace.main_url, [])
            if result.ok and result.cursor is not None:
                return result.cursor
        except Exception:
            logger.exception("main-lane cursor read failed")
        return None

    def consume_annotation_duration(self) -> float:
        """Newly accumulated annotation-I/O plus native search/rewrite seconds
        since the last consume. The search/rewrite half is backend-owned (not
        the annotator's) so the wall-clock exemption works with annotate=false,
        where no annotator exists at all."""
        total, self._io_duration = self._io_duration, 0.0
        trace = self._trace
        if trace is None:
            return total
        try:
            return total + trace.annotator.consume_duration()
        except Exception:
            logger.exception("annotation duration accounting failed")
            return total

    def deliver_recall(self, recall: dict, step: int, msg_index: int | None, cursor: int | None) -> None:
        """Post memory_delivery for one placed transient block; never raises.

        The delivery binds at the main-lane cursor read immediately before
        placement, so the model call(s) of this query — retries included —
        are exactly its bound interval, and the canonical-message proof
        (msg_index + block hash) re-proves placement against every bound call.
        Without a confirmed cursor no delivery posts at all: a placed delivery
        carrying a provable proof_kind must record binding_status bound, which
        only a real cursor can produce.
        """
        trace = self._trace
        if trace is None or not trace.session_open or not trace.delivery_enabled:
            return
        cached = bool(recall.get("cached"))
        if cached:
            # A cache-hit payload cites the search it was rendered from — the
            # anchor memoized with it (never a newer search, whose returned set
            # may differ); the recorder allows several deliveries per search
            # and checks each delivery's refs are an ordered subsequence of
            # the cited search's returned set.
            operation = self._cached_anchor
        else:
            operation = trace.pending_search
        trace.pending_search = None
        if operation is None:
            return  # the search itself was untraced: a delivery would dangle
        try:
            if cursor is None or msg_index is None:
                self._log_event(
                    "annotation",
                    op="delivery",
                    step=step,
                    reason="annotation_delivery_no_cursor",
                    search_operation_id=operation.operation_id,
                )
                logger.warning(
                    "memory delivery skipped: no confirmed main-lane cursor (search %s)",
                    operation.operation_id,
                )
                return
            block = recall["content"]
            refs = [self._memory_ref(memory) for memory in recall["memories"]]
            adapter_extensions = {
                "session_id": self._session_id,
                "delivered": len(refs),
                "msg_index": msg_index,
            }
            if cached:
                adapter_extensions["cached"] = True
            event = self._event(
                "memory_delivery",
                {
                    "trace_session_id": trace.trace_session_id,
                    "delivery_id": str(uuid4()),
                    "search_operation_id": operation.operation_id,
                    "from_role": "memory",
                    "to_role": "main",
                    "main_step": step,
                    "status": "placed",
                    "placement": {
                        "kind": "prompt_message",
                        "content": inline_text_ref(block),
                        "proof_kind": "canonical_message",
                    },
                    "memories": refs,
                    "extensions": {self._adapter_meta()["name"]: adapter_extensions},
                },
                binding={
                    "after_role_call_index": cursor,
                    "proofs": [
                        {
                            "kind": "canonical_message",
                            "msg_index": msg_index,
                            "content_sha256": text_sha256(block),
                        }
                    ],
                },
            )
            result = trace.annotator.post(trace.main_url, [event])
            if not result.ok:
                # An unconfirmed delivery (transport/5xx) may or may not be
                # recorded; a rejection (4xx/409) definitively is not. Either
                # way later deliveries risk conflicting with this one's
                # interval, so delivery tracing stops for the session.
                reason = (
                    "annotation_delivery_rejected"
                    if result.status is not None and result.status < 500
                    else "annotation_delivery_unconfirmed"
                )
                trace.delivery_enabled = False
                self._log_event(
                    "annotation", op="delivery", step=step, reason=reason, status=result.status
                )
                logger.warning(
                    "memory delivery tracing disabled for this session (%s, status=%s)",
                    reason,
                    result.status,
                )
        except Exception:
            logger.exception("memory delivery annotation failed")

    def note_recall(self, payload: dict, step: int) -> None:
        """Bookkeeping only; unconditionally no-throw, including in strict mode.

        Both recall counters count here, at delivery: ``recall_injections``
        every placed block, ``recall_cache_hits`` the placed blocks that were
        served from the dirty-flag cache — a rendered-but-undelivered payload
        (e.g. a terminal limit preflight) inflates neither.
        """
        try:
            self._log_event(
                "recall",
                step=step,
                n_memories=payload.get("n_memories", 0),
                chars=payload.get("chars", 0),
                # The per-hit origin list the rendered lines' suffixes were
                # derived from — the cross-episode accounting's only input.
                origins=payload.get("origins"),
            )
            self._counts["recall_injections"] += 1
            if payload.get("cached"):
                self._counts["recall_cache_hits"] += 1
        except Exception:
            logger.exception("memory recall accounting failed")

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------
    def finalize(self) -> None:
        """Final flush + dump + memory.json + close. Idempotent, exception-safe."""
        if self._finalized:
            return
        self._finalized = True
        first_error = None

        # Without this flush the tail messages are never processed. Runs
        # despite a tripped breaker.
        if self._available:
            try:
                self._extract("final")
            except Exception as e:  # only reachable in strict mode
                logger.exception("final extraction flush failed")
                first_error = first_error or e

        final_memories: list[dict] = []
        if self._available:
            try:
                final_memories = self._final_dump()
            except Exception as e:
                logger.exception("final memory dump failed")
                self._counts["backend_errors"] += 1
                self._log_event("error", op="finalize_dump", error=str(e))
                first_error = first_error or e

        try:
            self._write_memory_json(final_memories=final_memories)
        except Exception as e:
            logger.exception("memory.json write failed")
            first_error = first_error or e
        finally:
            try:
                self._close()
            except Exception as e:
                logger.exception("failed to close the memory backend")
                first_error = first_error or e

        if first_error is not None and self.config.strict:
            raise first_error

    @abstractmethod
    def _final_dump(self) -> list[dict]:
        """The integration's final memory dump for memory.json (diagnostic)."""

    @abstractmethod
    def _close(self) -> None:
        """Close the integration handle — mechanics only, errors propagate;
        the base's call sites own containment and logging."""

    def stats(self) -> dict:
        """In-memory counters only — safe after close."""
        return {
            "enabled": self.config.enabled,
            "available": self._available,
            "session_id": self._session_id,
            "counts": self._counts.copy(),
            **self._stats_extras(),
        }

    def _stats_extras(self) -> dict:
        """Integration-owned extra ``stats`` fields (default: none)."""
        return {}

    # ------------------------------------------------------------------
    # Event log / memory.json
    # ------------------------------------------------------------------
    def _log_event(self, kind: str, **kwargs) -> None:
        self._events.append({"kind": kind, **kwargs})

    def _memory_json_fields(self) -> dict:
        """Integration-owned extra memory.json fields (default: none)."""
        return {}

    def _write_memory_json(self, final_memories: list | None = None) -> None:
        if not self.config.output_dir:
            return
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "instance_id": self.instance_id,
            "scope": self.config.scope,
            "user_id": self.config.user_id,
            "session_id": self._session_id,
            "enabled": self.config.enabled,
            "available": self._available,
            **self._memory_json_fields(),
            "settings": self._settings,
            "counts": self._counts,
            "events": self._events,
            "final_memories": final_memories if final_memories is not None else [],
        }
        (output_dir / "memory.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))

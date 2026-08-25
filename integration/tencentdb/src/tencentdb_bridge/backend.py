"""TencentDB-Agent-Memory (MemoryCore) backend for the automatic-extraction arm.

Drives one standalone MemoryCore gateway container (driver-managed, one per
run root) for one SWE-bench episode on top of the shared lifecycle skeleton:
buffered message recording, periodic ``conversation/add`` flushes that feed
the server-side threshold-batched extraction pipeline, an idle-timer-aware
finalize drain so the below-threshold episode tail lands inside the episode,
transient recall over the native layers (L1 atomic facts repo-scoped via
``task_id``, plus the L3 persona as a prepended score-less pseudo-hit and
the L2 scene index as a header section with a self-contained read guide —
L0 raw conversation search is agent-initiated through a second, unconditional
header guide whose curls are observed, never mediated), and the final dump.

Extraction runs inside the container against the provider upstream directly
(decision: extraction traffic is not recorded in the trajectory, the same
treatment as mem0's hosted extraction); memory protocol events reach the
trajectory via the annotate MEMORY lane from API receipts (the watermark-
resolved L1 rows), never from model traffic.

Scoping: the isolation quadruple is team ``minisweagent`` / agent
``memory-bridge`` / user id (driver-minted from the run-root name) / task id
= the episode's repo key — completing the upstream two-tier design: L1
becomes the repo tier, L2/L3 profiles accumulate at team+agent level (the
general tier). Cross-episode origin attribution maps each hit's
``created_at`` onto per-episode windows persisted in the run-root sidecar
``<run-root>/tdai/episodes.jsonl``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from shared_bridge.annotate import inline_text_ref, normalize_score, sanitize_url, text_sha256
from shared_bridge.backend import (
    BaseMemoryBackend,
    _BackendUnavailable,
    _new_session_id,
    _repo_of,
)

from tencentdb_bridge.client import (
    AGENT_ID,
    IDLE_WAIT_MARGIN_SECONDS,
    MESSAGE_CONTENT_MAX_CHARS,
    SEARCH_LIMIT_MAX,
    TEAM_ID,
    WATERMARK_SKEW_SECONDS,
    TencentDBApiError,
    TencentDBClient,
    clamp_utf16_units,
    utc_now_iso,
)
from tencentdb_bridge.config import TencentDBConfig
from tencentdb_bridge.prompts import (
    CONVO_SEARCH_GUIDE,
    CONVO_SEARCH_LEAD_IN,
    CONVO_SEARCH_TITLE,
    PERSONA_LINE_PREFIX,
    RECALL_LEAD_IN,
    RECALL_SECTION_TITLE,
    RECALL_TITLE,
    SCENE_INDEX_LEAD_IN,
    SCENE_INDEX_TITLE,
    SCENE_READ_GUIDE,
)

logger = logging.getLogger("tencentdb_bridge.backend")

_ADAPTER_NAME = "tencentdb"
_IDENTITY_SCHEME = "tencentdb-memorycore-l1-v1"
# The persona may carry an appended scene-navigation tail; the official
# injector strips at this exact upstream marker (scene-navigation.ts NAV_HEADER)
# because the bridge renders its own L2 index. The bridge strips it from the
# rendered persona line the same way AND consumes it: _parse_scene_nav reads
# the tail's per-scene heat/update stamps to order and decorate that index.
_NAV_MARKER = "---\n## 🗺️ Scene Navigation (Scene Index)"
# Read-detection markers inside a bash curl command for the two
# agent-initiated read kinds (disjoint strings — nothing cross-arms).
_SCENE_READ_MARKER = "/v3/scenario/read"
_CONVO_SEARCH_MARKER = "/v3/conversation/search"
# The body pair arming each kind ("path" for a scene read, "query" for a
# conversation search — each regex keys on its OWN field, so neither kind can
# arm off the other's body): the guide's plain double quotes and the
# shell-escaped form (\"path\":\"...\") both arm; single-quoted pairs are
# invalid JSON upstream (the call fails), so they deliberately never match.
_PATH_RE = re.compile(r'\\?"path\\?"\s*:\s*\\?"([^"\\]+)')
_QUERY_RE = re.compile(r'\\?"query\\?"\s*:\s*\\?"([^"\\]+)')
# Non-empty sentinel for an unresolvable origin: the base renders NO suffix
# for a None origin, but "not this episode" must still read as cross-episode.
_UNKNOWN_ORIGIN = "unknown"
_PERSONA_HIT_ID = "persona"

try:
    from importlib.metadata import version as _pkg_version

    _BRIDGE_VERSION = _pkg_version("tencentdb-bridge")
except Exception:  # source tree without installed metadata
    _BRIDGE_VERSION = "0.1.0"


def _row_is_fresh(row: dict) -> bool:
    """A produced L1 row is a fresh create iff its ``version`` is 0/absent —
    the store zeroes fresh rows (``writeMemory`` leaves nextVersion 0 for the
    ``store`` action and the SQLite DDL defaults to 0); a dedup-merge rewrite
    carries ``max(target versions) + 1`` >= 1. The one predicate behind
    ``_attribute_changes``, the generation-end context, and the counters."""
    version = row.get("version")
    return not isinstance(version, int) or version < 1


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# The stored nav tail's canonical path prefix (StoragePaths.sceneBlocksDir):
# ls paths are the same storage keys minus this prefix, so stripping it makes
# the nav path join verbatim against the ls path.
_NAV_PATH_PREFIX = "scene_blocks/"
# Entry lines use HALFWIDTH colons (the footer's fullwidth-colon 热度： prose
# never matches); the emoji run, if any, trails the digits.
_NAV_HEAT_RE = re.compile(r"^\*\*热度\*\*:\s*(\d+)")
_NAV_UPDATED_RE = re.compile(r"\*\*更新\*\*:\s*(\S+)")


def _parse_scene_nav(tail: str) -> dict[str, dict]:
    """Parse the persona's scene-navigation tail into path -> {heat, updated}.

    Receives ONLY the post-marker segment of the core/read content — a
    model-generated persona body that happens to contain ``### Path:`` lines
    must never seed the stash. Line-oriented against the fixed upstream shape
    (scene-navigation.ts): ``### Path: scene_blocks/<filename>`` opens a
    block; the block's ``**热度**: <heat>`` line carries the heat plus,
    inline after `` | ``, the optional ``**更新**: <stamp>`` clause. The
    stamp is searched on that same heat line only, so a model-written
    ``Summary:`` containing the literal ``**更新**:`` cannot seed a bogus
    stamp, and ``(\\S+)`` takes the first whitespace-free token (the stamp is
    model-written text, rendered verbatim downstream). The path key is the
    segment after the LAST ``scene_blocks/`` (the rsplit covers any
    absolute-path variant, which a leading-prefix strip could not; scene
    filenames contain no separators). A block without a parseable Path+heat
    pair is skipped, and the parser never raises — persona content is
    model-generated text, so any unexpected shape degrades to fewer or empty
    entries.
    """
    scenes: dict[str, dict] = {}
    path: str | None = None
    for line in tail.splitlines():
        if line.startswith("### Path: "):
            path = line[len("### Path: ") :].strip().rsplit(_NAV_PATH_PREFIX, 1)[-1] or None
            continue
        if path is None:
            continue
        heat = _NAV_HEAT_RE.match(line)
        if heat is None:
            continue
        stamp = _NAV_UPDATED_RE.search(line)
        scenes[path] = {"heat": int(heat.group(1)), "updated": stamp.group(1) if stamp else None}
        path = None  # one heat line per block; the rest of the block is its Summary
    return scenes


class _Window:
    """One episode's attribution window [start, end) over the shared clock.

    ``start`` is floored to whole milliseconds. The boundary now serves L1
    origin attribution alone: the only stamps compared against it are L1
    ``created_at`` values — container-side extraction stamps minutes past the
    episode start, themselves round-tripped through the gateway's millisecond
    ``toISOString()`` — so the <= 1 ms widening at the start edge is a no-op
    there rather than a correctness requirement (no L1 row can be stamped
    that close to the start). Kept as one cheap line. ``end`` stays exact —
    attribution errs window-inclusive there.
    """

    __slots__ = ("start", "end", "session_id")

    def __init__(self, start: datetime, session_id: str):
        self.start = start.replace(microsecond=start.microsecond // 1000 * 1000)
        self.end: datetime | None = None  # None = open (crashed episode or own)
        self.session_id = session_id

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment and (self.end is None or moment < self.end)


class TencentDBBackend(BaseMemoryBackend):
    """Drives the MemoryCore gateway's extraction lifecycle for one episode."""

    _COUNTERS = (
        "memories_added",
        "memories_updated",
        "agent_scene_reads",
        "scene_read_chars",
        "agent_conversation_searches",  # observed agent-initiated L0 searches (one agent step each)
        "conversation_search_chars",  # observation chars those searches put in front of the model
    )

    # The stored /v3/memory-prompt/* channel is scope-stored, not per-request:
    # it cannot carry the guidelines' per-episode context half, and the async
    # (timer-fired) extraction would race per-episode upserts. No conveyance.
    _CONVEYS_EXTRACTION_GUIDELINES = False

    def __init__(self, config: TencentDBConfig, instance_id: str, model_base_url: str = ""):
        super().__init__(config, instance_id, model_base_url)
        self._client: TencentDBClient | None = None
        self._pending: list[dict] = []  # recorded messages not yet flushed
        self._added = False  # any conversation/add this episode (finalize needs it)
        # Production-window start (host clock), taken at the episode's first
        # add and never narrowed: the ``_counted`` dedup set, not the
        # watermark, is the exactly-once mechanism — a below-threshold add
        # arms the L1 idle timer (invisible to /v2/pipeline/status), so a
        # resolve can succeed with rows still landing up to the L1 idle
        # timeout later, and narrowing the window would strand them uncounted.
        self._watermark: str | None = None
        self._counted: set[tuple[str, object]] = set()  # (id, version) pairs already counted
        self._windows: list[_Window] = []
        self._persona_hit: dict | None = None  # L3 pseudo-hit, refreshed per search cycle
        self._scenes: list[dict] = []  # L2 index, refreshed per search cycle
        # L2 heat/update stamps parsed from the persona's scene-navigation
        # tail (path -> {heat, updated}): derived ORDERING metadata for the
        # index section — scenario/ls stays the existence source.
        self._nav_scenes: dict[str, dict] = {}
        self._pending_reads: dict[str, str] = {}  # armed agent scene-reads: tool_call_id -> path
        self._pending_searches: dict[str, str] = {}  # armed agent conversation searches: tool_call_id -> query
        # The effective L1 idle timeout, resolved exactly once per start from
        # the driver-generated gateway yaml (0.0 = not yet resolved).
        self._l1_idle_timeout = 0.0

    # ------------------------------------------------------------------
    # Startup (base template hooks)
    # ------------------------------------------------------------------
    def _initial_settings(self) -> dict:
        return {
            "api_base_url": "",
            "service_id": self.config.service_id,
            "team_id": TEAM_ID,
            "agent_id": AGENT_ID,
            "prompt_mode": "code",
            "bm25_language": "en",
            # Computed keys, blank until _startup resolves them (the same
            # filled-at-start pattern as api_base_url): the effective L1 idle
            # timeout is read back from the driver-generated gateway yaml —
            # the single source of truth.
            "l1_idle_timeout": 0.0,
            "l1_idle_timeout_source": "",
            "drain_budget": self.config.drain_budget,
            "finalize_drain_budget": self.config.finalize_drain_budget,
            "drain_interval": self.config.drain_interval,
            "embedding_provider": self.config.embedding_provider,
            "embedding_model": self.config.embedding_model,
            "conversation_search_limit": self.config.conversation_search_limit,
            **self._core_initial_settings(),
        }

    def _resolve_settings(self) -> dict:
        if not self.config.run_root:
            raise _BackendUnavailable("missing run root: set agent.memory.run_root (the driver fills it)")
        return {
            "endpoint": self.config.endpoint.rstrip("/"),
            "l1_idle_timeout": self._resolve_l1_idle_timeout(),
        }

    def _resolve_l1_idle_timeout(self) -> float:
        """The effective L1 idle timeout, read exactly once per start from the
        driver-generated gateway yaml — the single source of truth (a
        host-side copy would silently mis-frame the finalize wait when the
        two drifted). The driver's writer emits
        ``memory.pipeline.l1IdleTimeoutSeconds`` as a plain numeric literal
        (no ``${TDAI_*}`` leaf on that key). Failure discipline: a missing
        file, a missing/blank key, or a non-numeric value fails the start
        LOUDLY — silently guessing a value is exactly the divergence this
        single-sourcing removes."""
        path = Path(self.config.run_root) / "tdai" / "tdai-gateway.yaml"
        try:
            parsed = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as e:
            raise _BackendUnavailable(f"cannot read the gateway yaml at {path}: {e}") from e
        value = parsed
        for key in ("memory", "pipeline", "l1IdleTimeoutSeconds"):
            value = value.get(key) if isinstance(value, dict) else None
        # bool first: isinstance(True, int) is True, and True is not a timeout.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _BackendUnavailable(
                f"missing or non-numeric memory.pipeline.l1IdleTimeoutSeconds in the gateway yaml at {path}"
            )
        return float(value)

    def _make_client(self, settings: dict) -> TencentDBClient:
        """Isolated gateway-client constructor (the test seam)."""
        return TencentDBClient(
            settings["endpoint"],
            api_key=self.config.api_key,
            service_id=self.config.service_id,
        )

    def _repo_key(self) -> str:
        """The task_id tier: the episode's repository key (cure's lattice shape)."""
        return _repo_of(self.instance_id)

    def _startup(self, settings: dict) -> None:
        self._settings["api_base_url"] = sanitize_url(settings["endpoint"])
        # The resolved idle timeout lands on the instance (the finalize drain
        # reads it) and in the settings artifact — the resolution, not a guess.
        self._l1_idle_timeout = settings["l1_idle_timeout"]
        self._settings["l1_idle_timeout"] = self._l1_idle_timeout
        self._settings["l1_idle_timeout_source"] = "gateway-yaml"
        self._client = self._make_client(settings)
        self._client.health()  # fail fast on an unreachable gateway
        if self.config.recall_min_score is not None:
            # One start-time warning (the guideline-override pattern): L1's
            # own score is not one scale across retrieval strategies — a
            # healthy hybrid lane yields tiny RRF ranks, an FTS-only lane a
            # normalized bm25 in (0,1), a vector-only lane cosine similarity;
            # lane availability is per-query and the response carries no
            # strategy field, so the floor would keep/drop hits by whichever
            # lane answered, not by relevance. The integration's documented
            # stance is recall_min_score stays unset.
            logger.warning(
                "agent.memory.recall_min_score is set, but tencentdb L1 scores are not one scale across "
                "retrieval strategies (RRF ranks vs normalized bm25 vs cosine similarity, chosen per query "
                "server-side); the floor keeps/drops hits by lane, not by relevance"
            )
        self._session_id = _new_session_id(self.instance_id)
        started = utc_now_iso()
        self._load_windows()
        self._windows.append(_Window(_parse_iso(started) or datetime.now(timezone.utc), self._session_id))
        self._append_sidecar(
            {"event": "start", "session_id": self._session_id, "instance_id": self.instance_id, "started_at": started}
        )
        with contextlib.suppress(Exception):
            total = self._client.atomic_count(
                team_id=TEAM_ID, agent_id=AGENT_ID, user_id=self.effective_user_id(), task_id=self._repo_key()
            )
            self._log_event("store_count", total=total)  # run-start diagnostic only

    def _reset_extras(self) -> None:
        self._pending = []
        self._added = False
        self._watermark = None
        self._counted = set()
        self._windows = []
        self._persona_hit = None
        self._scenes = []
        self._nav_scenes = {}
        self._pending_reads = {}
        self._pending_searches = {}

    def _start_event_extras(self) -> dict:
        return {
            "user_id": self.effective_user_id(),
            "api_base_url": self._settings.get("api_base_url", ""),
            "team_id": TEAM_ID,
            "agent_id": AGENT_ID,
            "task_id": self._repo_key(),
        }

    def _close(self) -> None:
        self._pending_reads = {}
        self._pending_searches = {}
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    # ------------------------------------------------------------------
    # Episode-window sidecar (cross-episode origin attribution)
    # ------------------------------------------------------------------
    def _sidecar_path(self) -> Path:
        return Path(self.config.run_root) / "tdai" / "episodes.jsonl"

    def _load_windows(self) -> None:
        """Prior episodes' windows from the run-root sidecar (serial episodes)."""
        self._windows = []
        try:
            lines = self._sidecar_path().read_text().splitlines()
        except OSError:
            return
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("event") == "start":
                started = _parse_iso(str(record.get("started_at") or ""))
                session = record.get("session_id")
                if started is not None and isinstance(session, str) and session:
                    self._windows.append(_Window(started, session))
            elif record.get("event") == "drain":
                drained = _parse_iso(str(record.get("drained_at") or ""))
                session = record.get("session_id")
                if drained is None or not isinstance(session, str):
                    continue
                for window in reversed(self._windows):
                    if window.session_id == session:
                        window.end = drained
                        break

    def _append_sidecar(self, record: dict) -> None:
        # Attribution is observability: a sidecar failure logs and degrades
        # origins to the "unknown" sentinel, never fails the arm.
        try:
            path = self._sidecar_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("failed to append the episode-window sidecar")

    def _record_drain(self) -> None:
        drained = utc_now_iso()
        self._append_sidecar({"event": "drain", "session_id": self._session_id, "drained_at": drained})
        for window in reversed(self._windows):
            if window.session_id == self._session_id and window.end is None:
                window.end = _parse_iso(drained) or datetime.now(timezone.utc)
                break

    def _origin_session(self, created_at: str) -> str:
        """Map a hit's created_at onto the owning episode's session id ('' = unknown)."""
        moment = _parse_iso(created_at)
        if moment is None:
            return ""
        for window in reversed(self._windows):
            if window.contains(moment):
                return window.session_id
        return ""

    # ------------------------------------------------------------------
    # Trace adapter hooks (the protocol machinery lives in the base)
    # ------------------------------------------------------------------
    def _adapter_meta(self) -> dict:
        return {"name": _ADAPTER_NAME, "version": _BRIDGE_VERSION}

    def _trace_namespace(self) -> str:
        # The effective user id is the store's run-isolation tier; the hash
        # keeps the run-root-derived id itself out of the recorded refs.
        return text_sha256(self.effective_user_id())

    def _trace_context(self) -> dict:
        return {
            "session_id": self._session_id,
            "user_id": self.effective_user_id(),
            "team_id": TEAM_ID,
            "agent_id": AGENT_ID,
        }

    def _memory_ref(self, hit: dict) -> dict:
        """One L1 row or the persona pseudo-hit as a portable MemoryRef."""
        memory_id = str(hit["id"])  # id-less rows are dropped at the intake points
        text = hit.get("content")
        if isinstance(text, str) and text:
            content = inline_text_ref(text)
            version_id = f"{memory_id}:{text_sha256(text)}"
        else:
            content = {"availability": "unavailable", "reason": "no_gateway_text"}
            version_id = f"{memory_id}:unavailable"
        extensions = {}
        for key in ("created_at", "updated_at", "version", "task_id"):
            value = hit.get(key)
            if value is not None:
                extensions[key] = value
        # An unusable score is dropped, never coerced to 0.0.
        score = normalize_score(hit.get("score"))
        if score is not None:
            extensions["score"] = score
        return {
            "version_id": version_id,
            "identity_strength": "native_stable",
            "identity_scheme": _IDENTITY_SCHEME,
            "item_id": memory_id,
            "namespace": self._namespace,
            "content": content,
            "extensions": {_ADAPTER_NAME: extensions},
        }

    def _attribute_changes(self, operation, result, after):
        """Watermark-resolved L1 rows -> the change series (native_receipt,
        partial: the async pipeline offers no before-image and its silent
        dedup deletes stay unobservable). Row ``version`` 0 = a fresh create
        (the store zeroes fresh L1 rows); >= 1 = a rewrite (a row created and
        rewritten within one tick counts only as updated)."""
        if result is None:
            return [], [], []
        changes: list[dict] = []
        produced: list[dict] = []
        for row in result:
            if not row.get("id"):
                continue
            version = row.get("version")
            action = "create" if _row_is_fresh(row) else "update"
            changes.append(
                self._change_payload(
                    operation, action, [], [row],
                    evidence="native_receipt",
                    extensions={_ADAPTER_NAME: {"version": version}},
                    completeness="partial",
                )
            )
            produced.append(self._memory_ref(row))
        return changes, produced, []

    def _generation_end_context(self, step, result, audit: dict) -> dict:
        context = {
            "session_id": self._session_id,
            "extraction_step": str(step),
            "user_id": self.effective_user_id(),
            "task_id": self._repo_key(),
        }
        if result is not None:
            context["added"] = sum(1 for row in result if row.get("id") and _row_is_fresh(row))
            context["updated"] = sum(1 for row in result if row.get("id") and not _row_is_fresh(row))
        return context

    # ------------------------------------------------------------------
    # Recording (role fold + agent-initiated read observation)
    # ------------------------------------------------------------------
    def _normalize_role(self, role: str) -> str:
        # conversation/add accepts only user/assistant (zod enum — anything
        # else is a 400, not coerced); system and tool fold to user. Folding
        # observations to user is deliberate: the pipeline counts
        # role=="user" messages as conversation rounds.
        return role if role in ("user", "assistant") else "user"

    def _should_store(self, text: str) -> bool:
        return bool(text)

    def _store_message(self, role: str, text: str, step: int) -> None:
        # Host-clock ingest stamp: conversation/add honors a caller-supplied
        # recorded_at (an optional field in the upstream schema) over the
        # container's receive time, so the raw-message timestamps the agent's
        # conversation search renders stay in the host clock domain —
        # consistent across episodes and immune to host-vs-container skew
        # (Docker Desktop VM clock drift after sleep/wake). The content clamp
        # is the wire's per-message cap (zod, shared with update text) measured
        # in the wire's own unit — UTF-16 code units, not Python code points:
        # the base's max_message_chars truncation keeps text under it at the
        # shipped default, but a run raising that knob above the cap would
        # draw a gateway 400 on every add — the breaker class. Mechanical,
        # never a policy decision (the client's query-cap precedent).
        self._pending.append(
            {"role": role, "content": clamp_utf16_units(text, MESSAGE_CONTENT_MAX_CHARS), "recorded_at": utc_now_iso()}
        )

    def record(self, messages: list[dict], step: int) -> None:
        """Read-detection runs on the raw messages, pre-normalization — the
        closer's role test needs the un-folded ``tool`` role (the base hands
        ``_store_message`` the role after the fold). Detection never raises
        into the agent loop."""
        if self._available and not self._finalized:
            for message in messages:
                if isinstance(message, dict):
                    try:
                        self._observe_agent_read(message, step)
                    except Exception:
                        logger.exception("agent-read observation failed")
        super().record(messages, step)

    def _observe_agent_read(self, message: dict, step: int) -> None:
        """Observation for both agent-initiated read kinds (L2 scene reads
        and L0 conversation searches): shared arming/closing/clearing over
        two separate pending maps, each keyed by tool_call_id."""
        role = str(message.get("role") or "")
        if role == "assistant":
            # Any assistant message before the matching observation clears all
            # pending reads (a FormatError emits no observation at all).
            self._pending_reads = {}
            self._pending_searches = {}
            actions = message.get("extra", {}).get("actions")
            if not isinstance(actions, list):
                return
            for action in actions:
                if not isinstance(action, dict):
                    continue
                command = action.get("command")
                tool_call_id = action.get("tool_call_id")
                # Without a tool_call_id the closer cannot be matched exactly
                # under multi-action steps; such actions never arm. Arming
                # requires the kind's own parsed body pair ("path" / "query"):
                # the injected guides always carry one, so a marker-mentioning
                # command without it (e.g. grep over the guide text) is not a
                # read and must not be counted as one. One command containing
                # both markers arms BOTH slots under one id (the chained-curl
                # case; its one observation then closes both).
                if not isinstance(command, str) or not isinstance(tool_call_id, str) or not tool_call_id:
                    continue
                if _SCENE_READ_MARKER in command:
                    match = _PATH_RE.search(command)
                    if match:
                        self._pending_reads[tool_call_id] = match.group(1)
                if _CONVO_SEARCH_MARKER in command:
                    match = _QUERY_RE.search(command)
                    if match:
                        self._pending_searches[tool_call_id] = match.group(1)
        elif role == "tool":
            # Id-matching, not next-message-matching: a sibling action's
            # observation (same step, different tool_call_id) must not close.
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str):
                return
            path = self._pending_reads.pop(tool_call_id, None)
            if path is not None:
                chars = len(self._message_text(message))
                self._counts["agent_scene_reads"] += 1
                self._counts["scene_read_chars"] += chars
                self._log_event("scene_read", step=step, path=path, chars=chars)
            query = self._pending_searches.pop(tool_call_id, None)
            if query is not None:
                # A both-kinds chained command's chars land in BOTH counters:
                # mini-swe-agent gives one observation per action, so the two
                # cannot be separated — each counter keeps its own meaning
                # ("chars this read kind put in front of the model").
                chars = len(self._message_text(message))
                self._counts["agent_conversation_searches"] += 1
                self._counts["conversation_search_chars"] += chars
                self._log_event("conversation_search", step=step, query=query, chars=chars)

    # ------------------------------------------------------------------
    # Extraction (server-side pipeline + L1 drain)
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def _timed(self):
        """Accrue native-call seconds onto the base's I/O-exemption
        accumulator (a plain attribute the base drains through
        ``consume_annotation_duration``; it times only ``_search`` and the
        rewrite itself)."""
        started = time.monotonic()
        try:
            yield
        finally:
            self._io_duration += time.monotonic() - started

    def _sleep(self, seconds: float) -> None:
        """Test seam over the finalize idle-wait sleep."""
        time.sleep(seconds)

    def _send_pending(self) -> None:
        assert self._client is not None
        try:
            with self._timed():
                self._client.conversation_add(
                    list(self._pending),
                    team_id=TEAM_ID,
                    agent_id=AGENT_ID,
                    user_id=self.effective_user_id(),
                    session_id=self._session_id,
                    task_id=self._repo_key(),
                    timeout=self.config.add_timeout,
                )
        except TencentDBApiError as e:
            # A mid-chunk failure has its earlier chunks CONFIRMED persisted
            # server-side (their responses came back): drop exactly that
            # prefix so the retained-buffer retry (the add caller keeps the
            # buffer only on a failed add) covers just the uncertain tail.
            # Re-feeding the confirmed prefix would duplicate those messages
            # in L0 and re-count their user-rounds against the threshold.
            if e.persisted_messages:
                del self._pending[: e.persisted_messages]
            raise
        self._added = True

    def _drain_tick(self) -> None:
        """Wait for L1 idle within the per-tick budget (one L1 cycle: idle
        timer + extraction LLM + margin). L2/L3 cascades are deliberately not
        waited on — /v2/pipeline/status splits per-layer for exactly this."""
        assert self._client is not None
        with self._timed():
            idle = self._client.wait_l1_idle(self.config.drain_budget, self.config.drain_interval)
        if not idle:
            raise TimeoutError(f"L1 pipeline did not reach idle within {self.config.drain_budget:.0f}s drain budget")

    def _drain_final(self) -> None:
        """Idle-timer-aware finalize drain: the below-threshold tail's only
        landing mechanism. There is no working flush API for isolated
        sessions (POST /session/end misses the team/agent-keyed capture state
        and silently no-ops), so: drain the threshold task within the
        finalize budget, wait out the full idle window unconditionally, then
        drain the tail with a fresh per-tick budget. The status API never
        exposes armed timers, so any finalize may carry an armed tail — the
        wait is the only way to know the timer has fired before the second
        poll, and it deliberately ignores the deadline (the tail landing
        inside the episode wins over a hard wall at its end). The second
        drain must NOT ride the deadline remainder: the sleep above may have
        consumed it entirely, and the timer-fired tail task then needs a full
        L1 cycle — a ~0-budget poll would raise on a tail that lands moments
        later. The drain record closes the episode's attribution window even
        when the drain fails — the boundary is when this episode stopped
        waiting."""
        assert self._client is not None
        try:
            deadline = time.monotonic() + self.config.finalize_drain_budget
            with self._timed():
                if not self._client.wait_l1_idle(
                    max(0.0, deadline - time.monotonic()), self.config.drain_interval
                ):
                    raise TimeoutError(
                        f"L1 pipeline did not reach idle within {self.config.finalize_drain_budget:.0f}s finalize drain budget"
                    )
                self._sleep(self._l1_idle_timeout + IDLE_WAIT_MARGIN_SECONDS)
                if not self._client.wait_l1_idle(self.config.drain_budget, self.config.drain_interval):
                    raise TimeoutError(f"L1 tail did not land within {self.config.drain_budget:.0f}s post-idle drain budget")
        finally:
            self._record_drain()

    def _resolve_watermark(self) -> list[dict]:
        """All L1 rows produced since the episode's first add, via the
        watermark query (matches ``updated_time``: on the arm that equals
        created-after, with dedup merges' fresh ids included). The window
        never narrows; the caller's ``_counted`` set filters rows an earlier
        resolve already delivered. Paginated (limit 100, offset on total) —
        the default limit 20 silently truncates otherwise."""
        assert self._client is not None and self._watermark is not None
        with self._timed():
            return self._client.atomic_query(
                team_id=TEAM_ID,
                agent_id=AGENT_ID,
                user_id=self.effective_user_id(),
                time_start=self._watermark,
                task_id=self._repo_key(),
            )

    def _perform_extraction(self, step) -> None:
        client = self._client
        if client is None:
            return
        final = step == "final"
        # Readiness guard before counting: nothing to flush AND no armed
        # timer possible (never added) — an unready tick is not a counted call.
        if not self._pending and not (final and self._added):
            return
        self._counts["extraction_calls"] += 1
        n_messages = len(self._pending)
        try:
            operation = self._generation_begin(step)
        except Exception:
            logger.exception("annotation generation-begin failed; extraction continues untraced")
            operation = None
        try:
            if self._pending:
                # The production window opens at the episode's first add and
                # never narrows — the (id, version) dedup set below, not the
                # watermark, is the exactly-once mechanism. The skew margin
                # keeps a host-ahead-of-container clock from stranding the
                # window's first rows (the filter reads the gateway clock).
                if self._watermark is None:
                    self._watermark = utc_now_iso(skew_seconds=WATERMARK_SKEW_SECONDS)
                self._send_pending()
                # The add returned — L0 is persisted server-side. The buffer
                # must not survive a later drain failure: a re-add would
                # re-feed the pipeline wholesale, and each re-add enqueues
                # fresh extraction work that chains L1 tasks past every
                # drain budget. Only a failed ADD retains the buffer (an
                # uncertain outcome — the mem0-style retry), and then only
                # its unconfirmed tail: ``_send_pending`` already dropped the
                # confirmed chunk prefix the error carried.
                self._pending.clear()
            if final:
                self._drain_final()
            else:
                self._drain_tick()
            rows = self._resolve_watermark() if self._watermark else []
        except Exception as e:
            try:
                self._generation_finish_exception(operation, step, e)
            except Exception:
                logger.exception("annotation generation-end failed; extraction continues untraced")
            raise  # hard failure: the base shell counts, registers, and gates
        # Exactly-once over the open window: a row-version an earlier resolve
        # already delivered is skipped; rows still landing behind an armed
        # (status-invisible) idle timer are counted by whichever later resolve
        # first sees them — including the finalize drain's.
        produced: list[dict] = []
        for row in rows:
            if not row.get("id"):
                continue
            # Absent normalizes to 0 — the same fresh-row class _row_is_fresh
            # uses — so one row sighted once without a version and once with
            # the store's version-0 default counts exactly once.
            version = row.get("version")
            key = (str(row["id"]), version if isinstance(version, int) else 0)
            if key in self._counted:
                continue
            self._counted.add(key)
            produced.append(row)
        added = sum(1 for row in produced if _row_is_fresh(row))
        updated = sum(1 for row in produced if not _row_is_fresh(row))
        self._counts["memories_added"] += added
        self._counts["memories_updated"] += updated
        self._consecutive_errors = 0
        if self._trace is not None:
            self._trace.pending_inputs.clear()
        self._log_event("extraction", step=step, messages=n_messages, added=added, updated=updated)
        try:
            self._generation_finish(operation, step, produced, [])
        except Exception:
            logger.exception("annotation generation-end failed; extraction continues untraced")

    def _register_extraction_failure(self, step, error: str, *, log_event: bool = True) -> None:
        logger.error("tencentdb extraction failed: %s", error)
        super()._register_extraction_failure(step, error, log_event=log_event)

    # ------------------------------------------------------------------
    # Recall (L1 hit layer, L2 index, L3 persona)
    # ------------------------------------------------------------------
    def _search(self) -> list:
        # Readiness guard first, before anything counted.
        if self._client is None:
            return []
        # The auxiliary layers degrade, never fail the search: a transient
        # blip on core_read (L3) or scenario_ls (L2) must not take down the
        # L1 recall with it — the measured surface is L1's facts, so the
        # persona/scene index are dropped for this cycle (refreshed on the
        # next search cycle) and only a failed atomic_search counts as a
        # search error.
        self._persona_hit = self._safe_persona()
        self._scenes = self._safe_scenes()
        try:
            # L1: one atomic/search, repo-scoped. Small overfetch under the
            # schema cap (the base's floor/slice work below the fetch).
            limit = min(SEARCH_LIMIT_MAX, self.config.max_memories + 5)
            l1 = self._client.atomic_search(
                self._recall_query() or "",
                limit=limit,
                team_id=TEAM_ID,
                agent_id=AGENT_ID,
                user_id=self.effective_user_id(),
                task_id=self._repo_key(),
                timeout=self.config.search_timeout,
            )
        except Exception:
            self._counts["search_errors"] += 1
            raise
        # L3 persona pseudo-hit, PREPENDED — load-bearing: the base slices
        # hits[:max_memories] in list order with no score sort, so an
        # appended pseudo-hit would be silently dropped whenever the L1 list
        # fills the budget (the persona displaces the lowest-ranked line
        # instead). Not-yet-generated persona answers 200 with nulls.
        persona_first: list[dict] = [self._persona_hit] if self._persona_hit is not None else []
        return persona_first + [hit for hit in l1 if hit.get("id")]

    def _safe_persona(self) -> dict | None:
        try:
            persona = self._client.core_read(team_id=TEAM_ID, agent_id=AGENT_ID, user_id=self.effective_user_id())
        except Exception:
            # A FAILED read leaves the nav stash untouched (one degraded cycle
            # of last-known ordering) — a deliberate divergence from the
            # persona/scenes drop-for-the-cycle discipline: the stash is
            # derived ordering metadata, not content, and scenario/ls keeps
            # existence and paths fresh on the same failed cycle, so stale
            # heats can misorder entries at worst, never invent or hide one.
            logger.exception("persona read failed; the L3 line is omitted this cycle")
            return None
        # A SUCCESSFUL read replaces the stash wholesale (content null or
        # marker-less => empty stash). The refresh keys off the RAW response
        # content, INDEPENDENT of _persona_from's return: that mapper returns
        # None whenever the pre-marker body is empty, so gating the stash on
        # it would silently drop heat for a tail-only persona.md — the two
        # reads are of different halves of one string and sequenced as such.
        content = persona.get("content")
        tail = content.split(_NAV_MARKER, 1)[1] if isinstance(content, str) and _NAV_MARKER in content else ""
        self._nav_scenes = _parse_scene_nav(tail)
        return self._persona_from(persona)

    def _safe_scenes(self) -> list[dict]:
        """The L2 index, stashed on the search cycle — the same
        cache-invalidation rhythm as the search itself (episode start, a
        successful rewrite, the next counted extract tick)."""
        try:
            return self._client.scenario_ls(team_id=TEAM_ID, agent_id=AGENT_ID, user_id=self.effective_user_id())
        except Exception:
            logger.exception("scene-index read failed; the L2 section is omitted this cycle")
            return []

    def _persona_from(self, data: dict) -> dict | None:
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        content = content.split(_NAV_MARKER, 1)[0].rstrip()
        if not content.strip():
            return None
        hit = {
            "id": _PERSONA_HIT_ID,
            "content": content,
            "score": None,  # score-less pseudo-hit: dropped under any recall_min_score floor
        }
        created = data.get("created_at")
        if isinstance(created, str) and created:
            hit["created_at"] = created
        return hit

    def _recall_sections(self) -> str:
        parts = [RECALL_TITLE, RECALL_LEAD_IN]
        scene_lines = self._scene_index_lines()
        if scene_lines:
            parts.extend(
                [
                    "",
                    SCENE_INDEX_TITLE,
                    SCENE_INDEX_LEAD_IN,
                    *scene_lines,
                    "",
                    SCENE_READ_GUIDE.format(
                        team_id=TEAM_ID, agent_id=AGENT_ID, user_id=self.effective_user_id()
                    ),
                ]
            )
        # The conversation-search guide is unconditional WITHIN the header
        # (the scene-read guide stays scene-gated): like native's
        # always-registered tool it is available whenever the header reaches
        # the model, so it must not wait on the L2 index's presence.
        # "Unconditional" is within the header only — the base composes the
        # header only alongside >= 1 delivered line, so an all-empty recall
        # still injects nothing at all (native's own gate too).
        parts.extend(
            [
                "",
                CONVO_SEARCH_TITLE,
                CONVO_SEARCH_LEAD_IN,
                "",
                CONVO_SEARCH_GUIDE.format(
                    team_id=TEAM_ID,
                    agent_id=AGENT_ID,
                    user_id=self.effective_user_id(),
                    task_id=self._repo_key(),
                    limit=self.config.conversation_search_limit,
                ),
            ]
        )
        parts.extend(["", RECALL_SECTION_TITLE])
        return "\n".join(parts)

    def _scene_index_lines(self) -> list[str]:
        files: list[dict] = []
        for entry in self._scenes:
            path = entry.get("path")
            if not isinstance(path, str) or not path or path.endswith("/"):
                continue  # directories carry no summary and waste index slots
            files.append(entry)

        def _order(entry: dict) -> tuple[int, int]:
            # Native parity (scene-navigation.ts sorts heat-desc): entries the
            # persona's nav tail covers come first by heat descending — the
            # sort is stable, so heat ties keep ls order — and nav-less
            # entries (index lag, pre-L3 persona) trail in ls order. ls stays
            # the existence truth: a nav entry absent from ls never renders.
            nav = self._nav_scenes.get(entry["path"])
            return (0, -nav["heat"]) if nav is not None else (1, 0)

        files.sort(key=_order)
        lines: list[str] = []
        for entry in files:
            path = entry["path"]
            # summary is optional upstream (only present when the scene index
            # has an entry for the file); whitespace-collapsed into the
            # one-line shape at FULL length — the section has no host-side
            # caps (native parity: upstream's nav lists the full index).
            summary = entry.get("summary")
            summary = " ".join(summary.split()) if isinstance(summary, str) and summary.strip() else ""
            nav = self._nav_scenes.get(path)
            if nav is None:
                lines.append(f"- {path} — {summary}" if summary else f"- {path}")
                continue
            decoration = f"heat {nav['heat']}"
            stamp = nav.get("updated")
            if stamp:
                # Verbatim, whatever token the parser captured: the stamp is
                # model-written text — never parsed, reformatted, or validated.
                decoration += f", updated {stamp}"
            lines.append(f"- {path} — {decoration} — {summary}" if summary else f"- {path} — {decoration}")
        return lines

    def _hit_origin(self, hit: dict) -> str | None:
        # atomic/search hits carry no session_id (cross-session by design):
        # created_at maps onto the episode windows; unresolvable hits get the
        # non-empty sentinel so the base renders the generic cross-episode
        # suffix (a None origin renders no suffix at all).
        if not isinstance(hit, dict):
            return _UNKNOWN_ORIGIN
        created = hit.get("created_at")
        if not isinstance(created, str) or not created:
            return _UNKNOWN_ORIGIN
        return self._origin_session(created) or _UNKNOWN_ORIGIN

    def _hit_budget_exempt(self, hit) -> bool:
        # The persona pseudo-hit is the arm's one budget-exempt layer (native
        # parity: applyRecallBudget governs the L1 memory lines only, the
        # persona injects unbounded). Outside the budget the persona can no
        # longer crowd out L1 lines — the old quarter-share cap's mechanism
        # does not exist anymore.
        return isinstance(hit, dict) and hit.get("id") == _PERSONA_HIT_ID

    def _hit_score(self, hit: dict) -> float | None:
        if not isinstance(hit, dict):
            return None
        return normalize_score(hit.get("score"))

    def _render_line(self, hit: dict) -> str:
        if not isinstance(hit, dict):
            return ""
        content = hit.get("content")
        if not isinstance(content, str):
            return ""
        if hit.get("id") == _PERSONA_HIT_ID:
            # Unbounded (budget-exempt, native parity) — no local cap. The
            # whitespace collapse stays: the line-based payload needs the
            # persona as ONE rendered line, so collapsing is shape
            # normalization, not truncation.
            collapsed = " ".join(content.split())
            return f"{PERSONA_LINE_PREFIX} {collapsed}" if collapsed else ""
        content = content.strip()
        if not content:
            return ""
        # No local pre-truncation: the base's rank-then-fill owns line
        # truncation (the per-memory cap, then truncate-to-fit at the floor).
        return f"- {content}"

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------
    def _final_dump(self) -> list[dict]:
        if self._client is None:
            return []
        rows = self._client.atomic_query(
            team_id=TEAM_ID,
            agent_id=AGENT_ID,
            user_id=self.effective_user_id(),
            task_id=self._repo_key(),
        )
        final_memories = [
            {
                "id": row.get("id"),
                "content": row.get("content"),
                "background": row.get("background"),
                "version": row.get("version"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "origin": self._hit_origin(row),
            }
            for row in rows
            if row.get("id")
        ]
        self._log_event("finalize_dump", memories=len(final_memories))
        return final_memories

    def _memory_json_fields(self) -> dict:
        return {"effective_user_id": self.effective_user_id()}

    def _stats_extras(self) -> dict:
        return {"user_id": self.effective_user_id(), "api_base_url": self._settings.get("api_base_url", "")}

"""Host-side mem0 backend for the automatic-extraction arm.

Owns the mem0 policy for one SWE-bench episode on top of the shared lifecycle
skeleton (``shared_bridge.backend.BaseMemoryBackend``): buffered message
recording, periodic extraction adds, transient recall over the store's search,
and the final dump. The event log and memory.json artifact are the base's.

``config.mode`` selects the deployment the store talks to: ``platform`` (the
hosted API — its hosted extraction stands in for a local extraction lane),
``server`` (a per-run self-hosted OSS server container; extraction runs inside
the container against the provider upstream), or ``library`` (the in-process
``mem0ai`` engine; extraction runs in this process against the provider
upstream). Extraction traffic is NEVER recorded through the trajectory proxy
in any mode — the memory lane stays a zero-model-call annotate namespace.

When the benchmark model runs through a trajectory-proxy, the shared base
annotates the run's memory protocol (schema v6): one trace session per
episode, extraction adds as generation operations whose changes are the
engine's own ADD/UPDATE/DELETE receipts (``native_receipt`` evidence), and
recalls as search + main delivery. The memory lane carries no model traffic,
so its annotate endpoint comes from the explicit
``MEMORY_ANNOTATE_MEMORY_URL`` env / ``annotate_memory_url`` config. Tracing
is pure observability: every annotation failure degrades to untraced native
work, never to a behavior change.

Scoping: run isolation comes from the effective user id in every mode
(platform's store is hosted and persistent across run roots; server/library
add fresh per-run stores on top). ``scope=run`` keeps the configured user id
for the whole run root (cross-instance recall); ``scope=instance`` appends
the instance id. The driver mints the configured user_id from the timestamped
run-root name, so a fresh run root never recalls a previous run's memories —
and a stale attempt inside a run root is refused for the same reason CURE
refuses it (the rerun would recall the aborted attempt's memories).
"""

import hashlib
import logging
import os

from shared_bridge.annotate import inline_text_ref, normalize_score, sanitize_url, text_sha256
from shared_bridge.backend import BaseMemoryBackend, _BackendUnavailable, _config_or_env, _new_session_id

from mem0_bridge.config import Mem0Config
from mem0_bridge.prompts import RECALL_LEAD_IN, RECALL_SECTION_TITLE, RECALL_TITLE
from mem0_bridge.stores import Mem0Store, open_store

logger = logging.getLogger("mem0_bridge.backend")

_SUPPORTED_ROLES = frozenset({"user", "assistant", "system"})
# The final memory dump is diagnostic: the store paginates to exhaustion up to
# this ceiling, far above what a run root's episodes produce.
_FINAL_DUMP_LIMIT = 10000
_ADAPTER_NAME = "mem0"

try:
    from importlib.metadata import version as _pkg_version

    _BRIDGE_VERSION = _pkg_version("mem0-bridge")
except Exception:  # source tree without installed metadata
    _BRIDGE_VERSION = "0.1.0"


class Mem0Backend(BaseMemoryBackend):
    """Drives the mem0 Platform's extraction lifecycle for one SWE-bench episode."""

    _COUNTERS = ("memories_added", "memories_updated", "memories_deleted", "search_calls")

    # The platform's add endpoint accepts advisory per-request extraction
    # instructions, so the shared extraction guidelines have a channel:
    # they ride every extraction add as custom_instructions.
    _CONVEYS_EXTRACTION_GUIDELINES = True

    def __init__(self, config: Mem0Config, instance_id: str, model_base_url: str = ""):
        super().__init__(config, instance_id, model_base_url)  # the main lane's annotate URL derives from it
        self._store: Mem0Store | None = None
        self._pending: list[dict] = []  # recorded messages not yet flushed to the store
        # Trace identity scheme per mode (recorded in MemoryRefs): the same
        # engine build means the same id semantics, but the deployment surface
        # is part of what an id identifies.
        self._identity_scheme = f"mem0-{config.mode}-memory-v1"

    # ------------------------------------------------------------------
    # Startup (base template hooks)
    # ------------------------------------------------------------------
    def _initial_settings(self) -> dict:
        return {
            "mode": self.config.mode,
            "bridge_version": _BRIDGE_VERSION,
            "api_base_url": "",
            "infer": self.config.infer,
            "poll_budget": self.config.poll_budget,
            "poll_interval": self.config.poll_interval,
            "search_threshold": self.config.search_threshold,
            **self._core_initial_settings(),
        }

    def _resolve_settings(self) -> dict:
        """Mode-dispatched connection settings; config values win over env."""
        mode = self.config.mode
        if mode == "platform":
            api_key = _config_or_env(self.config.api_key, "MEM0_API_KEY")
            if not api_key:
                raise _BackendUnavailable("missing API key: set agent.memory.api_key or the MEM0_API_KEY env")
            base_url = _config_or_env(self.config.base_url, "MEM0_BASE_URL") or "https://api.mem0.ai"
            return {
                "mode": mode,
                "api_key": api_key,
                "base_url": base_url.rstrip("/"),
                "poll_budget": self.config.poll_budget,
                "poll_interval": self.config.poll_interval,
            }
        if mode == "server":
            server_url = _config_or_env(self.config.server_url, "MEM0_SERVER_URL")
            if not server_url:
                raise _BackendUnavailable(
                    "server mode needs agent.memory.server_url or the MEM0_SERVER_URL env "
                    "(the driver mints it per run)"
                )
            self._embedding_quartet(mode)
            return {
                "mode": mode,
                "server_url": server_url.rstrip("/"),
                "server_api_key": _config_or_env(self.config.server_api_key, "MEM0_SERVER_API_KEY"),
            }
        # library: the in-process engine pointed at the provider upstream. The
        # roster MODEL/API_KEY/BASE_URL are read, never OPENAI_* — the driver
        # overwrites OPENAI_* with the proxy lane URL (recorded traffic), and
        # library extraction must stay OFF the trajectory in every mode.
        if not self.config.run_root:
            raise _BackendUnavailable("library mode needs agent.memory.run_root (the driver passes $RUN_ROOT)")
        quartet = self._embedding_quartet(mode)
        model = os.environ.get("MODEL", "")
        api_key = os.environ.get("API_KEY", "")
        base_url = os.environ.get("BASE_URL", "")
        if not (model and api_key and base_url):
            raise _BackendUnavailable(
                "library mode needs the roster MODEL/API_KEY/BASE_URL env (the driver exports them per run)"
            )
        return {
            "mode": mode,
            "run_root": self.config.run_root,
            "llm_model": model,
            "llm_api_key": api_key,
            "llm_base_url": base_url,
            **quartet,
        }

    @staticmethod
    def _embedding_quartet(mode: str) -> dict:
        """The all-four-or-unavailable embedding settings. The OSS engine
        embeds on every add and every search with no lexical-only fallback, so
        server and library modes fail closed without the complete quartet: a
        missing embedder otherwise boots healthy and dies on the first add."""
        quartet = {
            "embedding_model": os.environ.get("EMBEDDING_MODEL", ""),
            "embedding_api_key": os.environ.get("EMBEDDING_API_KEY", ""),
            "embedding_base_url": os.environ.get("EMBEDDING_BASE_URL", ""),
            "embedding_dimensions": os.environ.get("EMBEDDING_DIMENSIONS", ""),
        }
        missing = [f"EMBEDDING_{name.removeprefix('embedding_').upper()}" for name, value in quartet.items() if not value]
        if missing:
            raise _BackendUnavailable(
                f"{mode} mode needs the full EMBEDDING_* quartet in the roster .env (missing: {', '.join(missing)})"
            )
        try:
            dimensions = int(quartet["embedding_dimensions"])
        except ValueError:
            dimensions = 0
        if dimensions <= 0:
            raise _BackendUnavailable(
                f"{mode} mode needs EMBEDDING_DIMENSIONS to be a positive integer "
                f"(got {quartet['embedding_dimensions']!r})"
            )
        quartet["embedding_dimensions"] = dimensions
        return quartet

    def _open_store(self, settings: dict) -> Mem0Store:
        """Isolated store constructor (the test seam)."""
        return open_store(settings["mode"], settings)

    def _startup(self, settings: dict) -> None:
        # Only the safe form is persisted: no userinfo/query/fragment — the
        # store below keeps the real URL (project rule 4). For library mode
        # the recorded URL is the extraction's LLM upstream (there is no
        # per-run server to point at); for server mode the per-run container.
        mode = settings["mode"]
        if mode == "platform":
            self._settings["api_base_url"] = sanitize_url(settings["base_url"])
        elif mode == "server":
            self._settings["api_base_url"] = sanitize_url(settings["server_url"])
        else:
            self._settings["api_base_url"] = sanitize_url(settings["llm_base_url"])
        self._store = self._open_store(settings)
        self._store.health()  # fail fast on bad credentials or endpoint
        # Unique per episode: stamped as the engine-side run_id so memories
        # attribute to one agent episode (and recall provenance can read it).
        self._session_id = _new_session_id(self.instance_id)

    def _reset_extras(self) -> None:
        self._pending = []

    def _start_event_extras(self) -> dict:
        return {
            "mode": self.config.mode,
            "user_id": self.effective_user_id(),
            "api_base_url": self._settings["api_base_url"],
        }

    def _close(self) -> None:
        # The handle is always nulled, even when close fails; the error
        # propagates to the base's call sites (containment + logging there).
        if self._store is not None:
            try:
                self._store.close()
            finally:
                self._store = None

    # ------------------------------------------------------------------
    # Trace adapter hooks (the protocol machinery lives in the base)
    # ------------------------------------------------------------------
    def _adapter_meta(self) -> dict:
        return {"name": _ADAPTER_NAME, "version": _BRIDGE_VERSION}

    def _trace_namespace(self) -> str:
        # The effective user id is the store scope (the sole
        # retrieval-isolation boundary); the hash keeps the run-root-derived
        # id itself out of the recorded refs.
        return hashlib.sha256(self.effective_user_id().encode()).hexdigest()

    def _trace_context(self) -> dict:
        return {"session_id": self._session_id, "user_id": self.effective_user_id()}

    def _memory_ref(self, hit: dict) -> dict:
        """One engine memory as a portable native-stable MemoryRef.

        The mem0 id is stable across UPDATE versions, so the version id
        pairs it with the content digest (satisfying the recorder's
        one-version-id-one-content join rule) while ``item_id`` carries the
        bare id. Callers drop id-less rows first
        (``_search``/``_attribute_changes``): fabricating an id would
        collapse every such row into one item.
        """
        memory_id = str(hit["id"])  # id-less rows are dropped at the intake points (uncitable)
        text = hit.get("memory")
        if isinstance(text, str) and text:
            content = inline_text_ref(text)
            version_id = f"{memory_id}:{text_sha256(text)}"
        else:
            # A DELETE receipt may carry no text: the version cites the id only.
            content = {"availability": "unavailable", "reason": "no_platform_text"}
            version_id = f"{memory_id}:unavailable"
        extensions = {}
        for key in ("user_id", "run_id", "created_at", "updated_at"):
            value = hit.get(key)
            if value is not None:
                extensions[key] = value
        # An unusable score is dropped, never coerced to 0.0 — that would
        # fabricate ranking evidence.
        score = normalize_score(hit.get("score"))
        if score is not None:
            extensions["score"] = score
        return {
            "version_id": version_id,
            "identity_strength": "native_stable",
            "identity_scheme": self._identity_scheme,
            "item_id": memory_id,
            "namespace": self._namespace,
            "content": content,
            "extensions": {_ADAPTER_NAME: extensions},
        }

    def _attribute_changes(self, operation, result, after):
        """Engine receipts -> the change series (evidence: native_receipt).

        No mode offers a before-image (no extra get_all per flush —
        a recorded fidelity limit), so every change is completeness=partial.
        NONE receipts emit no change — a zero-change generation is legal
        (change_count: 0). ``result`` is None on the exception path: a failed
        add returns no receipts.
        """
        if result is None:
            return [], [], []
        changes: list[dict] = []
        produced: list[dict] = []
        for item in result:
            if not item.get("id"):
                # A receipt without a platform id can never be cited or
                # grouped; the native counters still count its event.
                continue
            event = str(item.get("event") or "").upper()
            extensions = {_ADAPTER_NAME: {"event": event}}
            if event == "ADD":
                changes.append(
                    self._change_payload(
                        operation, "create", [], [item],
                        evidence="native_receipt", extensions=extensions, completeness="partial",
                    )
                )
                produced.append(self._memory_ref(item))
            elif event == "UPDATE":
                changes.append(
                    self._change_payload(
                        operation, "update", [], [item],
                        evidence="native_receipt", extensions=extensions, completeness="partial",
                    )
                )
                produced.append(self._memory_ref(item))
            elif event == "DELETE":
                changes.append(
                    self._change_payload(
                        operation, "delete", [item], [],
                        evidence="native_receipt", extensions=extensions, completeness="partial",
                    )
                )
            # NONE: the platform deduped/ignored the fact — no change.
        return changes, produced, []

    def _generation_end_context(self, step, result, audit: dict) -> dict:
        """extensions.mem0 for the generation end: the engine event counts
        (absent on the exception path, which has no receipts)."""
        context = {"session_id": self._session_id, "extraction_step": str(step), "user_id": self.effective_user_id()}
        if result is not None:
            counts = {"ADD": 0, "UPDATE": 0, "DELETE": 0, "NONE": 0}
            for item in result:
                event = str(item.get("event") or "").upper()
                if event in counts:
                    counts[event] += 1
            context.update(
                {
                    "added": counts["ADD"],
                    "updated": counts["UPDATE"],
                    "deleted": counts["DELETE"],
                    "none": counts["NONE"],
                }
            )
        return context

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def _normalize_role(self, role: str) -> str:
        # The platform accepts only these roles; anything else folds to user.
        return role if role in _SUPPORTED_ROLES else "user"

    def _should_store(self, text: str) -> bool:
        return bool(text)

    def _store_message(self, role: str, text: str, step: int) -> None:
        self._pending.append({"role": role, "content": text})

    # ------------------------------------------------------------------
    # Extraction (hosted)
    # ------------------------------------------------------------------
    def _perform_extraction(self, step) -> None:
        """Flush the pending buffer as one store add (the extraction).

        The buffer is retained across failures and cleared only on success, so
        a failed batch is retried at the next boundary (the engine dedupes
        re-sent facts, and a timeout does not silently lose messages). Traced
        as one core generation operation around the unchanged native call; any
        annotation failure leaves the native path untouched.
        """
        if self._store is None or not self._pending:
            return
        self._counts["extraction_calls"] += 1
        n_messages = len(self._pending)
        try:
            operation = self._generation_begin(step)
        except Exception:
            logger.exception("annotation generation-begin failed; extraction continues untraced")
            operation = None
        try:
            results = self._store.add(
                messages=list(self._pending),
                user_id=self.effective_user_id(),
                run_id=self._session_id,
                infer=self.config.infer,
                guidelines=self._extraction_guidelines() or None,
            )
        except Exception as e:
            try:
                self._generation_finish_exception(operation, step, e)
            except Exception:
                logger.exception("annotation generation-end failed; extraction continues untraced")
            raise  # hard failure: the base shell counts, registers, and gates
        added = updated = deleted = 0
        for item in results:
            event = str(item.get("event") or "").upper()
            if event == "ADD":
                added += 1
            elif event == "UPDATE":
                updated += 1
            elif event == "DELETE":
                deleted += 1
            elif event != "NONE":
                # Not an extraction failure (the engine answered fine), but
                # never silent: an unknown event name under-reports otherwise.
                logger.warning("mem0 add returned a receipt with an unrecognized event: %r", item.get("event"))
        self._counts["memories_added"] += added
        self._counts["memories_updated"] += updated
        self._counts["memories_deleted"] += deleted
        self._consecutive_errors = 0
        self._pending.clear()
        if self._trace is not None:
            self._trace.pending_inputs.clear()
        self._log_event(
            "extraction",
            step=step,
            messages=n_messages,
            added=added,
            updated=updated,
            deleted=deleted,
        )
        try:
            # No native soft errors: a platform failure raises above and takes
            # the exception path, so the error list is always empty here.
            self._generation_finish(operation, step, results, [])
        except Exception:
            logger.exception("annotation generation-end failed; extraction continues untraced")

    def _register_extraction_failure(self, step, error: str, *, log_event: bool = True) -> None:
        logger.error("mem0 extraction failed: %s", error)
        super()._register_extraction_failure(step, error, log_event=log_event)

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------
    def _search(self) -> list:
        # Readiness guard first, before the private counter: an unready call
        # is not a counted search. Post-finalize dormancy is the base's.
        if self._store is None:
            return []
        self._counts["search_calls"] += 1
        # With a host-side floor set, the engine's top_k cut would otherwise
        # truncate the pool before the floor filters it and the fill could
        # starve with no way to backfill: request a wider pool (a hypothesis
        # to tune on the next verify pair, like the floor value itself).
        top_k = max(50, self.config.max_memories) if self.config.recall_min_score is not None else self.config.max_memories
        try:
            hits = self._store.search(
                query=self._recall_query() or "",
                user_id=self.effective_user_id(),
                top_k=top_k,
                threshold=self.config.search_threshold,
                timeout=self.config.search_timeout,
            )
        except Exception:
            self._counts["search_errors"] += 1
            raise
        # A hit without an engine id is malformed and uncitable (the endpoint
        # adapter drops it too): never rendered, never traced. Non-dict rows
        # pass through to the renderer's own malformed-line skip.
        return [hit for hit in hits if not isinstance(hit, dict) or hit.get("id")]

    def _recall_sections(self) -> str:
        return "\n".join([RECALL_TITLE, RECALL_LEAD_IN, "", RECALL_SECTION_TITLE])

    def _hit_origin(self, hit: dict) -> str | None:
        # The engine echoes the add-time run_id (the backend's per-episode
        # session id) on every search row — the provenance signal.
        if not isinstance(hit, dict):
            return None
        origin = hit.get("run_id")
        return origin if isinstance(origin, str) and origin else None

    def _hit_score(self, hit: dict) -> float | None:
        # The mode's native score (platform: the combined multi-signal score,
        # 0-1; OSS: the hybrid-retrieval score — scales never compared across
        # modes); an unusable one drops (never coerced) like in refs.
        if not isinstance(hit, dict):
            return None
        return normalize_score(hit.get("score"))

    def _render_line(self, hit: dict) -> str:
        # A malformed hit (non-dict row, non-string memory) is an unrenderable
        # line: skip it like an empty one rather than failing the whole recall.
        if not isinstance(hit, dict):
            return ""
        memory = hit.get("memory")
        if not isinstance(memory, str):
            return ""
        memory = memory.strip()
        return f"- {memory}" if memory else ""

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------
    def _final_dump(self) -> list[dict]:
        if self._store is None:
            return []
        rows = self._store.get_all(user_id=self.effective_user_id(), limit=_FINAL_DUMP_LIMIT)
        final_memories = [
            {
                "id": item.get("id"),
                "memory": item.get("memory"),
                # Provenance: the platform's v3 get-all surface omits run_id and
                # carries the same value under session_id; the OSS surfaces
                # promote the payload's run_id directly. Read both.
                "run_id": item.get("run_id") or item.get("session_id"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in rows
        ]
        self._log_event("finalize_dump", memories=len(final_memories))
        return final_memories

    def _memory_json_fields(self) -> dict:
        return {"effective_user_id": self.effective_user_id()}

    def _stats_extras(self) -> dict:
        return {"user_id": self.effective_user_id(), "api_base_url": self._settings.get("api_base_url", "")}

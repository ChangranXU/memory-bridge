"""The fake reference integration for the shared-bridge suites.

FakeBackend is the minimal integration over the scripted in-memory FakeSystem
— every hook it implements is one the base documents, so a third
integration's author can read it as the contract guide. A plain module (not
the conftest) so same-named conftests in the sibling integration suites can
never shadow it.
"""

import hashlib
import logging

from shared_bridge.annotate import inline_text_ref, text_sha256
from shared_bridge.backend import BaseMemoryBackend, _BackendUnavailable, _new_session_id
from shared_bridge.config import MemoryConfig

logger = logging.getLogger("shared_bridge.tests")


class FakeSystem:
    """Scripted stand-in for an integration's memory-system handle."""

    def __init__(self):
        self.pending: list[dict] = []
        self.rows: list[str] = []  # extracted memories (the store's live set)
        self.extraction_guidelines = ""  # the guidelines handed to the extraction engine
        self.hits: list = []
        self.search_calls = 0  # native search invocations (the dirty-flag cache's yardstick)
        self.origins: dict = {}  # hit -> origin session id (the _hit_origin seam)
        self.scores: dict = {}  # hit -> relevance score (the _hit_score seam)
        self.dump_rows: list[dict] = []
        self.store_error: Exception | None = None
        self.extract_error: Exception | None = None
        self.extract_soft_error: str | None = None
        self.search_error: Exception | None = None
        self.dump_error: Exception | None = None
        self.close_error: Exception | None = None
        self.closed = False
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeBackend(BaseMemoryBackend):
    """The minimal integration: one handle, one pending buffer, one extra
    counter (``widgets``), and the three required trace adapter hooks. The
    extraction turns each pending message into one stored row, so the base's
    generic observed diff audits the generation."""

    _COUNTERS = ("widgets",)

    # The fake's extraction engine accepts prompt rules, so it demonstrates the
    # guidelines contract end to end (a real integration composes them into its
    # policy prompt or sends them as its platform's advisory field).
    _CONVEYS_EXTRACTION_GUIDELINES = True

    def __init__(self, config, instance_id="fake-instance", model_base_url=""):
        super().__init__(config, instance_id, model_base_url)
        self.system: FakeSystem | None = None
        self.fail_resolve = False
        self.raise_in_render = False

    # Startup hooks
    def _initial_settings(self):
        return {"api_base_url": "", "strict": self.config.strict}

    def _resolve_settings(self):
        if self.fail_resolve:
            raise _BackendUnavailable("fake settings missing")
        return {"base_url": "https://fake.invalid"}

    def _startup(self, settings):
        self._settings["api_base_url"] = settings["base_url"]
        self.system = FakeSystem()
        self.system.extraction_guidelines = self._extraction_guidelines()
        self._session_id = _new_session_id(self.instance_id)

    def _close(self):
        if self.system is not None:
            self.system.close()

    # Trace adapter hooks
    def _adapter_meta(self):
        return {"name": "fake", "version": "0.0.1"}

    def _trace_namespace(self):
        return hashlib.sha256(b"fake-store").hexdigest()

    def _memory_ref(self, obj):
        digest = text_sha256(obj)
        return {
            "version_id": f"row-{digest[:16]}",
            "identity_strength": "derived_content",
            "identity_scheme": "fake-row-v1",
            "item_id": f"item-{digest[:16]}",
            "namespace": self._namespace,
            "content": inline_text_ref(obj),
            "extensions": {"fake": {"chars": len(obj)}},
        }

    def _snapshot_memory_state(self):
        return {index: row for index, row in enumerate(self.system.rows)}

    # Record hooks
    def _store_message(self, role, text, step):
        if self.system.store_error is not None:
            raise self.system.store_error
        self.system.pending.append({"role": role, "content": text, "step": step})
        self._counts["widgets"] += 1
        # No native message id: the base numbers the pending input synthetically.

    # Extraction
    def _perform_extraction(self, step):
        if self.system is None or not self.system.pending:
            return  # readiness guard before any counting: an empty tick is free
        self._counts["extraction_calls"] += 1
        try:
            operation = self._generation_begin(step)
        except Exception:
            logger.exception("annotation generation-begin failed; extraction continues untraced")
            operation = None
        if self.system.extract_error is not None:
            error = self.system.extract_error
            try:
                self._generation_finish_exception(operation, step, error)
            except Exception:
                logger.exception("annotation generation-end failed; extraction continues untraced")
            raise error  # hard failure: the shell counts/registers/gates
        if self.system.extract_soft_error is not None:
            # Soft failure (the CURE shape): no rows written, the checkpoint
            # held (pending kept); the error is counted and registered with no
            # event duplicate, never raised — and the traced operation still
            # closes with the native soft-error list.
            soft_error = self.system.extract_soft_error
            self._counts["extraction_errors"] += 1
            self._register_extraction_failure(step, soft_error, log_event=False)
            try:
                self._generation_finish(operation, step, [], [soft_error])
            except Exception:
                logger.exception("annotation generation-end failed; extraction continues untraced")
            return
        new_rows = [message["content"] for message in self.system.pending]
        self.system.rows.extend(new_rows)
        self.system.pending.clear()
        self._consecutive_errors = 0
        if self._trace is not None:
            self._trace.pending_inputs.clear()
        try:
            self._generation_finish(operation, step, new_rows, [])
        except Exception:
            logger.exception("annotation generation-end failed; extraction continues untraced")

    # Recall
    def _search(self):
        if self.system is None:
            return []
        self.system.search_calls += 1
        if self.system.search_error is not None:
            self._counts["search_errors"] += 1  # the private counting the base contract describes
            raise self.system.search_error
        return self.system.hits

    def _hit_origin(self, hit):
        return self.system.origins.get(hit)

    def _hit_score(self, hit):
        return self.system.scores.get(hit)

    def _recall_sections(self):
        return "## Fake Memories"

    def _render_line(self, hit):
        if self.raise_in_render:
            raise RuntimeError("render boom")
        return f"- {hit}" if str(hit).strip() else ""

    # Finalize
    def _final_dump(self):
        if self.system is None:
            return []
        if self.system.dump_error is not None:
            raise self.system.dump_error
        return list(self.system.dump_rows)


def _config(output_dir, **overrides):
    return MemoryConfig(enabled=True, output_dir=str(output_dir), **overrides)


def _started(output_dir, **overrides):
    backend = FakeBackend(_config(output_dir, **overrides))
    backend.start()
    assert backend._available
    return backend

"""Memory agent (automatic-extraction arm): lifecycle + record/extract/recall hooks.

The benchmark model sees only the stock bash tool — no memory tools, no prompt
nudges, no model subclass. With ``agent.memory.enabled=false`` this agent is a
no-op wrapper and the model-visible trajectory is byte-identical to baseline.

This module is integration-agnostic: an integration package subclasses
``MemoryAgent`` and binds ``backend_class`` (and usually ``config_class``).
"""

import logging
import os

from pydantic import Field

from minisweagent.agents.default import AgentConfig
from minisweagent.run.benchmarks.utils.common import ProgressTrackingAgent

from shared_bridge.config import MemoryConfig

logger = logging.getLogger("shared_bridge.agent")


class MemoryAgentConfig(AgentConfig):
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


class MemoryAgent(ProgressTrackingAgent):
    """ProgressTrackingAgent with host-side memory recording/extraction/recall."""

    config_class: type[MemoryAgentConfig] = MemoryAgentConfig
    # Integration subclasses bind their memory backend here. The backend drives
    # the memory lifecycle: start -> set_task -> record -> maybe_extract ->
    # recall_context/note_recall/deliver_recall -> finalize, plus stats().
    backend_class: type | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, config_class=type(self).config_class, **kwargs)
        self._mem = None
        self._last_run_stats: dict | None = None

    def _make_backend(self):
        if type(self).backend_class is None:
            raise RuntimeError(
                f"{type(self).__name__}.backend_class is not set: agent.memory.enabled=true "
                "requires an integration subclass that binds a memory backend"
            )
        return type(self).backend_class(
            self.config.memory, self.instance_id, model_base_url=self._main_model_base_url()
        )

    def run(self, task="", **kwargs):
        if not self.config.memory.enabled:
            return super().run(task, **kwargs)

        # Constructed inside run(): the SQLite connection must live on the
        # SWE-bench worker thread that uses it.
        mem = self._mem = self._make_backend()
        primary: BaseException | None = None
        try:
            # start() stays inside the guarded block: strict startup can fail
            # after opening SQLite and must not bypass finalization.
            mem.start()
            mem.set_task(task)
            return super().run(task, **kwargs)
        except BaseException as e:
            primary = e
            raise
        finally:
            finalize_error = None
            try:
                mem.finalize()
            except Exception as e:
                finalize_error = e
                logger.exception("memory finalize failed")
            finally:
                self._mem = None
                try:
                    self._last_run_stats = mem.stats()
                except Exception:
                    self._last_run_stats = None
            # A finalize exception never masks a primary agent/startup exception.
            if finalize_error is not None and primary is None and self.config.memory.strict:
                raise finalize_error

    def add_messages(self, *messages):
        """Recording hook: every trajectory message flows through here."""
        added = super().add_messages(*messages)
        if self._mem is not None:
            self._mem.record(added, step=self.n_calls)
        return added

    def step(self):
        """Extraction + rewrite ticks: fire only after clean steps; missed
        boundaries are serviced by the backend's high-water schedules on the
        next clean step. The rewrite tick runs after this step's query(), so
        its duration is consumed at the next query()'s wall-time preflight."""
        result = super().step()
        if self._mem is not None:
            self._mem.maybe_extract(self.n_calls)
            self._mem.maybe_rewrite(self.n_calls)
        return result

    def query(self):
        """Transient recall injection: reaches the model, never persists.

        Annotation I/O and native search/rewrite time are excluded from the
        inherited wall-time preflight by shifting ``_start_time`` forward — a
        slow recorder or a slow hosted search must never change whether the
        next benchmark model call is attempted. The delivery binds at the
        main-lane cursor read immediately before placement, so exactly this
        query's model call(s) form its verified interval."""
        marker = None
        recall = None
        msg_index = None
        cursor = None
        query_error = None
        calls_before = self.n_calls
        if self._mem is not None:
            recall = self._mem.recall_context(planned_step=self.n_calls + 1)
            if recall:
                try:
                    cursor = self._mem.main_lane_cursor()
                except Exception:
                    # Annotation hooks never change native behavior: an
                    # unreadable cursor only costs this block its delivery.
                    logger.exception("main-lane cursor read failed")
                msg_index = len(self.messages)
                # Direct append: bypasses add_messages, so it is never recorded.
                marker = {
                    "role": "user",
                    "content": recall["content"],
                    "extra": {"transient_recall": True},
                }
                self.messages.append(marker)
            # The search posts, the cursor read, AND the native search/rewrite
            # seconds are exempted I/O: consume before super().query()'s
            # wall-time preflight, not after it.
            try:
                self._start_time += self._mem.consume_annotation_duration()
            except Exception:
                logger.exception("annotation duration accounting failed")
        try:
            return super().query()
        except BaseException as e:
            query_error = e
            raise
        finally:
            if marker is not None:
                # Remove by identity: the assistant reply may already follow it.
                for i in range(len(self.messages) - 1, -1, -1):
                    if self.messages[i] is marker:
                        del self.messages[i]
                        break
                # DefaultAgent increments n_calls immediately before model.query(),
                # so this counts actual model-call attempts only (not limit preflights).
                if self.n_calls > calls_before and self._mem is not None:
                    try:
                        self._mem.note_recall(recall, step=self.n_calls)
                    except Exception:
                        logger.exception("memory recall accounting failed")
                    # A raised query may have failed client-side before any request
                    # reached the proxy: posting the delivery then would bind a
                    # provable "placed" claim to an empty interval (no_call). Probe
                    # the lane cursor and deliver only if a call actually landed;
                    # a server-side failure folds normally and still delivers.
                    deliver = True
                    if query_error is not None and cursor is not None:
                        try:
                            advanced = self._mem.main_lane_cursor()
                        except Exception:
                            advanced = None
                        if advanced is None or advanced <= cursor:
                            deliver = False
                            logger.warning(
                                "memory delivery skipped: the failed model call left no proxy-visible call"
                            )
                            try:
                                # The injection stays counted (the block was placed
                                # and the call attempted); the marker keeps the
                                # missing trajectory delivery visible in memory.json.
                                self._mem.note_undelivered_recall(step=self.n_calls)
                            except Exception:
                                logger.exception("memory recall accounting failed")
                    if deliver:
                        try:
                            self._mem.deliver_recall(recall, step=self.n_calls, msg_index=msg_index, cursor=cursor)
                        except Exception:
                            logger.exception("memory delivery annotation failed")
            if self._mem is not None:
                # The delivery post and crash-probe cursor read above are annotation I/O too.
                try:
                    self._start_time += self._mem.consume_annotation_duration()
                except Exception:
                    logger.exception("annotation duration accounting failed")

    def _main_model_base_url(self) -> str:
        """The main lane's effective model base URL for annotation derivation:
        the model's configured api_base first, then $OPENAI_BASE_URL."""
        model_config = getattr(self.model, "config", None)
        model_kwargs = getattr(model_config, "model_kwargs", None)
        if isinstance(model_kwargs, dict):
            api_base = model_kwargs.get("api_base")
            if isinstance(api_base, str) and api_base.strip():
                return api_base
        return os.environ.get("OPENAI_BASE_URL", "")

    def serialize(self, *extra_dicts):
        return super().serialize(*extra_dicts, {"info": {"memory": self._memory_info()}})

    def _memory_info(self):
        """Cheap and side-effect-free: run() saves after EVERY step."""
        if not self.config.memory.enabled:
            return {"enabled": False}
        if self._mem is not None:
            try:
                return {"enabled": True, **self._mem.stats()}
            except Exception:
                return {"enabled": True, "available": False}
        if self._last_run_stats is not None:
            return {"enabled": True, **self._last_run_stats}
        return {"enabled": True}

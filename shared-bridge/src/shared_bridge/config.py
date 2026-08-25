"""Memory bridge configuration (automatic-extraction arm).

Integration-agnostic: an integration package subclasses ``MemoryConfig`` to
add its own locator fields (e.g. a source-tree path for its memory system).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")  # memory config typos fail loudly

    enabled: bool = False  # THE on/off switch (default off)
    scope: Literal["run", "instance"] = "run"  # run: shared cross-instance scope; instance: fresh scope per task
    user_id: str = "minisweagent"
    output_dir: str = ""  # per-instance artifact root (= --output dir); REQUIRED when enabled
    strict: bool = False  # true -> backend errors raise (debugging); false -> fail-closed
    # recording
    max_message_chars: int = Field(default=4000, gt=0)  # exact cap, marker included
    # extraction schedule (payload/client settings are integration-owned)
    extract_every_n_steps: int = Field(default=10, ge=0)  # 0 -> final flush only
    extract_max_consecutive_errors: int = Field(default=3, ge=0)  # 0 -> never break
    # Extraction policy/guidelines text ("" -> the shared default in
    # shared_bridge.prompts.EXTRACTION_GUIDELINES_DEFAULT; non-empty = the run's
    # override, replacing the default wholesale). Either form is followed by the
    # base-composed episode context (instance + repository key). Integrations
    # convey the combined text through their native channel when they have one
    # and ignore it otherwise.
    extraction_guidelines: str = ""
    # recall
    inject_recall: bool = True
    max_memories: int = Field(default=10, gt=0)  # upper bound on delivered lines; budget may deliver fewer
    # Host-side render budgets over the rendered memory LINES (the header is
    # not counted; the payload's chars still means "what was placed"). The
    # pair mirrors a native engine's per-memory/total recall budget (see the
    # adopting integration's docs for the exact upstream kin) — host-side
    # render knobs, deliberately NOT any server-side recall limit an engine's
    # own config may expose. The per-memory cap applies first, then
    # truncate-to-fit against the total budget with a 40-char floor — both
    # suffix-bearing (shared_bridge.backend.RECALL_LINE_TRUNCATION). 0
    # disables each bound.
    max_chars_per_memory: int = Field(default=0, ge=0)  # cap on one rendered line (content + provenance suffix); 0 = off (the native default)
    max_total_recall_chars: int = Field(default=2000, ge=0)  # total over the delivered lines; 2000 preserves the shipped arm behavior — the native default-off is deliberately not adopted (pin 0 for it)
    # Bound on one native search call. Implemented only where the search is a
    # network call (a hosted platform): an in-process lexical search cannot be
    # interrupted cleanly, so a local deadline would be theater.
    search_timeout: float = Field(default=10.0, gt=0)
    # Relevance floor: hits scoring below it (or carrying no score) are dropped
    # before any quantity bound; None disables the floor. The scale is
    # integration-defined (never compare values across integrations).
    recall_min_score: float | None = None
    # Query rewrite schedule: 0 disables rewriting (the query stays the task
    # text). Default off, mirroring the "default = current behavior"
    # discipline: no arm run should depend on an LLM rewriter before rewrite
    # quality is measurable.
    rewrite_every_n_steps: int = Field(default=0, ge=0)
    rewrite_max_consecutive_errors: int = Field(default=3, ge=0)  # 0 -> never break
    # Rewriter connection ("" -> MEMORY_QUERY_MODEL / MEMORY_QUERY_MODEL_URL /
    # MEMORY_QUERY_API_KEY). The driver fills the env fallbacks from the QUERY
    # proxy lane, so the defaults need no user configuration.
    rewrite_model: str = ""
    rewrite_base_url: str = ""
    rewrite_api_key: str = Field(default="", exclude=True, repr=False)
    rewrite_timeout: float = Field(default=20.0, gt=0)
    # 1600, not 200: the default rewriter is the role-1 model, a
    # reasoning-style model whose reasoning can eat a small budget — a
    # fail-closed rewrite at 200 tokens silently keeps the task text forever
    # while paying the QUERY traffic.
    rewrite_max_tokens: int = Field(default=1600, gt=0)
    # trajectory annotation ("" -> env MEMORY_ANNOTATE_MAIN_URL / MEMORY_ANNOTATE_MEMORY_URL
    # -> derived from the lane's model base URL; otherwise that lane is untraced)
    annotate: bool = True
    # The annotate URLs embed the bearer trajectory ID: credential fields (rule 4).
    annotate_main_url: str = Field(default="", exclude=True, repr=False)
    annotate_memory_url: str = Field(default="", exclude=True, repr=False)
    annotate_timeout: float = Field(default=0.5, gt=0)
    annotate_retries: int = Field(default=1, ge=0)  # transient connection/5xx only
    annotate_max_consecutive_errors: int = Field(default=3, gt=0)  # exhausted logical requests

    @model_validator(mode="after")
    def _validate_identity_and_output(self):
        # fail fast at agent construction (inside process_instance's try -> loud
        # exit_status) instead of silently writing memory.json into the CWD
        self.user_id = self.user_id.strip()
        if not self.user_id:
            raise ValueError("agent.memory.user_id must not be blank")
        self.output_dir = self.output_dir.strip()
        if self.enabled and not self.output_dir:
            raise ValueError("agent.memory.output_dir must be set when agent.memory.enabled=true")
        return self

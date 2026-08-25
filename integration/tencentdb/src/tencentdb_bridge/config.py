"""TencentDB-Agent-Memory (MemoryCore) configuration for the memory arm."""

from pydantic import Field

from shared_bridge.config import MemoryConfig


class TencentDBConfig(MemoryConfig):
    """MemoryConfig plus the MemoryCore gateway locator and drain knobs.

    The gateway runs as one container per run root (driver-managed); auth is
    off, but ``parseV2Auth`` unconditionally demands a non-empty Bearer and a
    service id on every v2/v3 route, so the client always sends both.
    """

    endpoint: str = "http://127.0.0.1:8420"
    # The Bearer value: gateway auth is off, any non-empty string passes.
    api_key: str = Field(default="local", exclude=True, repr=False)
    service_id: str = "default"
    # Run root (driver-filled, next to output_dir): anchors the episode-window
    # sidecar at <run-root>/tdai/episodes.jsonl for cross-episode origin
    # attribution. A field, not output_dir-derived arithmetic, so tests never
    # write outside their tmp dirs.
    run_root: str = ""
    # conversation/add timeout: with the vector lane on, the gateway embeds
    # every L0 message sequentially inside the add (upstream behavior), so a
    # slow embedding provider can stretch one add well past a generic client
    # timeout — the call must not die client-side while the gateway is still
    # processing it (the buffer would be re-added wholesale next tick).
    add_timeout: float = Field(default=300.0, gt=0)
    # L1 drain budgets (seconds). The per-tick drain must never reuse
    # search_timeout (10 s — one L1 extraction-LLM cycle routinely exceeds it;
    # upstream's own LLM timeout defaults to 120 s).
    drain_budget: float = Field(default=180.0, gt=0)
    finalize_drain_budget: float = Field(default=300.0, gt=0)
    drain_interval: float = Field(default=1.0, gt=0)
    # The conversation-search limit rendered into the header's curl guide:
    # the native tool's and the wire schema's own default is 5; the route's
    # zod schema caps limit at 1..100 inline (independent of atomic/search's
    # own inline cap — SEARCH_LIMIT_MAX (client.py) is the bridge client's
    # constant for that route, not an upstream name). A typical 5-hit
    # full-content result stays under the harness's 10k observation
    # truncation; the accumulating episode file retains whatever clips.
    conversation_search_limit: int = Field(default=5, ge=1, le=100)
    # Embedding lane mode, mirrored from the generated gateway config for the
    # settings artifact (provider + model only; never a key).
    embedding_provider: str = "none"
    embedding_model: str = ""

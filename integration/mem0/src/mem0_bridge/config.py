"""mem0 integration configuration: the shared memory config plus the mem0
connection settings used by the backend. ``mode`` selects the deployment the
bridge talks to (the hosted platform, a self-hosted OSS server container, or
the in-process library); the mode is yaml-owned — the driver reads the same
line and refuses ``--config agent.memory.mode=`` extras, so the two can never
diverge."""

from typing import Literal

from pydantic import Field

from shared_bridge.config import MemoryConfig


class Mem0Config(MemoryConfig):
    mode: Literal["platform", "server", "library"] = "platform"
    api_key: str = Field(default="", exclude=True, repr=False)  # platform: "" -> $MEM0_API_KEY
    base_url: str = ""  # platform: "" -> $MEM0_BASE_URL -> https://api.mem0.ai
    server_url: str = ""  # server: "" -> $MEM0_SERVER_URL (driver-minted per run)
    server_api_key: str = Field(default="", exclude=True, repr=False)  # server: optional under AUTH_DISABLED
    run_root: str = ""  # library: store dir anchor (the driver passes $RUN_ROOT)
    infer: bool = True  # true: extraction-side fact extraction; false: verbatim storage
    # Semantics are PER SURFACE — never document a blanket "disables":
    # platform 0.0 = cutoff off (documented); OSS (server/library) 0.0 =
    # minimal gate on the raw semantic score BEFORE the hybrid combine — a
    # floor, not a switch. Either way the value is always sent explicitly.
    search_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    # Platform mode only (server/library adds are synchronous): a platform add
    # with infer=true is processed asynchronously (the API answers with an
    # event id); the backend polls the event until it is terminal so writes
    # stay synchronous from the agent's point of view. poll_budget is the
    # total add+poll budget per batch.
    poll_budget: float = Field(default=60.0, gt=0)
    poll_interval: float = Field(default=1.0, gt=0)  # async add event polling cadence

"""mem0 integration configuration: the shared memory config plus the mem0
Platform connection settings used by the backend's REST client."""

from pydantic import Field

from shared_bridge.config import MemoryConfig


class Mem0Config(MemoryConfig):
    api_key: str = Field(default="", exclude=True, repr=False)  # "" -> $MEM0_API_KEY
    base_url: str = ""  # "" -> $MEM0_BASE_URL -> https://api.mem0.ai
    infer: bool = True  # true: platform-side extraction; false: verbatim storage
    search_threshold: float = Field(default=0.0, ge=0.0, le=1.0)  # 0.0 disables the cutoff
    # A mem0 add with infer=true is processed asynchronously (the API answers
    # with an event id); the backend polls the event until it is terminal so
    # writes stay synchronous from the agent's point of view. poll_budget is
    # the total add+poll budget per batch.
    poll_budget: float = Field(default=60.0, gt=0)
    poll_interval: float = Field(default=1.0, gt=0)  # async add event polling cadence

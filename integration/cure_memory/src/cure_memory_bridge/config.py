"""CURE integration configuration: the shared memory config plus the CURE
source-tree locator and the local extraction-client settings (the
trajectory-annotation fields are the shared ``MemoryConfig``'s)."""

from pydantic import Field

from shared_bridge.config import MemoryConfig


class CureMemoryConfig(MemoryConfig):
    cure_repo_path: str = ""  # "" -> $CURE_MEMORY_REPO -> source-tree candidate
    db_path: str = ""  # "" -> derived (see backend); explicit override
    # extraction client ("" -> env EXTRACT_MODEL / EXTRACT_BASE_URL / EXTRACT_API_KEY)
    extract_model: str = ""
    extract_base_url: str = ""
    extract_api_key: str = Field(default="", exclude=True, repr=False)
    # extraction payload (defaults follow CURE's recommendation)
    extract_max_tokens: int = Field(default=1600, gt=0)  # -> client max_completion_tokens
    extract_reasoning_effort: str = "low"  # "" -> omit the param from the payload entirely
    extract_timeout: float = Field(default=60.0, gt=0)
    extract_max_retries: int = Field(default=1, ge=0)  # 0 -> one attempt, no retries

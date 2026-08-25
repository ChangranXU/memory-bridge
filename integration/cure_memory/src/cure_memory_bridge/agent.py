"""CURE memory agent (automatic-extraction arm): the shared memory agent shell
bound to the CURE backend."""

from pydantic import Field

from shared_bridge.agent import MemoryAgent, MemoryAgentConfig

from cure_memory_bridge.backend import CureMemoryBackend
from cure_memory_bridge.config import CureMemoryConfig


class CureMemoryAgentConfig(MemoryAgentConfig):
    memory: CureMemoryConfig = Field(default_factory=CureMemoryConfig)


class CureMemoryAgent(MemoryAgent):
    """MemoryAgent with host-side CURE recording/extraction/recall."""

    config_class = CureMemoryAgentConfig
    backend_class = CureMemoryBackend

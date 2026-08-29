"""mem0 memory agent (automatic-extraction arm): the shared memory agent shell
bound to the mem0 backend (the mode selects the deployment it talks to)."""

from pydantic import Field

from shared_bridge.agent import MemoryAgent, MemoryAgentConfig

from mem0_bridge.backend import Mem0Backend
from mem0_bridge.config import Mem0Config


class Mem0AgentConfig(MemoryAgentConfig):
    memory: Mem0Config = Field(default_factory=Mem0Config)


class Mem0Agent(MemoryAgent):
    """MemoryAgent with host-side mem0 recording/extraction/recall."""

    config_class = Mem0AgentConfig
    backend_class = Mem0Backend

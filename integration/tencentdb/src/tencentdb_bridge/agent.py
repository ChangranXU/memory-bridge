"""Agent glue: binds the shared MemoryAgent onto the tencentdb backend."""

from pydantic import Field

from shared_bridge.agent import MemoryAgent, MemoryAgentConfig

from tencentdb_bridge.backend import TencentDBBackend
from tencentdb_bridge.config import TencentDBConfig


class TencentDBAgentConfig(MemoryAgentConfig):
    memory: TencentDBConfig = Field(default_factory=TencentDBConfig)


class TencentDBAgent(MemoryAgent):
    config_class = TencentDBAgentConfig
    backend_class = TencentDBBackend

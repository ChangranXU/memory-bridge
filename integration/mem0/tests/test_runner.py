"""The runner module rebinds the SWE-bench batch runner's agent class."""

from minisweagent.run.benchmarks import swebench as swebench_module

from mem0_bridge.agent import Mem0Agent


def test_runner_rebinds_progress_tracking_agent():
    import mem0_bridge.run.swebench  # noqa: F401  (import performs the rebinding)

    assert swebench_module.ProgressTrackingAgent is Mem0Agent
    assert mem0_bridge.run.swebench.app is swebench_module.app


def test_bind_allows_rebinding_over_another_integration():
    """The upstream-refactor guard accepts any ProgressTrackingAgent subclass,
    so a second integration's bind (another suite ran first, any order) is not
    mistaken for an upstream refactor."""
    from minisweagent.run.benchmarks.utils.common import ProgressTrackingAgent
    from shared_bridge.run import bind_swebench_app

    class OtherAgent(ProgressTrackingAgent):
        pass

    bind_swebench_app(OtherAgent)
    assert swebench_module.ProgressTrackingAgent is OtherAgent
    bind_swebench_app(Mem0Agent)
    assert swebench_module.ProgressTrackingAgent is Mem0Agent

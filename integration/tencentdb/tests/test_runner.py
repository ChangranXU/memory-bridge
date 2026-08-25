"""Runner module rebinding tests."""

from minisweagent.run.benchmarks import swebench as swebench_module

from tencentdb_bridge.agent import TencentDBAgent


def test_runner_rebinds_progress_tracking_agent():
    import tencentdb_bridge.run.swebench  # noqa: F401  (import performs the rebinding)

    assert swebench_module.ProgressTrackingAgent is TencentDBAgent
    assert tencentdb_bridge.run.swebench.app is swebench_module.app


def test_bind_allows_rebinding_over_another_integration():
    """The upstream-refactor guard accepts any ProgressTrackingAgent subclass,
    so a second integration's bind (another suite ran first, any order) is not
    mistaken for an upstream refactor."""
    from minisweagent.run.benchmarks.utils.common import ProgressTrackingAgent
    from shared_bridge.run import bind_swebench_app

    class OtherAgent(TencentDBAgent):
        pass

    app = bind_swebench_app(OtherAgent)
    assert app is swebench_module.app
    assert swebench_module.ProgressTrackingAgent is OtherAgent
    assert issubclass(swebench_module.ProgressTrackingAgent, ProgressTrackingAgent)

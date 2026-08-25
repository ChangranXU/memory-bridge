"""bind_swebench_app guards (the runner factory; minisweagent imported lazily)."""

import pytest


def test_bind_rejects_a_non_agent_class():
    """The bind class itself is validated, not just the current module global:
    installing a non-ProgressTrackingAgent would fail later inside
    process_instance with a far less clear error."""
    from shared_bridge.run import bind_swebench_app

    with pytest.raises(AssertionError):
        bind_swebench_app(object)

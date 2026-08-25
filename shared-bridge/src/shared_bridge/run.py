"""SWE-bench runner factory: bind an integration's agent class into the stock
swebench batch runner.

``process_instance`` resolves ``ProgressTrackingAgent`` from the module globals
at call time, so one rebinding is enough (the subclass check turns upstream
refactors into a loud error while still allowing a re-bind over another
integration's agent). ``minisweagent`` is imported lazily inside
the function — it is provided by the environment that runs the agent, and this
package deliberately does not depend on it.

Not exported from ``shared_bridge.__init__``: the endpoint contract must stay
importable in environments without the benchmark stack.
"""


def bind_swebench_app(agent_class):
    """Rebind the stock swebench runner's agent class and return its ``app``."""
    from minisweagent.run.benchmarks import swebench as _swebench
    from minisweagent.run.benchmarks.utils.common import (
        ProgressTrackingAgent as OriginalProgressTrackingAgent,
    )

    # issubclass (not identity): rebinding over another integration's agent is
    # legitimate — one process binds one integration, but test suites bind
    # several in any order. The checks turn upstream refactors (the name
    # gone, or resolving to something that is not a ProgressTrackingAgent)
    # and an invalid bind class alike into a loud error.
    assert issubclass(_swebench.ProgressTrackingAgent, OriginalProgressTrackingAgent)
    assert issubclass(agent_class, OriginalProgressTrackingAgent)
    _swebench.ProgressTrackingAgent = agent_class
    return _swebench.app

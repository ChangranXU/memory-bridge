"""SWE-bench batch runner with CURE automatic-extraction memory.

CLI identical to minisweagent's swebench runner: ``process_instance`` resolves
``ProgressTrackingAgent`` from module globals at call time, so one rebinding is
enough (the subclass check turns upstream refactors into a loud error).
"""

from shared_bridge.run import bind_swebench_app

from cure_memory_bridge.agent import CureMemoryAgent

app = bind_swebench_app(CureMemoryAgent)

if __name__ == "__main__":
    app()

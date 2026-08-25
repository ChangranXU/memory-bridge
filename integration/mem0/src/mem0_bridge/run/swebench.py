"""SWE-bench batch runner with mem0 automatic-extraction memory.

CLI identical to minisweagent's swebench runner: ``process_instance`` resolves
``ProgressTrackingAgent`` from module globals at call time, so one rebinding is
enough (the subclass check turns upstream refactors into a loud error).
"""

from shared_bridge.run import bind_swebench_app

from mem0_bridge.agent import Mem0Agent

app = bind_swebench_app(Mem0Agent)

if __name__ == "__main__":
    app()

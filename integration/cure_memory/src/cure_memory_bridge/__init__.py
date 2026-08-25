"""CURE automatic-extraction memory bridge for mini-swe-agent SWE-bench runs.

The package root deliberately imports nothing: an eager re-export of the
endpoint module would import ``cure_memory`` at package-init time, caching the
installed tree in ``sys.modules`` before the backend's origin-checked import
runs and defeating the ``CURE_MEMORY_REPO`` / ``cure_repo_path`` override.
Import the submodules directly (``cure_memory_bridge.agent``,
``cure_memory_bridge.backend``, ``cure_memory_bridge.config``,
``cure_memory_bridge.endpoint``).
"""

"""Shared fixtures for the shared-bridge suites (fully offline): the fake
reference integration lives in fake_integration.py (a plain module — never
shadowed by same-named conftests in the sibling integration suites), and the
capture server (shared_bridge.testing) is a real local HTTP stand-in for the
recorder's annotate endpoint.
"""

import pytest

from fake_integration import FakeBackend, _config

from shared_bridge.testing import CaptureServer


@pytest.fixture
def traced_backend(tmp_path, capture_server):
    """FakeBackend traced against the capture server: the main lane derives
    from the model URL; the memory lane carries no model URL, so its explicit
    config endpoint resolves via the no-model-URL lane rule."""
    def _factory(**overrides):
        overrides.setdefault("annotate_memory_url", capture_server.annotate_url("MEMORY"))
        backend = FakeBackend(
            _config(tmp_path / "inst", **overrides),
            model_base_url=capture_server.lane_url("MAIN"),
        )
        backend.start()
        assert backend._available
        assert backend._trace is not None
        return backend

    return _factory


@pytest.fixture
def capture_server():
    server = CaptureServer().start()
    yield server
    server.stop()

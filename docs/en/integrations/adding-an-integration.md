---
description: How to add a new memory system to memory-bridge.
---

# Adding an Integration

Adding a new memory system means creating a self-contained package under `integration/<name>/` that binds to the generic bridge. Nothing in `shared-bridge/` needs to change — the zero-naming scan test enforces this.

## Steps

{% stepper %}
{% step %}
### Create the integration package

Create `integration/<name>/` as a uv workspace member with the following structure:

```text
integration/<name>/
├── pyproject.toml          # workspace member
├── src/<name>_bridge/
│   ├── __init__.py
│   ├── backend.py          # BaseMemoryBackend subclass
│   ├── agent.py            # MemoryAgent subclass
│   ├── config.py           # MemoryConfig subclass (integration fields)
│   ├── endpoint.py         # MemoryEndpoint adapter
│   ├── prompts.py          # the prompt home; for a bundled memory system it may live in the system package instead, as with CURE
│   └── run/
│       └── swebench.py     # the runner module
├── configs/                # Default configuration files
└── tests/                  # Offline test suite
```

Register the package as a workspace member in the root `pyproject.toml`.
{% endstep %}

{% step %}
### Implement the backend

Subclass `BaseMemoryBackend` and implement all abstract hooks:

```python
from shared_bridge.backend import BaseMemoryBackend

class MyMemoryBackend(BaseMemoryBackend):
    def _resolve_settings(self) -> dict:
        """Validate config and environment; raise for expected unavailability."""
        ...

    def _startup(self, settings: dict) -> None:
        """Construct the memory system."""
        ...

    def _store_message(self, role: str, text: str, step: int) -> None:
        """Persist one normalized message."""
        ...

    def _perform_extraction(self, step: int) -> None:
        """Run one extraction cycle."""
        ...

    def _search(self) -> list:
        """Return recall hits for the current query."""
        ...

    # ... implement all remaining abstract hooks
```
{% endstep %}

{% step %}
### Bind the agent

Create a `MemoryAgent` subclass that binds `backend_class` and (usually) `config_class`:

```python
from shared_bridge.agent import MemoryAgent, MemoryAgentConfig
from .backend import MyMemoryBackend

class MyMemoryAgent(MemoryAgent):
    backend_class = MyMemoryBackend
    config_class = MemoryAgentConfig  # or a subclass with extra fields
```

Create the runner module `run/swebench.py` that calls `bind_swebench_app(MyMemoryAgent)`.
{% endstep %}

{% step %}
### Implement the endpoint adapter

```python
from shared_bridge.endpoint import (
    MemoryEndpoint, AddRequest, AddResponse,
    SearchRequest, SearchResponse,
    UpdateRequest, UpdateResponse, DeleteResponse,
)

class MyEndpoint(MemoryEndpoint):
    def add(self, request: AddRequest) -> AddResponse: ...
    def search(self, request: SearchRequest) -> SearchResponse: ...
    def update(self, memory_id, request, *, user_id=None) -> UpdateResponse: ...
    def delete(self, memory_id, *, user_id=None) -> DeleteResponse: ...
```

The endpoint and the backend's `_search()` must share one semantics — one native call, same ranking/filter behavior.
{% endstep %}

{% step %}
### Add offline tests

Write tests in the same style as the existing suites:

* **No model calls, no Docker** — use scripted fakes and mock transports
* **Pin the failure paths**, not just the happy ones
* **Name tests `test_<behavior>`** — every test should exercise a real point of failure
* Tests must be runnable from the bundle root in a single pytest invocation
{% endstep %}

{% step %}
### Verify

Run the full offline suite from the bundle root:

```bash
uv run python -m pytest shared-bridge/tests integration/<name>/tests -q
```

Confirm that the zero-naming scan in `shared-bridge/tests` still passes — it verifies that no shared source names your integration (adding an integration means adding its name to the scan's word-bounded pattern, a one-line test-only change).
{% endstep %}
{% endstepper %}

## Rules

{% hint style="danger" %}
These constraints are non-negotiable:
{% endhint %}

1. Nothing in `shared-bridge/` may name the integration
2. The integration must be a uv workspace member — never its own venv
3. The endpoint and backend `_search()` must share semantics
4. Prompt text lives in the integration's own `prompts.py`, never inline
5. Credentials use pydantic `exclude=True, repr=False`

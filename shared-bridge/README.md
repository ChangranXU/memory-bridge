# shared-bridge

The generic, integration-agnostic core of
[memory-bridge](../README.md). It provides the agent hook shell, the
memory-backend lifecycle skeleton, the standardized memory endpoint
contract, and the trajectory-annotation transport. Nothing in this package
names a specific memory system — a
[dedicated test](tests/test_backend_base.py)
scans the shared sources and fails the build if an integration name leaks
in, so adding a new memory system requires no changes here.

[English](README.md) | [简体中文](README.zh-CN.md)

## Modules

| Module | Provides |
|---|---|
| [`agent.py`](src/shared_bridge/agent.py) | `MemoryAgent` — wires the memory lifecycle into the agent loop.<br>Records every trajectory message, triggers extraction after clean steps, and injects recalled memories as a transient user message before each model query (visible to the model but never persisted).<br>With `agent.memory.enabled=false`, acts as a no-op wrapper, byte-identical to baseline. |
| [`backend.py`](src/shared_bridge/backend.py) | `BaseMemoryBackend` — the lifecycle skeleton: `start → set_task → record → maybe_extract → recall → finalize`.<br>Produces the `memory.json` artifact, maintains counters, and emits schema-v6 memory-protocol traces.<br>Every legitimate integration divergence is surfaced as an explicit hook. |
| [`config.py`](src/shared_bridge/config.py) | `MemoryConfig` — the shared `agent.memory.*` fields (scope, cadence, recall budgets, annotation settings).<br>Integrations subclass it to add their own fields. |
| [`endpoint.py`](src/shared_bridge/endpoint.py) | The standardized `MemoryEndpoint` contract (`add` / `search` / `update` / `delete`) and its pydantic wire models.<br>Importable without the benchmark stack. |
| [`serve.py`](src/shared_bridge/serve.py) | A stdlib-only HTTP front that exposes any `MemoryEndpoint` on `/v1/memories/` routes. |
| [`annotate.py`](src/shared_bridge/annotate.py) | Annotation transport for the [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) proxy.<br>Handles lane-URL resolution and validation, credential-safe URL sanitization, batching, retries, and circuit breaking. |
| [`run.py`](src/shared_bridge/run.py) | `bind_swebench_app()` — one-line rebinding of the stock swebench runner's agent class onto an integration's agent. |
| [`testing.py`](src/shared_bridge/testing.py) | `CaptureServer` — a local HTTP stand-in for the recorder's annotate endpoint.<br>Shared by every offline test suite in the bundle. |

## Design rules

- **Stdlib + pydantic only.** `minisweagent` is imported lazily (in the
  agent shell and run factory) so that the endpoint contract remains
  importable in environments without the benchmark stack.
- **Fail-closed.** Backend errors are contained and logged; nothing raises
  into the agent loop unless `config.strict`. `note_recall` never raises —
  observability must not mask a model exception.
- **Credentials never reach artifacts.** Secret and bearer-token fields are
  pydantic fields with `exclude=True, repr=False`. Logged URLs carry only
  the sanitized form (userinfo/query/fragment stripped, trajectory ID
  reduced to a 16-hex hash prefix).
- **Annotation is pure observability.** Any annotation failure degrades to
  untraced native behavior; annotation I/O time is excluded from the
  agent's wall-time budget.

## Building an integration

1. **Subclass `BaseMemoryBackend`** and implement the abstract hooks
   (store / extract / search / render, plus the tracing adapter trio) —
   see [memory lifecycle](../doc/memory-lifecycle.md).
2. **Bind it to the runner:** subclass `MemoryAgent` with `backend_class`
   (and usually a `MemoryConfig` subclass), then wire it through
   `bind_swebench_app()` — see [architecture](../doc/architecture.md). For
   a non-mini-swe-agent host, replace this step with an equivalent runner
   that hooks your agent's loop into the `MemoryAgent` subclass — the
   backend and endpoint layers require no changes.
3. **Adapt your store to the shared contract** with a `MemoryEndpoint`
   implementation — see [endpoint API](../doc/endpoint-api.md).

The two reference implementations are
[`integration/cure_memory`](../integration/cure_memory/README.md) and
[`integration/mem0`](../integration/mem0/README.md).

## Tests

All tests are offline, driven through a fake reference integration
(`FakeBackend`), so the generic suites never touch a real memory system:

```bash
cd <bundle-root> && uv run python -m pytest shared-bridge/tests -q
```

Coverage includes the annotation transport, the endpoint contract and its
HTTP round-trip, the backend lifecycle (including failure-path artifact
pins), the run factory, and the zero-integration-naming scan. These tests
can also be run together with both integration suites in a single
invocation from the bundle root.

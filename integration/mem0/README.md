# mem0 Integration

**[mem0 Platform](https://github.com/mem0ai/mem0) as a hosted memory system for
coding agents. Currently wired into
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) for SWE-bench
evaluation.**

[English](README.md) | [简体中文](README.zh-CN.md)

Mirrors `integration/cure_memory` in structure but replaces CURE's local
SQLite store and extraction LLM with mem0's hosted API. The memory lane
carries no model traffic — it exists only as the trajectory-proxy annotation
namespace where the backend posts the schema-v6 memory protocol from the
platform's receipts.

## Architecture

```text
python -m mem0_bridge.run.swebench        # runner: rebinds ProgressTrackingAgent
  └─ Mem0Agent(MemoryAgent)               # shared-bridge agent shell
      └─ Mem0Backend                      # lifecycle for one episode
          ├─ record()        buffer trajectory messages (truncated, role-mapped)
          ├─ maybe_extract() POST /v3/memories/add/ (platform-side extraction),
          │                  polled to completion; failures retain batch for retry
          ├─ recall_context() POST /v3/memories/search/; rank-then-fill render;
          │                  injected as a transient user message
          └─ finalize()      final flush + get_all dump + memory.json + close
```

`Mem0Endpoint` (`mem0_bridge.endpoint`) adapts the same client to the shared
`MemoryEndpoint` contract (add/search/update/delete).

## REST Client Design

The mem0 SDK's `MemoryClient` pulls the full open-source stack (embedders,
vector stores, DB drivers) as transitive dependencies, risking conflicts with
`litellm[proxy]` in the shared environment. The bridge needs only a small
REST surface (`Token` auth, v3 add/search, v1 CRUD + event polling), and httpx
is already available. All request/response shapes were verified against the
live API.

## Run Isolation

The mem0 store is persistent across run roots, so isolation comes from the
effective user id:

- `scope=run` (default): one `user_id` for the whole run root — instance 2
  recalls instance 1's memories.
- `scope=instance`: `"{user_id}:{instance_id}"` — a fresh namespace per task.

The driver mints `user_id=minisweagent-mem0-<run-root-basename>` (timestamped),
so a fresh run root never recalls a previous run's memories. Only `user_id`
(never `agent_id`) is passed as the entity id — the platform's attribution
splitting would otherwise miss assistant-message facts in user-filtered
searches.

## Running

```bash
cd memory-bridge
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt mem0first2
./utils/run-memory-arm.sh mem0                       # uses output/LATEST
```

The driver reads the provider roster from `.env` and `MEM0_API_KEY` from
`integration/mem0/.env`, then runs each instance behind a per-instance
[traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory)
roster proxy (MAIN lane = benchmark model, MEMORY lane = annotation
namespace). It chains merge / Docker evaluation / summary, and refuses to
resume an instance with a stale attempt (agent.log without valid preds.json).

Per-instance outputs: `preds.json`, `agent.log`, `memory.json`, `proxy.log`,
`<id>/<id>.traj.json`, and `<id>/<id>/trajectory/`.

## Files

```text
integration/mem0/
├── .env                          # MEM0_API_KEY (never commit)
├── pyproject.toml                # uv workspace member
├── configs/memory_defaults.yaml  # partial agent.memory.* overlay
├── src/mem0_bridge/
│   ├── config.py                 # Mem0Config(MemoryConfig)
│   ├── client.py                 # Mem0PlatformClient (httpx)
│   ├── backend.py                # Mem0Backend
│   ├── agent.py                  # Mem0Agent / Mem0AgentConfig
│   ├── endpoint.py               # Mem0Endpoint(MemoryEndpoint)
│   └── run/swebench.py           # runner: one-line agent rebinding
└── tests/                        # offline suite
```

Installed as an editable workspace member by `uv sync` at the bundle root.

## Config Reference (`agent.memory.*`)

Shared fields are documented in
[Memory lifecycle](../../doc/memory-lifecycle.md). mem0-specific fields:

| Key | Default | Description |
|---|---|---|
| `api_key` | `""` | mem0 API key. `""` falls back to `$MEM0_API_KEY`. |
| `base_url` | `""` | `""` falls back to `$MEM0_BASE_URL`, then `https://api.mem0.ai`. |
| `infer` | `true` | Platform-side extraction. `false` stores messages verbatim. |
| `search_threshold` | `0.0` | Platform relevance cutoff (`0.0` disables; ranking + `max_memories` bound recall). |
| `poll_budget` | `60.0` (120 in overlay) | Total add+poll budget per batch. |
| `poll_interval` | `1.0` | Cadence for polling the async add event. |

## Interpreting memory.json

A healthy run shows `enabled: true`, `available: true`,
`extraction_errors: 0`, and `recall_injections > 0` after the first successful
extraction. With `scope=run`, the second instance recalls from step 0.
`counts` also carries `search_calls`/`search_errors` and
`memories_added`/`updated`/`deleted`. `final_memories` is a diagnostic dump of
the effective user's memories.

## Tests

```bash
cd memory-bridge && uv run python -m pytest integration/mem0/tests -q
```

Fully offline: backend tests use a scripted platform client, client tests run
against `httpx.MockTransport`, and agent tests use the deterministic toolcall
model. Runs green together with the shared-bridge and CURE suites.

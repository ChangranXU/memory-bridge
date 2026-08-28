# mem0 Integration

**[mem0](https://github.com/mem0ai/mem0) as a memory system for coding
agents — hosted Platform, self-hosted OSS server, or in-process library.
Currently wired into
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) for SWE-bench
evaluation.**

[English](README.md) | [简体中文](README.zh-CN.md)

Mirrors `integration/cure_memory` in structure but replaces CURE's local
SQLite store and extraction LLM with mem0, in one of three deployment modes
(the `mode:` line in `configs/memory_defaults.yaml`):

- `platform` (default) — the hosted API; extraction runs platform-side.
- `server` — a per-run self-hosted OSS server stack (pgvector + API server
  containers); extraction runs inside the container against the provider
  upstream.
- `library` — the in-process `mem0ai` engine; extraction runs in this
  process against the provider upstream.

In every mode extraction traffic stays off the trajectory, so the memory
lane carries no model traffic — it exists only as the trajectory-proxy
annotation namespace where the backend posts the schema-v6 memory protocol
from the engine's receipts.

## Architecture

```text
python -m mem0_bridge.run.swebench        # runner: rebinds ProgressTrackingAgent
  └─ Mem0Agent(MemoryAgent)               # shared-bridge agent shell
      └─ Mem0Backend                      # lifecycle for one episode
          ├─ record()        buffer trajectory messages (truncated, role-mapped)
          ├─ maybe_extract() store.add() — platform: async + event poll;
          │                  server/library: synchronous; failures retain the
          │                  batch for retry
          ├─ recall_context() store.search(); rank-then-fill render;
          │                  injected as a transient user message
          └─ finalize()      final flush + get_all dump + memory.json + close
              └─ Mem0Store  (open_store dispatches on mode, lazy per-mode imports)
                  ├─ platform  Mem0PlatformClient (httpx) → hosted v3/v1 API
                  ├─ server    ServerStore (httpx) → per-run OSS container at 127.0.0.1:8890
                  └─ library   LibraryStore → in-process mem0ai engine
```

`Mem0Endpoint` (`mem0_bridge.endpoint`) adapts the same `Mem0Store` to the
shared `MemoryEndpoint` contract (add/search/update/delete): backend and
endpoint consume one store protocol, so retrieval is implemented exactly
twice over the mode's one native call and the two surfaces cannot drift.

## Stores

Platform and server modes talk REST over httpx; library mode runs the engine
in-process:

- **platform** — `Mem0PlatformClient`: `Token` auth, v3 add/search/get-all,
  v1 CRUD + event polling; the async add is polled to completion
  (`poll_budget`/`poll_interval`).
- **server** — `ServerStore` (httpx): unprefixed routes, strict about
  trailing slashes; adds are synchronous (one extraction LLM round-trip
  inside the request — `add_timeout` defaults to 300 s); the driver raises
  `search_timeout` to 30 for server arms (one HTTP call hides the embedder
  round-trips plus the hybrid CPU work).
- **library** — `LibraryStore`: `from mem0 import Memory`, imported only in
  library mode. The `mem0ai` SDK enters the shared env ONLY through the
  opt-in root dependency group `mem0-library`
  (`uv run --group mem0-library`; a plain `uv sync` evicts it) — the default
  env stays mem0ai-free because the SDK pulls the full open-source stack
  (embedders, vector stores, DB drivers) as transitive dependencies, risking
  conflicts with `litellm[proxy]`.

All request/response shapes were verified against the live platform API and
the vendored OSS tree (pin in `VENDORING.md`).

## Run Isolation

Run isolation comes from the effective user id in every mode; the
server/library modes add fresh per-run stores on top (the platform store is
hosted and persistent across run roots):

- `scope=run` (default): one `user_id` for the whole run root — instance 2
  recalls instance 1's memories.
- `scope=instance`: `"{user_id}:{instance_id}"` — a fresh namespace per task.

The driver mints `user_id=minisweagent-mem0-<run-root-basename>` (timestamped),
so a fresh run root never recalls a previous run's memories. Only `user_id`
(never `agent_id`) is passed as the entity id — the engine's attribution
splitting would otherwise miss assistant-message facts in user-filtered
searches.

## Running

```bash
cd memory-bridge
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt mem0first2
./utils/run-memory-arm.sh mem0                       # uses output/LATEST
```

The mode comes from the anchored `mode:` line in
`configs/memory_defaults.yaml` (yaml-owned — the driver reads the same line
and refuses `--config agent.memory.mode=` extras; see
[AGENTS.md](AGENTS.md)). Per-mode prerequisites:

- `platform`: `MEM0_API_KEY` in the bundle-root `.env`.
- `server`: Docker installed and running, plus the full `EMBEDDING_MODEL` /
  `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` / `EMBEDDING_DIMENSIONS` quartet
  in the bundle-root `.env` (fail-closed — the OSS engine embeds on every
  add/search, no lexical fallback). The driver builds and manages the
  two-container stack (published at `127.0.0.1:8890`, machine-wide
  single-arm claim) and removes it on exit; the store lives under
  `<run-root>/mem0-server/`.
- `library`: the same `EMBEDDING_*` quartet (fail-closed). Each instance
  invocation carries `uv run --group mem0-library` itself; an optional
  `uv sync --group mem0-library` pre-warm just skips the per-instance
  resolve. The store lives under `<run-root>/mem0/`.

The driver reads the provider roster from the bundle-root `.env`, then runs
each instance behind a per-instance
[traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory)
roster proxy (MAIN lane = benchmark model, MEMORY lane = annotation
namespace). It chains merge / Docker evaluation / summary, and refuses to
resume an instance with a stale attempt (agent.log without valid preds.json).

Per-instance outputs: `preds.json`, `agent.log`, `memory.json`, `proxy.log`,
`<id>/<id>.traj.json`, and `<id>/<id>/trajectory/`.

## Files

```text
integration/mem0/
├── pyproject.toml                # uv workspace member
├── VENDORING.md                  # the vendored clone: acquisition, pins, boundary
├── configs/memory_defaults.yaml  # partial agent.memory.* overlay (carries the mode: line)
├── src/mem0_bridge/
│   ├── config.py                 # Mem0Config(MemoryConfig)
│   ├── client.py                 # Mem0PlatformClient (httpx; platform mode)
│   ├── stores/
│   │   ├── __init__.py           # Mem0Store protocol + open_store factory
│   │   ├── platform.py           # hosted API store (wraps Mem0PlatformClient)
│   │   ├── server.py             # per-run OSS server store (httpx)
│   │   └── library.py            # in-process mem0ai store (opt-in group)
│   ├── backend.py                # Mem0Backend
│   ├── agent.py                  # Mem0Agent / Mem0AgentConfig
│   ├── endpoint.py               # Mem0Endpoint(MemoryEndpoint)
│   └── run/swebench.py           # runner: one-line agent rebinding
├── vendor/mem0/                  # gitignored vendored OSS clone — never committed, never imported
└── tests/                        # offline suite
```

Installed as an editable workspace member by `uv sync` at the bundle root.

## Config Reference (`agent.memory.*`)

Shared fields are documented in
[Memory lifecycle](../../doc/memory-lifecycle.md). mem0-specific fields:

| Key | Default | Description |
|---|---|---|
| `mode` | `"platform"` | Deployment selector: `platform` \| `server` \| `library`. Yaml-owned — set it in `configs/memory_defaults.yaml`, never via `--config` extras. |
| `api_key` | `""` | Platform mode: mem0 API key. `""` falls back to `$MEM0_API_KEY`. |
| `base_url` | `""` | Platform mode: `""` falls back to `$MEM0_BASE_URL`, then `https://api.mem0.ai`. |
| `server_url` | `""` | Server mode: `""` falls back to `$MEM0_SERVER_URL` (the driver mints it per run). |
| `server_api_key` | `""` | Server mode: optional — the arm runs the container with `AUTH_DISABLED=true`, and an empty key sends no auth header. |
| `run_root` | `""` | Library mode: store dir anchor (`<run_root>/mem0/`); the driver passes `$RUN_ROOT`. |
| `infer` | `true` | Engine-side extraction. `false` stores messages verbatim. |
| `search_threshold` | `0.0` | Sent explicitly on every surface; semantics per surface — platform `0.0` disables the cutoff, OSS (server/library) `0.0` is a minimal gate on the raw score before the hybrid combine. Ranking + `max_memories` bound recall. |
| `poll_budget` | `60.0` (120 in overlay) | Platform mode only: total add+poll budget per batch (OSS adds are synchronous). |
| `poll_interval` | `1.0` | Platform mode only: cadence for polling the async add event. |

## Interpreting memory.json

A healthy run shows `enabled: true`, `available: true`,
`extraction_errors: 0`, and `recall_injections > 0` after the first successful
extraction. With `scope=run`, the second instance recalls from step 0.
Settings record `mode` and `bridge_version`; the start event carries `mode`.
`counts` also carries `search_calls`/`search_errors` and
`memories_added`/`updated`/`deleted`. `final_memories` is a diagnostic dump of
the effective user's memories.

## Tests

```bash
cd memory-bridge && uv run python -m pytest integration/mem0/tests -q
```

Fully offline: backend tests use scripted stores, client tests run against
`httpx.MockTransport`, library-mode tests ride a fake `Memory` seam (the
suite never imports `mem0ai`), and agent tests use the deterministic toolcall
model. Runs green together with the shared-bridge and CURE suites.

---
description: Running the memory arm with a selected integration.
---

# Memory Arm

The memory arm runs predictions with memory ON (`scope=run`) through a selected integration, then chains merge → evaluation → summary.

## Quick run

```bash
# Set up a run root with a small slice
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt first2

# Run the memory arm (pick one)
./utils/run-memory-arm.sh cure_memory
# or:
./utils/run-memory-arm.sh mem0
# or:
./utils/run-memory-arm.sh tencentdb
```

## How it works

The driver runs each instance behind a per-instance [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) roster proxy, then chains merge, evaluation, and summary.

{% tabs %}
{% tab title="cure_memory" %}
**Proxy lanes:** MAIN (benchmark model) + EXTRACT (extraction LLM) + QUERY (rewriter)

Per instance, the driver:
1. Starts a dedicated roster proxy
2. Runs mini-swe-agent with the CURE integration
3. Closes the proxy (SIGINT)

**Artifacts:**
* `runs/mini-swe-agent/cure_memory.sqlite3` — shared run-scope SQLite store
* Per-instance proxy artifacts: `proxy.log`, trajectory recording
{% endtab %}

{% tab title="mem0" %}
**Proxy lanes:** MAIN (benchmark model) + MEMORY (zero model calls, annotation namespace) + QUERY (rewriter)

Run isolation comes from a per-run user ID minted from the timestamped run-root name (the server/library modes add fresh per-run stores on top).

The deployment mode comes from the anchored `mode:` line in `integration/mem0/configs/memory_defaults.yaml` (yaml-owned — `--config agent.memory.mode=` extras are refused):

* `platform` (default) — the hosted API; extraction runs platform-side. Requires `MEM0_API_KEY` in the bundle-root `.env`.
* `server` — the driver manages a two-container OSS stack per run root (pgvector + the API server built from the vendored clone, engine pinned `mem0ai==2.0.19`) at `127.0.0.1:8890`, under a machine-wide single-arm claim; store volumes under `<run-root>/mem0-server/`. Requires Docker running plus the full `EMBEDDING_*` quartet (fail-closed).
* `library` — the `mem0ai` engine in-process via the opt-in `mem0-library` dependency group (every instance invocation carries `uv run --group mem0-library`); store under `<run-root>/mem0/`. Requires the `EMBEDDING_*` quartet.
{% endtab %}

{% tab title="tencentdb" %}
**Proxy lanes:** MAIN (benchmark model) + MEMORY (zero model calls, annotation namespace) + QUERY (rewriter)

The driver manages one MemoryCore container per run root: it writes a credential-free gateway yaml (`<run-root>/tdai/tdai-gateway.yaml`, secrets interpolated from `docker run -e` env), starts `agentmemory/memory-core:1.0.1-beta.1` on `127.0.0.1:8420` with the data volume at `<run-root>/tdai/data`, waits for `/health`, and removes the container on exit. Extraction runs inside the container against the provider upstream directly (not recorded in the trajectory).

**Requirements:**
* Docker installed and running
* Optional all-or-none embedding quartet in the roster `.env`: `EMBEDDING_MODEL`, `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL`, `EMBEDDING_DIMENSIONS` (the driver refuses a partial set, which upstream would silently disable)

**Artifacts:** additionally `<run-root>/tdai/` — the gateway config, the container data volume, and `episodes.jsonl` (the cross-episode origin-attribution sidecar).
{% endtab %}
{% endtabs %}

## Resume behavior

The driver resumes by skipping instances that already have a valid patch (a `preds.json` with a non-empty `model_patch`).

{% hint style="danger" %}
Resume is **refused** when the shared store exists and a listed instance has a stale attempt (`agent.log`) without a valid patch. Re-running it would recall memories approved during the aborted attempt, contaminating the arm. Start a fresh run root instead.
{% endhint %}

## Core artifacts

Every integration produces the same set of per-instance artifacts:

| Artifact | Description |
|---|---|
| `preds.json` | The instance patch (model extract) |
| `memory.json` | Episode log: settings, counters, events, final memories |
| `<id>/<id>.traj.json` | Trajectory with `info.memory` stats |
| `<id>/trajectory/` | traj-recorder recording with `memory_*` annotation events |
| `agent.log` | Full agent transcript |

Plus run-level artifacts: `runs/mini-swe-agent/merged-preds.json`, `local-eval/`, `memory-arm.log`.

## Memory analysis

```bash
# Aggregate memory behavior across all episodes
./utils/summarize-memory.sh [RUN_ROOT]
```

This produces per-episode tables showing:
* Store deltas (added/updated/deleted; the tencentdb arm prints "-" for deleted — dedup deletes are unobservable)
* Agent-initiated scene reads (the tencentdb arm's L2 read observation)
* Agent-initiated L0 conversation searches (the tencentdb arm's L0 read observation: the `agent_conversation_searches` count and `conversation_search_chars` observation chars, printed as the `agent l0 searches` / `l0 search chars` columns)
* Injection counts and character budgets
* Search-cache hit share
* Rewrite outcomes
* Cross-episode recall share from per-hit origin lists

## Environment variables

The driver regenerates the recorder's `.env` from the provider `.env` each run. All three integrations declare `ROLE3="QUERY"` for the recall-query rewriter lane.

| Variable | CURE arm | mem0 arm | tencentdb arm |
|---|---|---|---|
| `API_KEY` / `BASE_URL` / `MODEL` | Required | Required | Required |
| `EXTRACT_*` | Not used (driver-managed per instance from the EXTRACT proxy lane) | Not used | Not used (container points at the upstream directly) |
| `MEM0_API_KEY` | Not used | Required (platform mode only) | Not used |
| `EMBEDDING_*` (all four or none) | Not used | Required in server/library modes (fail-closed); unused in platform mode | Optional (vector lane; partial sets refused) |
| `QUERY_*` | Optional (defaults to role-1) | Optional (defaults to role-1) | Optional (defaults to role-1) |

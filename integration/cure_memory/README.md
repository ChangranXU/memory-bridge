# CURE Integration

**Automatic-extraction memory for coding agents, powered by the
[CURE memory system](https://github.com/staymylove/CURE_memory_system).
Currently wired into
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) for SWE-bench
evaluation.**

[English](README.md) | [简体中文](README.zh-CN.md)

The full `CUREMemorySystem` lifecycle runs host-side alongside the agent loop.
CURE's **extraction LLM** — not the benchmark model — is the sole memory
decision-maker. The benchmark model sees only the stock `bash` tool; memory
enters each episode as a transient recall injection and leaves it as recorded
messages that the extraction LLM later processes.

## Episode Lifecycle

1. Open the CURE store and start a session with a unique episode id.
2. Record every trajectory message (normalized, hard-capped).
3. Every N steps (default 10) and at episode end, send new messages to the
   extraction LLM for candidate / deletion / rejection decisions. A circuit
   breaker caps the cost of a dead endpoint; the final flush always runs.
4. Before every model query, inject approved memories as a **transient** user
   message (visible to the model, never persisted).
5. `finalize()` writes `memory.json` and closes SQLite. Everything is
   fail-closed; `strict: true` turns containment into raises.

## Applicability Layers (`scope="run"`)

Every memory's layer is fixed once, at extraction, by the decision LLM's
`scope` field:

- **repo-bound** (`scope="project"`) — the fact applies to one repository
  only: file paths, module layout, repo-specific commands, this codebase's API
  or dependency constraints. Stored with `project_id = <owner>__<repo>`
  (derived from the instance id) and retrievable only inside episodes of that
  repository.
- **general** (`scope="user"`) — a lesson that survives a repo switch:
  debugging methods, tool usage, generic workflow patterns. Stored with
  `project_id = NULL`, visible to every episode of the run.

An episode recalls its own repo's rows plus every general row, never another
repo's rows. The extractor fails closed (a missing or malformed scope lands
repo-bound), supersede never crosses layers in either direction (a general
candidate never supersedes a repo-bound row of the same type+key, and a
repo-bound candidate never supersedes a shared general row — the general
layer is run-wide, so the two coexist and the repo-bound row overlays the
general one in that repo's recall), a deletion stays in the session's own
layer unless it names one (`scope: "user"` reaches the shared general rows;
terminal rows are never re-matched, so one logical deletion counts once),
and recall lines name the layer
(`- [workflow:repo] ...` / `- [fact:general] ...`). `scope="instance"` keeps
per-instance isolation, unchanged.

## Architecture

```text
python -m cure_memory_bridge.run.swebench    (thin runner, CLI identical to stock swebench)
        | patches ProgressTrackingAgent -> CureMemoryAgent
        v
CureMemoryAgent(MemoryAgent)                 # shared_bridge's generic hook shell
        | owns (one per episode)
        v
CureMemoryBackend (host-side, fail-closed)
        | CUREMemorySystem (SQLite) + ChatGPTMemoryDecisionClient (EXTRACT_*)
        v
memory.json + cure_memory.sqlite3
```

The **agent** hooks lifecycle points, the **backend** owns the CURE lifecycle,
the **model** is stock. Generic components (agent shell, config base,
annotation transport, endpoint contract) live in
[`shared-bridge/`](../../shared-bridge/README.md); this package binds them to
CURE. `CureMemoryEndpoint` adapts `CUREMemorySystem` to the shared
`add`/`search`/`update`/`delete` contract.

## On / Off Control

| State | How to invoke | Behavior |
|---|---|---|
| **OFF** (baseline) | `utils/run-predictions.sh` | Stock runner, no bridge code imported. |
| **ON** (memory arm) | `python -m cure_memory_bridge.run.swebench` + config overlay + `enabled=true` | Same model/tools/templates, plus host-side memory and transient injection. |
| **Debug** (`enabled=false`) | Bridge runner + overlay, but `enabled=false` | No backend; trajectory byte-identical to baseline except `info.config` metadata. |

A/B comparison = OFF vs. ON. No `model.model_class` overlay — the stock model
class runs in both arms.

## Running

Use the turnkey driver:

```bash
cd memory-bridge
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt cure2
./utils/run-memory-arm.sh cure_memory          # uses output/LATEST
```

The driver reads the provider roster from `.env` (MODEL / API_KEY / BASE_URL /
API) plus the extraction LLM settings (EXTRACT_MODEL / EXTRACT_BASE_URL /
EXTRACT_API_KEY), starts a per-instance
[traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory)
roster proxy (MAIN lane = benchmark model, EXTRACT lane = extraction LLM),
runs the bridge, then chains merge / Docker evaluation / summary.

For manual invocation details, see [AGENTS.md](AGENTS.md).

Rules: one anchored instance per invocation, `--workers 1`, fresh run root per
arm. Keep `EXTRACT_API_KEY` in the environment, never on the command line.

## Config Reference (`agent.memory.*`)

Shared fields (`enabled`, `scope`, `user_id`, `output_dir`, `strict`,
`max_message_chars`, `inject_recall`, `max_memories`, `max_total_recall_chars`,
`max_chars_per_memory`, `annotate`, `annotate_*`) are documented in
[Memory lifecycle](../../doc/memory-lifecycle.md). CURE-specific fields:

| Key | Default | Description |
|---|---|---|
| `db_path` | `""` | Explicit DB path override (`""` = derived per scope). |
| `cure_repo_path` | `""` | Explicit CURE checkout path. `""` falls back to `$CURE_MEMORY_REPO`, then the integration's own `src/` tree. An explicit path that does not own the imported `cure_memory` is refused. |
| `extract_model` | `""` | Extraction LLM model name. `""` falls back to `$EXTRACT_MODEL`. |
| `extract_base_url` | `""` | Extraction LLM endpoint. `""` falls back to `$EXTRACT_BASE_URL`. |
| `extract_api_key` | `""` | Extraction LLM API key (excluded from all dumps/repr). `""` falls back to `$EXTRACT_API_KEY`. All three must be set or the backend refuses to start. |
| `extract_every_n_steps` | `10` | Extraction cadence. `0` = final flush only. |
| `extract_max_tokens` | `1600` | Maps to the client `max_completion_tokens`. |
| `extract_reasoning_effort` | `low` | `""` omits the parameter entirely (for endpoints that reject it). |
| `extract_timeout` | `60.0` | HTTP timeout per extraction attempt. |
| `extract_max_retries` | `1` | Client-level retries (`0` = single attempt). |
| `extract_max_consecutive_errors` | `3` | Circuit breaker threshold. `0` = never break. |

Unknown keys fail validation (`extra="forbid"`).

## Artifacts

Per instance, in the `--output` directory:

- **`memory.json`** — episode log: settings (sanitized), counters
  (`messages_recorded`, `extraction_calls`/`errors`, decision counts,
  `recall_injections`, `backend_errors`), events, and final memory statuses.
- **`cure_memory.sqlite3`** — CURE's SQLite store (instance-scoped or one
  level up for run scope).
- **`<id>/<id>.traj.json`** — stock trajectory plus `info.memory` stats.

## Trajectory Annotation

With `annotate=true` (default) and both lanes on the roster proxy, the backend
annotates `trajectory.jsonl` with schema-v6 `memory_*` events: session/role
binds, generation operations with change audits, search operations, and
delivery proofs. Full protocol details:
[Memory tracing protocol](../../doc/tracing.md).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `available: false` in memory.json | Read the `error` event at `op: "start"`. Either extraction settings are incomplete (all three `EXTRACT_*` required) or `cure_memory` is not importable (set `CURE_MEMORY_REPO`). |
| Extraction errors in events | `llm_decision_failed:http_*` — the endpoint rejected the call. Checkpoint held, next tick retries. After `extract_max_consecutive_errors` failures the breaker trips. If `reasoning_effort` is rejected, set it to `""`. |
| Nothing extracted | An extraction-quality result, not a harness failure. CURE's sensitive-information guard rejects messages containing tokens/passwords/secrets before the LLM sees them. Check `rejected_by_reason.sensitive_information`. |
| Raw-session sensitivity | `record_message()` commits content before the sensitive guard runs. Treat `cure_memory.sqlite3` as sensitively as the trajectory. |
| Run-scope contamination | Rerunning an aborted instance into the same shared DB can recall stale state. Always use a fresh run root per arm. |

## Tests

Fully offline (scripted fake decision client), run from the bundle root:

```bash
cd memory-bridge && uv run python -m pytest integration/cure_memory/tests -q
```

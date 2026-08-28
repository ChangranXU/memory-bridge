# memory-bridge

**A pluggable memory layer for coding agents** — attach a memory system to your
agent's loop with fail-closed semantics: when memory is off or failing, the
agent runs identically to stock.

[English](README.md) | [简体中文](README.zh-CN.md)

memory-bridge manages the full memory lifecycle — recording agent messages,
extracting memories, and injecting relevant context back into the agent's
prompt — as a host-side layer the model never sees. With memory enabled,
recalled memories enter each model call as a transient user message (visible to
the model but never persisted); with memory disabled, the agent is
byte-identical to stock. Every memory decision is recorded as first-class
annotated events, enabling downstream analysis to reconstruct exactly what
memory did.

Currently integrated with
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) and evaluated on
[SWE-bench Verified](https://www.swebench.com/). The backend lifecycle and
endpoint contract are agent-agnostic by design.

## Design

- **Fail-closed** — a failing memory system degrades to untraced stock
  behavior; nothing raises into the agent loop unless `strict: true`.
- **Generic bridge** — [`shared-bridge/`](shared-bridge/) owns the full
  lifecycle (record, extract, recall, finalize) and never names a specific
  integration (mechanically enforced by a test). Adding a memory system means
  adding one package under `integration/`.
- **Standardized endpoint** — a unified `add` / `search` / `update` /
  `delete` HTTP contract with synchronous writes and `user_id` isolation.
  See the [endpoint API](docs/en/api-reference/overview.md).
- **Portable core** — the backend lifecycle, endpoint contract, and all
  integrations are agent-agnostic. The agent hook layer (`MemoryAgent`) is a
  thin adapter (~200 LOC); backends and integrations stay untouched when
  adapting to another agent.

## Bundled integrations

| Integration | Storage | Extraction | Details |
|---|---|---|---|
| [CURE](integration/cure_memory/) | Local SQLite | Dedicated LLM (EXTRACT lane) | Two-layer repo/general scoping |
| [mem0](integration/mem0/) | Three modes: hosted ([mem0.ai](https://mem0.ai)) platform, per-run OSS server containers, in-process library | Engine-side (hosted / in-container / in-process) | Mode selected in `configs/memory_defaults.yaml` |
| [TencentDB-Agent-Memory](integration/tencentdb/) | Per-run MemoryCore container | Server-side pipeline | Three injected recall layers (L1/L2/L3) + on-demand L0 search |

## Key features

- **Query rewriter** — an optional side-model rewrites the recall query into a
  focused search query at a configurable cadence, improving recall relevance.
- **Dirty-flag search cache** — recall searches run only when the store has
  changed or the query has been rewritten, eliminating redundant calls.
- **Relevance floor** — `recall_min_score` drops low-scoring hits before any
  quantity bound.
- **First-class tracing** — the bridge annotates the
  [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory)
  proxy with schema-v6 `memory_*` events recording every extraction, recall,
  and delivery.
- **Built-in A/B evaluation** — run baseline and memory arms over the same
  SWE-bench instances, graded by the same Docker harness, so the score
  difference is attributable to the memory system alone.

## Quickstart

### Prerequisites

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)
- Docker installed and running (`docker info` succeeds)
- A provider `.env` at the repo root (see below)

### Companion checkouts

Three companion repositories must be cloned before running the
pipeline. None of them are part of this repository:

- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) — the
  prediction runner; an editable dependency of the shared environment
  (`swebench.yaml` lives in this checkout).
- [SWE-bench](https://github.com/SWE-bench/SWE-bench) — required for local
  evaluation.
- [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory)
  (memory branch) — required for the memory arm.

```bash
# Clone these before running uv sync
git clone https://github.com/SWE-agent/mini-swe-agent mini-swe-agent
git clone https://github.com/SWE-bench/SWE-bench SWE-bench
git clone -b memory https://github.com/ChangranXU/traj-recorder.git extension/traj-recorder
```

SWE-bench and traj-recorder may alternatively live as sibling directories of
the repo; mini-swe-agent must sit at `mini-swe-agent/` inside it.

### Setup and run

```dotenv
# .env (never commit)
MODEL=deepseek-v4-flash
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
API=openai-chat
```

```bash
uv sync                                                              # the one shared environment
uv run python -m pytest shared-bridge/tests integration/cure_memory/tests \
  integration/mem0/tests integration/tencentdb/tests -q              # offline test suites (no model calls)

# Baseline arm (stock mini-swe-agent, no memory)
./utils/setup-run.sh
./utils/run-predictions.sh && ./utils/merge-predictions.sh && \
  ./utils/run-evaluation.sh && ./utils/summarize-report.sh

# Memory arm (pick an integration)
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt first2
./utils/run-memory-arm.sh cure_memory        # or: mem0 (platform mode: MEM0_API_KEY in the root .env;
                                             #      server/library modes: EMBEDDING_* quartet required)
                                             # or: tencentdb (Docker; optional EMBEDDING_* quartet in .env)
```

## Reading the results

`summarize-report.sh` prints resolved / unresolved / error verdicts. A healthy
run has submitted == completed == number of ids.

For the memory arm, additionally check each instance's `memory.json`:
`enabled: true`, `available: true`, `counts.extraction_errors: 0`, and
`counts.recall_injections > 0` after the first approved memory. With
`scope=run`, the second instance recalls from its first model call onward —
general memories run-wide, repo-bound memories only within their repository (see
[the CURE integration](integration/cure_memory/README.md#applicability-layers-scoperun)).
For a deeper walkthrough of run artifacts and pipeline phases, see
[Architecture](docs/en/concepts/architecture.md).

## Repository layout

```text
# The memory layer
shared-bridge/            generic bridge: agent hooks, backend lifecycle, endpoint contract, tracing transport,
                            query rewriter, search cache, prompt homes
integration/cure_memory/  CURE integration (local SQLite + extraction LLM)
integration/mem0/         mem0 integration (three modes: hosted platform, OSS server, in-process library)
integration/tencentdb/    TencentDB-Agent-Memory integration (MemoryCore container)

# SWE-bench evaluation harness
utils/                    pipeline scripts: setup → predict → merge → evaluate → summarize
instance-ids.txt          ordered instance list (default pipeline input)
mini-swe-agent/           prediction runner (cloned companion, see Quickstart)
output/                   run roots (created by setup-run.sh)

# Documentation
docs/                     GitBook-ready documentation site (en/ + zh/ variants)
```

## Documentation

For a deeper understanding of the system, start with the architecture overview
and work through the guides below:

- [Architecture](docs/en/concepts/architecture.md) — layered design, the SWE-bench
  evaluation arms, pipeline phases, run artifacts, the shared environment,
  and the query rewriter.
- [Memory lifecycle](docs/en/concepts/memory-lifecycle.md) — agent hooks, the backend
  skeleton and its hook surface, search cache, query rewrite, and failure
  discipline.
- [Memory endpoint API](docs/en/api-reference/overview.md) — the standardized contract and
  its HTTP front.
- [Memory tracing protocol](docs/en/concepts/tracing-protocol.md) — schema-v6 annotation events.
- [Roadmap](docs/en/roadmap.md) — future development.
- Per-package guides: [shared-bridge](shared-bridge/README.md),
  [cure_memory](integration/cure_memory/README.md),
  [mem0](integration/mem0/README.md),
  [tencentdb](integration/tencentdb/README.md),
  [utils](utils/README.md).

## Related repositories

- [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) —
  recording proxy with roster lanes and the annotate endpoint.

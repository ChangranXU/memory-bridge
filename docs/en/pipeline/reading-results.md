---
description: Interpreting evaluation reports and memory artifacts.
---

# Reading Results

## Evaluation report

```bash
./utils/summarize-report.sh [RUN_ROOT]
```

The report prints three verdict categories:

| Verdict | Meaning |
|---|---|
| **Resolved** | Patch applied, tests passed — the model solved the instance |
| **Unresolved** | Patch applied, tests failed — a model miss |
| **Error** | Evaluation failure — read per-instance logs before judging the model |

{% hint style="success" %}
A healthy run has `submitted == completed == number of ids`. If errors appear, check the per-instance logs the summary points to before drawing conclusions about model quality.
{% endhint %}

## Memory artifacts

### `memory.json`

Each memory-arm instance produces a `memory.json` alongside the trajectory. Key fields to check:

| Field | Healthy value | Problem indicator |
|---|---|---|
| `enabled` | `true` | `false` means memory was off |
| `available` | `true` | `false` means the backend failed to start |
| `counts.extraction_errors` | `0` | Non-zero means some extractions failed |
| `counts.recall_injections` | `> 0` (after first approved memory) | `0` means no memories reached the model |
| `counts.recall_cache_hits` | Any | High value means the cache is working well |
| `counts.search_errors` | `0` | Non-zero means search calls failed |

### Memory summary

```bash
./utils/summarize-memory.sh [RUN_ROOT]
```

Aggregates every `memory.json` under a run root into per-episode tables:

* **Store deltas** — memories added / updated / deleted per episode
* **Agent-initiated reads** — the tencentdb arm's L2 scene reads and L0 conversation searches, each an observed, real agent step
* **Injection stats** — count and character budget of injected memories
* **Cache-hit share** — fraction of recall steps served from the search cache
* **Rewrite outcomes** — success/failure counts for the query rewriter
* **Cross-episode recall share** — from the per-hit origin lists, how much recall comes from other episodes vs. the current one

### Trajectory validation (any memory arm)

```bash
uv run python utils/validate_run.py <run-dir>
```

Validates one recorded proxy run directory (the `<ts>-memory-<hash>/` dir holding `trajectory.jsonl` + `run.json`, one level under `<id>/<id>/trajectory/`):
* Event order correctness
* Proxy-source tags present
* Memory-index extractability
* `run.json` counter consistency

## A/B comparison

To attribute a score difference to the memory system:

1. Run both arms over the **same ordered instance list**
2. Use **separate run roots** (fresh root per arm)
3. Grade with the **same Docker harness** (`run-evaluation.sh`)
4. Compare `summarize-report.sh` outputs side by side

The score difference is attributable to the memory system alone because every other variable (model, tools, templates, evaluation harness) is held constant.

---
description: The SWE-bench evaluation pipeline and its phases.
---

# Pipeline

memory-bridge includes a complete evaluation pipeline for running A/B comparisons on SWE-bench Verified. All scripts live in `utils/`. The memory arm and the merge/summary phases run through the bundle's shared uv environment; baseline predictions run in mini-swe-agent's own env (with an ephemeral `litellm[proxy]` overlay), and evaluation runs through the SWE-bench checkout.

## Two evaluation flows

{% columns %}
{% column width="50%" %}
### Baseline arm

Stock mini-swe-agent, no memory — establishes the control score.

```text
setup-run.sh
  → run-predictions.sh
  → merge-predictions.sh
  → run-evaluation.sh
  → summarize-report.sh
```
{% endcolumn %}

{% column width="50%" %}
### Memory arm

One integration, memory ON with `scope=run` — measures the memory system's impact.

```text
setup-run.sh
  → run-memory-arm.sh <integration>
    (chains predict → merge → eval → summarize)
```
{% endcolumn %}
{% endcolumns %}

## Pipeline scripts

| Script | Description |
|---|---|
| `setup-run.sh` | Create a timestamped run root `output/<name>-<ts>/` and record it in `output/LATEST`. |
| `run-predictions.sh` | Generate baseline predictions one instance at a time. |
| `run-memory-arm.sh` | Full memory arm for one integration (predictions + merge + eval + summary). |
| `merge-predictions.sh` | Validate and merge per-instance `preds.json` into `merged-preds.json`. |
| `run-evaluation.sh` | Grade merged predictions with the local SWE-bench Docker harness. |
| `summarize-report.sh` | Print resolved / unresolved / error verdicts. |
| `summarize-memory.sh` | Aggregate `memory.json` files into per-episode tables. |
| `validate_run.py` | Offline validation of one trajectory directory (any memory arm). |
| `common.sh` | Shared helpers: run-root resolution, `.env` loading, ids-file reading. |

{% hint style="info" %}
The phase scripts `run-predictions.sh`, `merge-predictions.sh`, `run-evaluation.sh`, `summarize-report.sh`, and `summarize-memory.sh` accept the run root as an optional first argument. Default resolution: `$RUN_ROOT` → the latest root recorded in `output/LATEST`. `run-memory-arm.sh` takes the integration first and the run root second: `run-memory-arm.sh <integration> [RUN_ROOT]`.
{% endhint %}

## Run root structure

| Path | Written by | Contents |
|---|---|---|
| `runs/mini-swe-agent/<id>/preds.json` | Predictions | The instance patch |
| `runs/mini-swe-agent/<id>/agent.log` | Memory arm | Full agent transcript (driver redirect) |
| `runs/mini-swe-agent/<id>/minisweagent.log` | Predictions | Stock mini-swe-agent log |
| `runs/mini-swe-agent/<id>/memory.json` | Memory arm | Episode log with settings, counters, and events |
| `runs/mini-swe-agent/<id>/<id>/<id>.traj.json` | Predictions | Stock trajectory; `info.memory` stats on the memory arm only |
| `runs/mini-swe-agent/<id>/<id>/trajectory/` | Memory arm | traj-recorder recording with `memory_*` events |
| `runs/mini-swe-agent/cure_memory.sqlite3` | CURE arm | Run-scope shared memory store |
| `runs/mini-swe-agent/merged-preds.json` | Merge | All patches, validated |
| `local-eval/` | Evaluation | SWE-bench harness report |
| `memory-arm.log` | Memory arm | Driver log |

## Critical rules

{% hint style="danger" %}
Violating these rules produces invalid or contaminated results.
{% endhint %}

1. **Fresh run root per arm** — with `scope=run`, the memory store is shared across instances; a dirty root contaminates the arm.
2. **Run exactly the listed instances, one at a time** — anchored `^id$` filter, `--workers 1`.
3. **Don't evaluate until merge passes** — a missing/empty patch makes the report denominator misleading.
4. **Local evaluation only** — always use the SWE-bench Docker harness (`run-evaluation.sh`), never `sb-cli`.
5. **Never reuse a `.proxy_env_role*` from a dead proxy** — SIGINT (not SIGKILL) the proxy so it finalizes run dirs.

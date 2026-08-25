# utils/ — Pipeline Scripts

[English](README.md) | [简体中文](README.zh-CN.md)

Every pipeline entry point lives here. Each script resolves the bundle root
from its own location, sources `common.sh`, and runs through the bundle's
shared uv environment. Two flows are supported:

- **Baseline arm** (stock mini-swe-agent, no memory):
  `setup-run.sh` → `run-predictions.sh` → `merge-predictions.sh` →
  `run-evaluation.sh` → `summarize-report.sh`.
- **Memory arm** (one integration, memory ON with `scope=run`):
  `setup-run.sh` → `run-memory-arm.sh`, which chains predictions, merge,
  evaluation, and summary.

## Scripts

| Script | Description |
|---|---|
| `setup-run.sh` | Create a timestamped run root `output/<name>-<ts>/` (containing `runs/`, `local-eval/`, and a copy of the ids file) and record it in `output/LATEST`. Usage: `setup-run.sh [IDS_FILE] [NAME]`. |
| `run-predictions.sh` | Generate baseline predictions one anchored instance at a time. Validates every `preds.json` on completion. |
| `run-memory-arm.sh` | Run the full memory arm for one integration: `run-memory-arm.sh <cure_memory\|mem0\|tencentdb> [RUN_ROOT]`. Each instance runs behind a per-instance [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) roster proxy (MAIN lane = benchmark model; role 2 = EXTRACT for cure_memory, or MEMORY for mem0/tencentdb — a zero-model-call annotate namespace). The tencentdb arm additionally manages one MemoryCore container per run root (generated gateway yaml, health wait, `docker rm -f` teardown; optional all-or-none `EMBEDDING_*` quartet in the roster `.env` enables the vector lane). Resumes by skipping instances with a valid patch (a `preds.json` with a non-empty `model_patch`); refuses to resume when an instance has a stale attempt without one. |
| `merge-predictions.sh` | Validate and merge per-instance `preds.json` files into `merged-preds.json`. Fails on anything missing, empty, or inconsistent. |
| `run-evaluation.sh` | Grade merged predictions with the local [SWE-bench](https://github.com/SWE-bench/SWE-bench) Docker harness (never `sb-cli`). |
| `summarize-report.sh` | Print the latest report's counters and instance lists with a resolved / unresolved / error verdict. |
| `summarize-memory.sh` | Aggregate every `memory.json` under a run root into a per-episode table: store deltas (added/updated/deleted), injections (count/chars), search-cache hit share, rewrite outcomes, and the cross-episode recall share from the per-hit origin lists. Read-only (no model calls, no Docker). |
| `validate_run.py` | Offline validation of one recorded proxy run directory — the `<ts>-memory-<hash>/` dir holding `trajectory.jsonl` + `run.json`, one level under `<id>/<id>/trajectory/` (any memory arm): event order, proxy-source tags, memory-index extractability, and `run.json` counters. Usage: `validate_run.py <run-dir>`. |
| `common.sh` | Shared helpers sourced by the wrapper scripts: run-root resolution, roster `.env` loading (`API_KEY`/`BASE_URL` → `OPENAI_*` mapping, model-name prefixing), and ids-file reading. |
| `merge_predictions.py` / `summarize_report.py` / `summarize_memory.py` | Logic behind their `.sh` wrappers — invoke via the wrapper, not directly. |

Phase scripts accept the run root as an optional first argument (default:
`$RUN_ROOT`, then the latest root recorded in `output/LATEST`). The
exceptions are `setup-run.sh` (it *creates* the run root) and
`validate_run.py` (it takes a recorded proxy run directory).

## Usage Examples

```bash
# Baseline arm (uses instance-ids.txt by default)
./utils/setup-run.sh
./utils/run-predictions.sh && ./utils/merge-predictions.sh && \
  ./utils/run-evaluation.sh && ./utils/summarize-report.sh

# Memory arm (pick an integration)
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt first2
./utils/run-memory-arm.sh cure_memory                 # or: mem0, or: tencentdb (needs Docker)
```

See the root [README](../README.md) for prerequisites and the
[architecture doc](../docs/en/concepts/architecture.md) for what each phase produces.

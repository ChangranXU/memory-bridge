---
description: Running the baseline arm (stock agent, no memory).
---

# Baseline Arm

The baseline arm runs stock mini-swe-agent with no memory layer — establishing the control score for A/B comparison.

## Quick run

```bash
# Set up a run root (uses instance-ids.txt by default)
./utils/setup-run.sh

# Run all four phases
./utils/run-predictions.sh
./utils/merge-predictions.sh
./utils/run-evaluation.sh
./utils/summarize-report.sh
```

## Phase details

{% stepper %}
{% step %}
### Phase 0 — Setup

```bash
./utils/setup-run.sh [IDS_FILE] [NAME]
```

Creates a timestamped run root `output/<name>-<ts>/` containing `runs/`, `local-eval/`, and a copy of the ids file. Records the path in `output/LATEST`.
{% endstep %}

{% step %}
### Phase 1 — Predictions

```bash
./utils/run-predictions.sh [RUN_ROOT]
```

Runs mini-swe-agent on each instance in the ids file, one at a time with anchored `^id$` filter. Validates every `preds.json` on completion.

The roster `.env` form is mapped automatically: `API_KEY`/`BASE_URL` → `OPENAI_API_KEY`/`OPENAI_BASE_URL`.
{% endstep %}

{% step %}
### Phase 2 — Merge

```bash
./utils/merge-predictions.sh [RUN_ROOT]
```

Validates and merges per-instance `preds.json` files into `merged-preds.json`. Fails on anything missing, empty, or inconsistent.
{% endstep %}

{% step %}
### Phase 3 — Evaluation

```bash
./utils/run-evaluation.sh [RUN_ROOT]
```

Grades merged predictions with the local SWE-bench Docker harness. Requires Docker running and the SWE-bench checkout.

{% hint style="warning" %}
Always use the local Docker harness (`run-evaluation.sh`), never `sb-cli`.
{% endhint %}
{% endstep %}

{% step %}
### Phase 4 — Summary

```bash
./utils/summarize-report.sh [RUN_ROOT]
```

Prints resolved / unresolved / error verdicts:

* **Resolved** — patch applied and tests passed
* **Unresolved** — patch applied but tests failed (model misses)
* **Errors** — evaluation failures (read per-instance logs before judging)
{% endstep %}
{% endstepper %}

## Environment

The baseline arm uses mini-swe-agent's own env with the ephemeral `litellm[proxy]` overlay. The provider `.env` at the bundle root supplies:

```dotenv
MODEL=deepseek-v4-flash
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
API=openai-chat
```

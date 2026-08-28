---
description: Get memory-bridge running in under 10 minutes.
---

# Quickstart

This guide walks you through setting up memory-bridge, running a memory-augmented evaluation, and inspecting the results.

## Prerequisites

{% hint style="info" %}
Make sure all prerequisites are installed before proceeding. Both arms require Docker for predictions and evaluation; the tencentdb memory arm additionally runs its MemoryCore container in Docker, and mem0's `server` mode runs its own two-container stack.
{% endhint %}

* Python 3.10+
* [`uv`](https://docs.astral.sh/uv/) on PATH
* Docker installed and running (`docker info` succeeds)
* A provider API key (e.g. DeepSeek, OpenAI)

## Setup

{% stepper %}
{% step %}
### Clone companion repositories

Clone the three companion checkouts. Only `mini-swe-agent` must be present before `uv sync` (it is an editable path dependency); SWE-bench is needed before any pipeline script runs, and traj-recorder only before the memory arm:

```bash
# Inside the memory-bridge directory
git clone https://github.com/SWE-agent/mini-swe-agent mini-swe-agent
git clone https://github.com/SWE-bench/SWE-bench SWE-bench
git clone -b memory https://github.com/ChangranXU/traj-recorder.git extension/traj-recorder
```

`mini-swe-agent` must sit at `mini-swe-agent/` inside the repo root. SWE-bench may alternatively live at `<parent>/SWE-bench` (a sibling of the repo), and traj-recorder at `<parent>/extension/traj-recorder` (inside an `extension/` directory of the parent).
{% endstep %}

{% step %}
### Create the provider `.env`

Create a `.env` file at the repository root (never commit this file):

```dotenv
MODEL=deepseek-v4-flash
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
API=openai-chat
```

The CURE arm wires its extraction lane automatically through the per-instance proxy, reusing `MODEL` — no extra `.env` keys needed. (The `EXTRACT_*` environment variables remain backend fallbacks when the integration runs outside the arm driver; see [Configuration](configuration.md).)

For the mem0 arm in `platform` mode, add the platform key to the same root `.env`:

```dotenv
MEM0_API_KEY=m0-...
```

The mem0 `server` and `library` modes need no platform key but require the full embedding quartet (`EMBEDDING_MODEL` / `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` / `EMBEDDING_DIMENSIONS`) in the root `.env` — the arm fails closed when the set is incomplete.
{% endstep %}

{% step %}
### Install the shared environment

```bash
uv sync
```

This creates the single `.venv` at the bundle root with all workspace members (shared-bridge, integrations) and dependencies (mini-swe-agent, litellm\[proxy]) installed.

{% hint style="warning" %}
Always run `uv sync` at the bundle root — never inside an integration directory.
{% endhint %}
{% endstep %}

{% step %}
### Verify the installation

Run the offline test suite to confirm everything is wired correctly:

```bash
uv run python -m pytest shared-bridge/tests integration/cure_memory/tests integration/mem0/tests integration/tencentdb/tests -q
```

These tests use no model calls and no Docker — they run with scripted fakes and a local capture server.
{% endstep %}
{% endstepper %}

## Run your first evaluation

### Set up a run root

```bash
# Use a small slice for a quick test
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt first2
```

This creates a timestamped directory under `output/` and records it in `output/LATEST`.

### Run the memory arm

{% tabs %}
{% tab title="CURE" %}
```bash
./utils/run-memory-arm.sh cure_memory
```

The CURE arm uses a local SQLite store with a dedicated extraction LLM. Per-instance, a roster proxy starts with MAIN (benchmark model), EXTRACT (extraction LLM), and QUERY (recall-query rewriter) lanes.
{% endtab %}

{% tab title="mem0" %}
```bash
./utils/run-memory-arm.sh mem0
```

The mem0 arm runs in the deployment mode selected by the anchored `mode:` line in `integration/mem0/configs/memory_defaults.yaml`: `platform` (default; hosted API — needs `MEM0_API_KEY` in the root `.env`), `server` (per-run OSS containers — needs Docker plus the `EMBEDDING_*` quartet), or `library` (in-process engine via the opt-in `mem0-library` group — needs the quartet).
{% endtab %}

{% tab title="tencentdb" %}
```bash
./utils/run-memory-arm.sh tencentdb
```

Needs Docker running. The driver starts one MemoryCore container per run root and removes it on exit; the optional embedding lane is enabled by the all-or-none `EMBEDDING_*` quartet in the roster `.env`.
{% endtab %}
{% endtabs %}

### Run the baseline arm (optional, for A/B comparison)

```bash
./utils/setup-run.sh /tmp/first2-ids.txt baseline
./utils/run-predictions.sh
./utils/merge-predictions.sh
./utils/run-evaluation.sh
./utils/summarize-report.sh
```

### Inspect results

```bash
# Print resolved / unresolved / error verdicts
./utils/summarize-report.sh

# Aggregate memory behavior across episodes
./utils/summarize-memory.sh
```

A healthy memory-arm instance shows `enabled: true`, `available: true`, `counts.extraction_errors: 0`, and `counts.recall_injections > 0` in its `memory.json`.

## What's next?

* Read the [Architecture](../concepts/architecture.md) to understand the layered design
* Explore the [API Reference](../api-reference/overview.md) for the standardized endpoint contract
* See [Configuration](configuration.md) for all available settings

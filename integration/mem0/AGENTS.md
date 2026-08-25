# mem0 integration — working guide

Short-form companion to `README.md` (architecture, config reference) and to
the bundle root `AGENTS.md` (pipeline, rules). This file is the what-to-run
and what-to-check sheet.

## Layout

- `src/mem0_bridge/` — the bridge package (`config`, `client`, `backend`,
  `agent`, `endpoint`, `run/swebench`).
- `configs/memory_defaults.yaml` — partial `agent.memory.*` overlay; pass AFTER
  `swebench.yaml`.
- `.env` — `MEM0_API_KEY` only. Never commit it.
- `tests/` — offline suite; runs green under
  `uv run python -m pytest integration/mem0/tests -q` from the bundle root
  (also together with the shared-bridge and cure suites in one invocation).

The memory-arm driver is bundle-level: `utils/run-memory-arm.sh mem0`
(predictions → merge → local Docker evaluation → summary). This integration
is a uv workspace member of the bundle root — it has NO uv environment of its
own; `uv sync` at `memory-bridge/` installs it into the one shared env.

## Run the memory arm (2-instance smoke)

```bash
cd memory-bridge
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt mem0first2
./utils/run-memory-arm.sh mem0
tail -f "$(cat output/LATEST | sed s|^|output/|)/memory-arm.log"
```

Everything resolves from the bundle: shared uv env, `mini-swe-agent/`,
`SWE-bench/` (via the bundle utils fallbacks), provider roster `.env`, and
`integration/mem0/.env` for `MEM0_API_KEY`.

## Checkpoints

1. Phase 1: one valid `preds.json` per instance under
   `runs/mini-swe-agent/<id>/`, plus `memory.json` and `agent.log`.
2. memory.json: `enabled: true`, `available: true`, `extraction_errors: 0`,
   `recall_injections > 0` (instance 1 after its first extraction; instance 2
   from its first model call — `scope=run` shares the user id across the run
   root). With the dirty-flag cache, `counts.search_calls` stays low (the cold
   start plus one search per extraction/rewrite boundary); most injections are
   cache-served (`recall_cache_hits`).
3. The traj.json `info.memory` block carries the same counters per step.
4. Phases 2–4: `merged-preds.json`, the Docker evaluation report, and the
   resolved/unresolved/error summary (read `summarize-report.sh` output —
   unresolved means a model miss, errors mean harness trouble).

## Rules that prevent known failures

1. Run through the shared env (`uv run --project <bundle-root>`): this
   integration is a workspace member installed by `uv sync`, so plain
   `python -m mem0_bridge...` resolves. Never run `uv` inside this directory
   and never `pip install mem0ai` into the shared env (litellm conflict
   risk).
2. One anchored instance at a time, `--workers 1`.
3. Fresh run root per arm: the driver mints the mem0 user id from the
   timestamped run-root name, and refuses stale attempts (agent.log without
   valid preds) — rerunning those would recall the aborted attempt's
   memories.
4. Keep `MEM0_API_KEY` in `.env`/env, never on the command line.
5. mem0 adds are async: the client polls the event; `poll_budget` is the
   total budget (default 120 s in the overlay). A timed-out batch is retained
   and retried at the next boundary — do not "fix" this by clearing the
   buffer on failure.
6. Pass only `user_id` (never `agent_id`) as the entity id on add — see
   README "Scoping".

## Platform facts (verified against the v3 API reference + recorded runs)

- The client's add/search/get-all ride **v3** (`/v3/memories/add|search|/`);
  v1 remains only for single-memory CRUD, `/v1/ping/`, and the `/v1/event/`
  poll the add flow uses.
- v3 search rows carry `score` (the combined multi-signal score, 0–1) and
  echo the add-time `run_id` — the provenance signal `_hit_origin` reads.
  v3 get-all rows omit `run_id` (they carry the same value under
  `session_id` — `_final_dump` reads it there).
- v3 search documents `threshold` with a server-side default of **0.1**
  (the v2 spec documented 0.3): an omitted threshold is a silent relevance
  cutoff whose value drifts across versions, so `client.search` always sends
  it explicitly (0.0 disables the cutoff). Both retrieval surfaces (the
  arm's `_search` and the endpoint adapter) share that one call; the single
  relevance door is the shared host-side `recall_min_score` floor
  (`search_threshold` stays 0.0 in the library config).
- v3 add is ADD-only on this surface ("single-pass ADD-only extraction: one
  LLM call, no UPDATE/DELETE" — the add page's prose): the arm's receipts
  show `memories_updated: 0` / `memories_deleted: 0` on every run. The
  client's UPDATE/DELETE receipt branches stay for the other surfaces it
  serves.

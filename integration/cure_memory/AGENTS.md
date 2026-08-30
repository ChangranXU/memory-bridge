# Running the CURE memory arm (automatic extraction + transient injection)

How to run the `cure_memory` integration with memory ON through the
traj-recorder proxy, so that **both** model traffics — the benchmark model and
the CURE extraction LLM — are recorded in one shared trajectory. Since schema
v6 the bridge also **annotates** the run's `trajectory.jsonl` with the core
memory protocol (session/role binds, generation, search, delivery — see
"Trajectory annotation" below).

The turnkey driver is the bundle-level memory arm (see the bundle AGENTS.md
"The two ways to run"):

```bash
head -2 instance-ids.txt > /tmp/first2-ids.txt   # any slice you want
./utils/setup-run.sh /tmp/first2-ids.txt first2  # creates output/first2-<ts>/
./utils/run-memory-arm.sh cure_memory            # uses output/LATEST
```

Per instance the driver regenerates the recorder `.env` from the provider
`.env`, starts a dedicated roster proxy, runs the bridge with
`agent.memory.scope=run`, and closes the proxy (SIGINT); afterwards it merges,
evaluates (local Docker harness), and summarizes. Validate the recorded
trajectories with `./utils/validate_run.py <run-root>/runs/mini-swe-agent/<id>/<id>/trajectory/<ts>-memory-*/`.
The sections below document the underlying manual flow for reference.

## How the pieces connect

The traj-recorder runs in **roster mode**: the workspace-root `.env` declares
two roles over one shared model catalog entry —

```dotenv
MODEL=deepseek-v4-flash          # shared by both roles (flat form)
API_KEY=sk-...                   # shared upstream credential
BASE_URL=https://api.deepseek.com
API=openai-chat
ROLE1=MAIN                       # benchmark-model traffic
ROLE2=EXTRACT                    # CURE extraction-LLM traffic
```

The driver-regenerated recorder `.env` adds `ROLE3=QUERY` (the recall-query
rewriter's lane, sharing the flat `BASE_URL`); a manual run that wants the
rewriter adds `ROLE3="QUERY"` plus `ROLE3_MODEL/API_KEY/API` here (each
defaults to the role-1 value). The QUERY lane receives no
`memory_role_bind` and emits no `memory_*` events: the proxy records its raw
model traffic natively.

One proxy process opens **one trajectory** and publishes two role-scoped base
URLs into it: every benchmark call and every extraction call lands in the same
`trajectory.jsonl`, tagged `"proxy_source": "MAIN"` / `"EXTRACT"`. Clients
receive only the placeholder credential `trajectory-proxy`; the proxy injects
the real key upstream per role.

```text
mini-swe-agent (LitellmModel, openai/$MODEL)
   OPENAI_BASE_URL = http://127.0.0.1:4000/MAIN/trajectories/<id>/v1   ─┐
CURE extraction client (EXTRACT_*, direct HTTP, bare model name)        ├─► one trajectory
   EXTRACT_BASE_URL = http://127.0.0.1:4000/EXTRACT/trajectories/<id>/v1 ┘
```

## Prerequisites

- `uv` installed; Docker running (`docker info` succeeds).
- The provider `.env` in the roster form above at the bundle root (or the
  parent workspace). The driver regenerates the recorder's own `.env` from it
  each run (mode 0600, never committed); a manual run must do that itself:
  `cp .env extension/traj-recorder/.env && chmod 600 extension/traj-recorder/.env`
- `CURE_MEMORY_REPO` points at the integration's `src/` tree
  (`memory-bridge/integration/cure_memory/src`) — the driver exports this
  itself; set it only for manual runs. `MSWEA_COST_TRACKING=ignore_errors`
  unless the model is in LiteLLM's pricing map.
- Fresh run root per arm (`output/<name>-<ts>/` from `utils/setup-run.sh`).
  With `agent.memory.scope=run` the SQLite store is shared across instances,
  so a dirty root contaminates the arm.

## 1. Start the proxy (one per arm, before the first instance)

The recorder checkout resolves bundle-first (`extension/traj-recorder/`), else
from the parent workspace (`../extension/traj-recorder/`):

```bash
WORKSPACE=/Users/changranxu/Downloads/memory-integration/memory-bridge
cd "$WORKSPACE/../extension/traj-recorder" && uv run trajectory \
  --output "$WORKSPACE/output/manual/trajectory" --label cure-v2
```

Startup prints the two role base URLs and writes one env file per role slot
inside the trajectory run dir — `.proxy_env_role1` (MAIN) and
`.proxy_env_role2` (EXTRACT). Keep this process alive for the whole arm;
`GET /healthz` reports liveness.

## 2. Run one instance (memory ON)

```bash
WORKSPACE=/Users/changranxu/Downloads/memory-integration/memory-bridge
TRAJ_DIR="$WORKSPACE/output/manual/trajectory/<ts>-cure-v2-<rand>"   # from proxy startup output
INSTANCE_ID=pydata__xarray-2905

set -a; source "$TRAJ_DIR/.proxy_env_role1"; set +a   # OPENAI_API_KEY + MAIN OPENAI_BASE_URL + MODEL
EXTRACT_BASE_URL=$(zsh -c 'source "'"$TRAJ_DIR"'/.proxy_env_role2"; print $OPENAI_BASE_URL')
export EXTRACT_MODEL="$MODEL" EXTRACT_BASE_URL EXTRACT_API_KEY="$OPENAI_API_KEY"
export CURE_MEMORY_REPO="$WORKSPACE/integration/cure_memory/src" MSWEA_COST_TRACKING=ignore_errors

# The shared env at the bundle root carries mini-swe-agent, litellm[proxy],
# and both bridge packages (editable) — no --with overlays needed.
cd "$WORKSPACE/mini-swe-agent" && uv run --project "$WORKSPACE" \
  python -m cure_memory_bridge.run.swebench \
  --subset verified --split test --filter "^${INSTANCE_ID}$" --workers 1 --redo-existing \
  --model "openai/$MODEL" \
  --config swebench.yaml \
  --config environment.pull_timeout=900 \
  --config "$WORKSPACE/integration/cure_memory/configs/memory_defaults.yaml" \
  --config agent.memory.enabled=true \
  --config agent.memory.scope=run \
  --config agent.memory.output_dir="$WORKSPACE/output/manual/runs/mini-swe-agent/${INSTANCE_ID}" \
  --output "$WORKSPACE/output/manual/runs/mini-swe-agent/${INSTANCE_ID}"
```

Notes on the wiring:

- The benchmark model gets `openai/$MODEL` (LiteLLM provider prefix — skip the
  prefix when `$MODEL` already carries one, or set `MSWEA_MODEL_NAME` to the
  full LiteLLM name); the extraction client is a plain HTTP adapter and takes
  the **bare** `$MODEL`.
- `EXTRACT_API_KEY`/`OPENAI_API_KEY` are both just `trajectory-proxy` (the
  placeholder) — the real key never leaves the proxy's `.env`.
- The shipped overlay enables the recall-query rewriter (`rewrite_every_n_steps: 10`)
  and the backend start fail-closed-validates its connection: a manual run must
  either export the QUERY lane (`MEMORY_QUERY_MODEL_URL` — derive it like
  `EXTRACT_BASE_URL` from the role3 slot — plus `MEMORY_QUERY_MODEL="$MODEL"` and
  `MEMORY_QUERY_API_KEY=trajectory-proxy`) or turn rewriting off with
  `--config agent.memory.rewrite_every_n_steps=0`. Otherwise the backend starts
  unavailable and the episode runs with no memory at all.
- The bridge packages are editable installs in the shared env, so local edits
  take effect on the next run with no reinstall step.

## 3. Stop the proxy

SIGINT/SIGTERM (never SIGKILL) after the last instance: the proxy finalizes
each open run with `agent_end` and `run.json` status `stopped` plus cumulative
usage totals.

## Multiple instances

Run them sequentially (`--workers 1`, anchored `^id$` filter each time — the
run-scope DB is shared). Each instance is a fresh agent process, so give each
its own trajectory inside the same proxy — mint one per instance and re-derive
the env from the new run dir:

```bash
curl -sS -X POST "${OPENAI_BASE_URL%/v1}/new?label=${INSTANCE_ID}"
# → 201 {run_id, base_url, proxy_env: <new run dir>/.proxy_env_role1, api_format}
```

A `/new` minted under a role prefix preserves that role but publishes **only
that slot's** env file; both role prefixes address every live trajectory, so
derive the extraction URL by swapping the role segment:
`EXTRACT_BASE_URL=${OPENAI_BASE_URL/\/MAIN\//\/EXTRACT\/}` after sourcing the
new `.proxy_env_role1`. Restarting the proxy per instance works too — never
point an instance at a `.proxy_env_role*` whose proxy has exited (every call
fails with `Unknown trajectory ID`).

## Outputs

Per instance, under `<run-root>/runs/mini-swe-agent/<id>/`:

- `preds.json` — the prediction (Phase 2 merge input).
- `memory.json` — CURE episode log: settings, counts (`messages_recorded`,
  `extraction_calls`/`extraction_errors`, candidates/approved/rejected by
  reason, `recall_injections`), extraction/recall events, final memories (the
  episode's lattice — its repo's rows plus the general layer, each row
  carrying its `project_id`).
- `<id>/<id>.traj.json`, `minisweagent.log`, `exit_statuses_*.yaml`.
- Driver runs add `proxy.log` and `<id>/<id>/trajectory/` (the per-instance
  proxy recording: `trajectory.jsonl` — one canonical event log for both
  roles, `proxy_source` tags on content events — plus `calls/`, the per-call
  wire ground truth). The transient memory injection is visible in MAIN
  `calls/*/request.json` but never in the agent's own `<id>.traj.json`.

Run-level:

- `<run-root>/runs/mini-swe-agent/cure_memory.sqlite3` — the shared run-scope
  memory store; instance order matters, memories accumulate across instances.
- `<run-root>/merged-preds.json`, `local-eval/`, `memory-arm.log`.

## Verifying a run

```bash
./utils/validate_run.py "<run-root>/runs/mini-swe-agent/<id>/<id>/trajectory/<ts>-memory-*"
```

Cross-checks that should hold:

- EXTRACT calls in `calls/` == `extraction_calls` in every `memory.json` of
  the arm; MAIN calls == agent steps.
- `memory.json` shows `enabled: true`, `available: true`,
  `extraction_errors: 0`, `recall_injections` > 0 from the step after the
  first approval.
- `preds.json` non-empty per instance before any merge/evaluation.

## Retrieval semantics and provenance

- Two-layer applicability: every memory's layer is fixed once, at extraction,
  by the decision LLM's `scope` — `project` rows are repo-bound
  (`project_id = _repo_of(instance_id)`, the instance id minus its trailing
  `-<number>`; retrievable only inside episodes of that repository), `user`
  rows are general repo-independent lessons (`project_id = NULL`, visible to
  every episode of the run). The store's `(project_id = ? OR project_id IS
  NULL)` predicate yields the lattice directly: an episode sees its own repo's
  rows plus every general row, never another repo's rows. The extractor fails
  closed — a missing or malformed scope lands repo-bound (a wrongly-project
  memory only fails to help, a wrongly-general one leaks); a candidate
  extracted under a project-less session (the endpoint's `add` path) is
  labeled `user`, since a `project` label with NULL `project_id` would claim
  repo-bound while flowing everywhere — and `_upsert_memory`'s supersede
  never crosses layers in either direction: a general candidate cannot
  supersede a repo-bound row of the same type+key, and a repo-bound candidate
  cannot supersede a general row either — the general layer is shared
  run-wide, so one repo's refinement must not destroy it for every other repo
  (the two coexist; the repo-bound row overlays the general one in that
  repo's recall). The identical-content dedupe no-op still spans layers — the
  same value is already persisted and visible, so nothing new is stored
  either way; the audit replay (`_first_active_row`) mirrors that no-op
  predicate exactly. Deletions get the same layer treatment: a deletion
  decision may carry an optional `scope` and stays in the session's own layer
  without one — a repo episode's forget removes only that repo's matching
  rows and cannot silently wipe the shared general layer run-wide unless it
  explicitly says `scope: "user"`. A `scope: "project"` deletion binds to the
  session's own repository only (a project-less session matches nothing), and
  terminal rows (`deleted`/`superseded`/…) are never re-matched, so one
  logical deletion counts once and history markers survive.
- The endpoint adapter and the backend's `_search()` ride the same native
  `memory_search` call (same ranking, same defaults) but deliberately differ
  in breadth: the arm's `_search()` passes `_project_id()`, so recall is
  narrowed to the episode's repo plus the general layer, while
  `CureMemoryEndpoint.search` keeps the standardized contract's user-wide
  semantics — `user_id` stays the sole interop boundary (the contract's
  `SearchRequest` carries no project field) and layering is arm-internal
  storage semantics.
- Recall lines name the layer — `- [<memory_type>:general] key: value` /
  `- [<memory_type>:repo] key: value` — derived from `project_id` (the field
  the lattice, the upsert guard, and the audit replay all key on), and carry
  a provenance suffix from `sources[0].session_id`: the episode that created
  the memory's CURRENT version (`memory_replace` preserves the original row's
  sources; the extraction path's `_upsert_memory` re-stamps the superseding
  episode's on a value change). The native search's term-overlap score rides
  `metadata["score"]` as a transient search-time annotation (never
  persisted); `recall_min_score` filters on it (the arm overlay pins 2 — the
  native search already drops zero-overlap rows, so 1 would be a no-op).

## Trajectory annotation (schema v6)

With `agent.memory.annotate=true` (the default) and both lanes on the roster
proxy, the bridge posts the core memory protocol to each lane's
`.../trajectories/<id>/annotate` endpoint (derived from the lane's model base
URL; explicit `agent.memory.annotate_main_url`/`annotate_memory_url` or
`$MEMORY_ANNOTATE_MAIN_URL`/`$MEMORY_ANNOTATE_MEMORY_URL` must match the
derived prefix through the trajectory ID):

- `memory_session` (exact task inline) + `memory_role_bind(main|memory)` at
  `set_task()`;
- `memory_generate_start` -> `memory_change`(s) -> `memory_generate_end` per
  extraction (CURE counts/checkpoint/mutation-audit under `extensions.cure`).
  A start event that would exceed the recorder's 1 MiB body cap degrades its
  inputs to digest-only refs (`availability: unavailable`, `reason: oversize`,
  sha256 kept) so the operation stays traced — recorded as
  `annotation_inputs_degraded` in `memory.json`;
- `memory_search_start` -> `memory_search_end` per recall (exact query and
  ordered returned refs; CURE matched/selected/rendered/budget counts under
  `extensions.cure`);
- `memory_delivery` per placed transient block on the MAIN lane, with a
  canonical-message proof binding exactly the query's model call(s).

Retrieval is then a one-pass affair over `trajectory.jsonl`
(`trajectory_proxy.retrieval.build_memory_index`): sessions, role lanes,
operations with change series, deliveries, and portable identities — no
prefix/role-name heuristics. `compare_minisweagent.py --logical-role main`
resolves the main lane from the `memory_role_bind` events. Annotation never
alters native behavior or wall-time preflights; failures degrade to untraced
native work with a credential-free `memory.json` event
(`annotation_start_oversize` / `annotation_recovery_conflict` /
`annotation_change_rejected` / `annotation_delivery_unconfirmed` /
`annotation_delivery_rejected` / `annotation_delivery_no_call` — the last
marks a placed block whose crashed model call never reached the lane, so no
delivery could bind). URLs in
`memory.json` and logs are sanitized: the bearer trajectory ID appears only
as the 16-hex `run.json.trajectory_id_hash` prefix.

The turnkey driver is `utils/run-memory-arm.sh cure_memory` (see the top of
this file): it regenerates the recorder `.env` from the provider `.env`
(mode 0600), starts one roster proxy per instance, runs the bridge with
`agent.memory.scope=run`, SIGINTs the proxy so each run dir finalizes, then
merges, evaluates, and summarizes. `utils/validate_run.py` checks one recorded
trajectory dir end to end (event order, proxy-source tags, memory-index
extractability, `run.json` counters).

When driving manually, Phase 2–4 as usual: `utils/merge-predictions.sh` →
`utils/run-evaluation.sh` (local Docker harness, never `sb-cli`) →
`utils/summarize-report.sh`, pointing them at the run root (the turnkey
driver already runs them itself). Don't evaluate until merge passes — the
report denominator is misleading with missing/empty patches.

## Rules that prevent known failures

1. Always invoke the bridge through the shared env (`uv run --project
   <bundle-root> ... python -m cure_memory_bridge.run.swebench`) — it carries
   `litellm[proxy]`; without those extras the first model call dies with
   `ModuleNotFoundError: No module named 'fastapi'`.
   Config layering: `swebench.yaml` → `memory_defaults.yaml` → dotted
   `agent.memory.*` overrides.
2. One instance at a time, anchored `^id$` filter, `--workers 1` — never a
   broad regex; the chronological ids file is the source of truth.
3. The driver is `utils/run-memory-arm.sh cure_memory`; the older workspace
   drivers one level up (`../utils/run-v2-memory.sh`, the smoke pair) predate
   this bundle — do not copy them in.
4. Fresh run root per arm; SIGINT (not SIGKILL) the proxy; never reuse a
   `.proxy_env_role*` from a dead proxy.
5. Keep `EXTRACT_*` in the environment; never pass the extraction settings as
   dotted CLI overrides (visible in the process command line) — the placeholder
   key is harmless, but the habit protects real keys.

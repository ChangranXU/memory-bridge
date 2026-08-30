# memory-bridge overview

- memory-bridge bundles everything needed to run memory bridge experiments
  against SWE-bench Verified with mini-swe-agent: the prediction runner
  (mini-swe-agent/), the GENERIC memory bridge (shared-bridge/), one
  directory per memory system (integration/<name>/), and the pipeline
  scripts (utils/). It is the single place to work from; the originals it
  was assembled from are archived at `../.bak` (see "Relationship to the
  original workspace" below).
- shared-bridge/ is generic: nothing in it ever names a specific
  integration — a test in shared-bridge/tests scans the shared sources for
  integration names. Adding a memory system means adding
  integration/<name>/ that binds shared_bridge.agent.MemoryAgent
  (backend_class/config_class), subclasses
  shared_bridge.backend.BaseMemoryBackend (the lifecycle skeleton: start /
  set_task / record / extract / recall / finalize, with hooks for every
  legitimate divergence), and implements the
  shared_bridge.endpoint.MemoryEndpoint contract.
- There is exactly one environment: the bundle root's shared uv env.
  shared-bridge and the integrations are uv workspace members (installed
  editable); mini-swe-agent is an editable path dependency and
  litellm[proxy] a regular one, so
  `uv run --project . ...` from anywhere has the full stack importable
  (dropping litellm[proxy] revives the `ModuleNotFoundError: fastapi`
  failure mode on the first model call). Integrations never carry their own
  uv environment — never run `uv` inside integration/<name>/ (that creates
  a local .venv/uv.lock), never `pip install` into the shared env, never
  import an integration via a PYTHONPATH hack: if a package is not
  importable after `uv sync`, fix the workspace membership instead.
  mini-swe-agent/ keeps its own .venv for its own tooling; the shared-env
  flows need no --with overlays.
- The standardized memory endpoint (shared_bridge.endpoint) fixes one
  contract for memory actions — add / search / update / delete — with
  synchronous writes (success only after persistence) and `user_id` as the
  sole retrieval-isolation boundary; each integration ships its adapter
  (see "The standardized memory endpoint" below).

The project is structured as:

```bash
pyproject.toml + uv.lock          # the one shared uv env (workspace root): the members below editable + mini-swe-agent + litellm[proxy]
instance-ids.txt                  # ordered instance list (default pipeline input)
mini-swe-agent/                   # prediction runner checkout (keeps its own .venv)
shared-bridge/src/shared_bridge/  # GENERIC components: agent hooks, BaseMemoryBackend lifecycle skeleton, config.py (MemoryConfig), run factory, annotate transport, endpoint contract + stdlib HTTP front, testing.py (the suites' shared capture server), prompts.py + side_model.py
shared-bridge/tests/              # generic suites (fake reference integration) + the zero-integration-naming scan
integration/cure_memory/          # CURE integration (own AGENTS.md): src/cure_memory (the CURE memory system), src/cure_memory_bridge (backend/agent glue + endpoint adapter), configs/, tests/
integration/mem0/                 # mem0 integration, three deployment modes — platform/server/library (own AGENTS.md + VENDORING.md): src/mem0_bridge (stores/ — the per-mode Mem0Store implementations: platform REST client, OSS server client, in-process library — plus backend, agent glue, endpoint), configs/ (memory_defaults.yaml carries the anchored mode: line), tests/, vendor/mem0 (gitignored vendored clone, never committed)
integration/tencentdb/            # TencentDB-Agent-Memory integration (own AGENTS.md + VENDORING.md): src/tencentdb_bridge (gateway REST client, backend, agent glue, endpoint), src/TencentDB-Agent-Memory (gitignored vendored clone, never committed), configs/, tests/ (a tencentdb.tests package)
utils/                            # all pipeline scripts (utils/README.md says what each does)
docs/                             # GitBook-ready documentation site (en/ + zh/ mirrors)
output/                           # run roots (created by setup-run.sh)
```

Runtime inputs are the provider roster `.env` (the bundle root's own — the
scripts still fall back to a parent-workspace copy, but the parent's `.env`
is archived at `../.bak/.env`, so the bundle's own is the live source;
never commit it):

```dotenv
MODEL=deepseek-v4-flash
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
API=openai-chat
```

plus the optional `QUERY_MODEL` / `QUERY_API_KEY` / `QUERY_API` keys selecting
the recall-query rewriter's model lane (each defaults to the role-1 value; the
lane shares the flat `BASE_URL` — a separate QUERY upstream would need the
per-role `BASE_URL` roster form, which the driver deliberately does not wire),
plus the shared checkouts SWE-bench/ (local Docker harness) and
extension/traj-recorder/ (trajectory proxy, memory arm only), resolved
bundle-first then parent workspace, `MEM0_API_KEY` in the bundle root's own
`.env` (mem0 platform mode only), and the embedding quartet
(`EMBEDDING_MODEL` / `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` /
`EMBEDDING_DIMENSIONS` in the roster `.env`): REQUIRED and fail-closed for
the mem0 server/library modes (the OSS engine embeds on every add/search,
no lexical fallback), optional all-or-none for the tencentdb arm (a partial
set is silently disabled upstream, so the driver refuses it). The mem0
server mode and the tencentdb arm need Docker running — tencentdb pulls
`agentmemory/memory-core:1.0.1-beta.1`, mem0 server builds its stack from
the vendored clone (127.0.0.1:8890 is a per-machine single-arm lock, as is
tencentdb's 8420). Prerequisites: `uv` on PATH; Docker
installed and running (`docker info` succeeds); `uv sync` has created the
shared env.

# Build, test, and development commands

- `uv sync` — create the one shared env at the bundle root (`.venv`); run
  it at the bundle root, never inside an integration.
- `uv run python -m pytest shared-bridge/tests integration/cure_memory/tests integration/mem0/tests integration/tencentdb/tests -q` —
  the offline checks (no model calls, no Docker; scripted decision/client
  fakes, a local capture server, mock HTTP transports): shared-bridge/tests
  covers the generic components (annotation transport, endpoint contract +
  HTTP round-trip, the backend lifecycle skeleton via a fake reference
  integration — failure-path artifact pins and the zero-naming scan
  included), integration/cure_memory/tests the CURE backend/agent/tracing
  and its endpoint adapter, integration/mem0/tests the mem0 REST client,
  backend, endpoint adapter, and agent glue, integration/tencentdb/tests
  the gateway REST client, backend (drain semantics, layered recall,
  origin attribution, agent-initiated read/search observation), endpoint
  adapter, and agent glue.
- `./utils/setup-run.sh [IDS_FILE] [NAME]` — Phase 0: a fresh timestamped
  run root under output/ (recorded in output/LATEST for the later phases).
- `./utils/run-memory-arm.sh <cure_memory|mem0|tencentdb> [RUN_ROOT]` — the memory
  arm end to end (see "The two ways to run"): predictions with memory ON
  (scope=run) through the selected integration, then merge → local Docker
  evaluation → summary.
- `./utils/run-predictions.sh` → `merge-predictions.sh` →
  `run-evaluation.sh` → `summarize-report.sh` — the baseline arm phases
  (stock mini-swe-agent, no bridge); each takes the run root as an optional
  first argument, else $RUN_ROOT, else the newest root in output/LATEST.
- `./utils/validate_run.py <run-dir>` — validate one recorded proxy run
  dir (the `<ts>-memory-<hash>/` directory holding `trajectory.jsonl` and
  `run.json`, one level under `<id>/<id>/trajectory/`; any memory arm):
  event order, proxy-source tags, memory-index extractability, run.json
  counters.

# Style guide

1. Target Python 3.10 or higher (the workspace `requires-python`). Use
   modern typing: `str | None`, built-in generics; annotate public
   functions and dataclass-style fields with parameterized types.
2. The one shared env is the only dependency source. `shared-bridge`
   rides stdlib + pydantic alone, with `minisweagent` imported lazily
   (the run factory, the `__init__` agent shim) so the endpoint contract
   stays importable without the benchmark stack; the mem0 platform/server
   clients use the httpx the env already carries for litellm — never the
   `mem0ai` SDK (only mem0 library mode imports `mem0ai`, and only via the
   opt-in `mem0-library` dependency group, which never enters
   default-groups, so the shared env stays mem0ai-free).
3. Failure discipline is part of the style: bridge code fails closed —
   nothing raises into the agent loop unless `strict: true`,
   `note_recall` never raises at all, and annotation failures degrade to
   untraced native work, never to behavior changes.
4. Credentials live in pydantic fields with `exclude=True, repr=False`;
   only sanitized URLs (userinfo/query/fragment stripped, the trajectory
   ID replaced by its 16-hex hash prefix) reach artifacts and logs.
5. Docstrings and comments explain *why* — protocol rules, failure
   discipline, what the recorder guarantees — not *what* the code does.
6. Bash pipeline scripts open with a usage header, source
   `utils/common.sh` for the shared helpers, and run under
   `set -euo pipefail` (the long-running memory-arm driver relaxes to
   `set -u`).

## Test style

1. Offline only: no model calls, no Docker. Model traffic is scripted
   (`DeterministicToolcallModel`, fake decision/platform clients), HTTP
   rides mock transports and a local capture server, and artifacts land
   in `tmp_path`.
2. shared-bridge/tests exercises the generic components through a fake
   reference integration — nothing in the shared sources may name a real
   one (the zero-integration-naming scan is a test; keep it green).
3. Pin the failure paths, not just the happy ones: failed-start
   artifacts, containment, breaker semantics, resume guards. A behavior
   change that alters a failure artifact updates its pins in the same
   change.
4. Name tests `test_<behavior>`; every test should exercise a real point
   of failure, not just a status code.
5. Run the full offline suite (see "Build, test, and development
   commands") before opening a PR — one invocation from the bundle root
   covers all four suites.

# The standardized memory endpoint

`shared_bridge.endpoint` fixes one contract for memory actions — `add` /
`search` / `update` / `delete` — reconciling the Agent Memory Leaderboard
synchronous Add/Search contract (agentmemories.ai) and the mem0 Platform v1
CRUD API: writes are synchronous (success only after persistence),
`user_id` is the sole retrieval-isolation boundary, and search returns
`{"data": [{id, content, score?, created_at?}, ...]}` capped at `top_k`.
`shared_bridge.serve` exposes any `MemoryEndpoint` over HTTP (stdlib only):

```text
GET    /health                  POST /v1/memories/search/
POST   /v1/memories/            PUT /v1/memories/{id}    DELETE /v1/memories/{id}
```

Each integration provides its adapter (CURE:
`cure_memory_bridge.endpoint.CureMemoryEndpoint`; mem0:
`mem0_bridge.endpoint.Mem0Endpoint`; tencentdb:
`tencentdb_bridge.endpoint.TencentDBEndpoint`).

An integration implements retrieval exactly twice — the backend's `_search()`
for the arm and `MemoryEndpoint.search` for the standardized surface — and
both must share one semantics (one native call, same ranking/filter
behavior). The arm's measured surface is the reference; the endpoint adopts
it, never a platform/server-side default. One deliberate breadth carve-out:
an integration may narrow the arm's `_search()` by its own storage-internal
applicability layer (cure_memory's repo/general lattice under `scope=run`;
tencentdb's native `task_id` repo tier) — the contract's `SearchRequest`
carries no project field, so the endpoint keeps the user-wide semantics and
the layer stays arm-internal.

# Recall pipeline: policy header, provenance, floors, cache, and the QUERY lane

The shared recall path (the `MemoryConfig` fields persist into `memory.json`
settings via the base's `_core_initial_settings()` — the credential fields,
the `annotate_*` knobs, and `output_dir` stay out of artifacts, and `enabled`
/ `scope` / `user_id` land as top-level `memory.json` keys instead — which
every integration's `_initial_settings()` splices):

- The injected block's header is base-composed: the shared policy preamble
  (`shared_bridge/prompts.py:RECALL_POLICY_DEFAULT` — this block is
  auto-injected reference context, do not respond to it) plus the
  integration's `_recall_sections()`. Integrations compose, never override.
  `max_chars_per_memory` (0 = off, the native default) and
  `max_total_recall_chars` (default 2000 = the shipped bound, 0 = off) bound
  the rendered memory LINES only (the header is not counted): per-line
  truncation first, then truncate-to-fit against the total budget with a
  40-char floor, the walk continuing past an unfittable line; a
  `_hit_budget_exempt` line renders in full outside both budgets but keeps
  its `max_memories` slot. The payload's `chars` still means "what was
  placed".
- Every rendered line carries a provenance suffix from the `_hit_origin()`
  hook ("from this episode" / "from earlier episode <instance>" / "from an
  earlier episode"); the per-hit origin list is logged on each recall event
  in `memory.json` (read by `utils/summarize-memory.sh`).
- `recall_min_score` (default None) is the single host-side relevance door:
  hits below the floor — or carrying no `_hit_score()` — are dropped before
  the `max_memories` slice and rank-then-fill. Score scales are
  integration-defined and never comparable across integrations.
- `search_timeout` bounds one native search call where the search is a
  network call; search/rewrite seconds accrue to a backend-owned accumulator
  drained through the same `consume_annotation_duration()` the agent's
  wall-time preflight already exempts (works with `annotate: false`).
- A dirty-flag search cache skips re-searches: the flag is set by episode
  start, a successful rewrite, and every counted extract tick — clean or
  failed (a failed extraction may have written, e.g. a hosted
  write-then-poll-timeout, so the cache is invalidated conservatively);
  `recall_cache_hits` counts cache-served injections. Cache-hit deliveries
  cite the memoized search's `operation_id` with a fresh `delivery_id` and
  `cached: true` in the adapter extensions; a failed search is never cached
  (`search_errors`, recall None that step, flag stays set).
- The query rewriter (`rewrite_every_n_steps`, default 0 = off) replaces the
  recall query every N steps via the QUERY proxy lane: the driver declares
  `ROLE3="QUERY"` in the regenerated recorder `.env` for every memory arm
  and exports `MEMORY_QUERY_MODEL_URL` / `MEMORY_QUERY_MODEL` /
  `MEMORY_QUERY_API_KEY="trajectory-proxy"` per instance. With rewriting
  enabled, `start()` fail-closed-validates the rewriter settings (the same
  check EXTRACT uses), and a lane that keeps failing stops being retried
  after `rewrite_max_consecutive_errors` (default 3, 0 = never break)
  consecutive failures — the same breaker shape as extraction. The QUERY
  lane receives NO `memory_role_bind` and emits no `memory_*` events — the
  proxy records its raw model traffic natively. The search start posts the
  current query with a `query_source: "task" | "rewritten"` adapter
  extension.
- Counters (`memory.json` `counts.*`): the core set includes `search_errors`,
  `recall_cache_hits`, `rewrite_calls`, `rewrite_successes`,
  `rewrite_failures`; a failed search counts both `search_errors` (the
  per-op grain, counted privately by `_search`) and `backend_errors` (the
  envelope grain).

Prompt homes: every prompt lives in a prompts module — shared text in
`shared_bridge/prompts.py`, integration text in the integration's own
prompts module (`cure_memory/prompts.py`, `mem0_bridge/prompts.py`,
`tencentdb_bridge/prompts.py`) — never inline in backend/agent/client
code. Fixed-format side-model calls ride
`shared_bridge/side_model.py` (a pydantic-model envelope as the single
source of truth; fail-closed on every error class).

Extraction guidelines (`EXTRACTION_GUIDELINES_DEFAULT` in
`shared_bridge/prompts.py`, overridden per run via the `MemoryConfig` field
`extraction_guidelines` — "" = the default, non-empty = the run's override,
replacing it wholesale) are the one extraction-policy layer shared across
integrations; the output-schema half of a local extractor's prompt is never
unified (it encodes each system's native data model). The default's Organize
clause asks for each memory's applicability to be made explicit —
deliberately brief and system-neutral, so it never collides with a memory
system's own scoping vocabulary (CURE's project/user lattice stays in CURE's
prompts). Whatever the policy form, the base appends one episode-context
section after it (`extraction_episode_context` in `shared_bridge/prompts.py`:
the episode's instance id plus its repository key via
`shared_bridge.backend._repo_of` — the same key cure's `_project_id()` uses
under `scope="run"`): the context is episode fact, so a run's override
replaces the policy, never the context. Conveyance is
capability-gated on the backend (`_CONVEYS_EXTRACTION_GUIDELINES`, a
class-level capability declaration like `_COUNTERS`): CURE appends the
combined text into its `MEMORY_POLICY_PROMPT`
(`cure_memory/prompts.py:memory_policy_prompt`);
mem0 sends it as the add endpoint's advisory per-request
`custom_instructions`; an integration whose engine accepts no custom prompt
rules conveys nothing — tencentdb's containerized extractor is that case
(settings record "", a configured override draws one start-time warning). The conveyed text persists into `memory.json`
settings as `extraction_guidelines`.

# The two ways to run

## A. Memory arm — `utils/run-memory-arm.sh <integration>`

The main event: predictions with memory ON (`scope=run`) through the
selected integration, then merge → local Docker evaluation → summary. The
integration argument selects `integration/<name>/`:

```bash
head -2 instance-ids.txt > /tmp/first2-ids.txt   # any slice you want
./utils/setup-run.sh /tmp/first2-ids.txt first2  # creates output/first2-<ts>/
./utils/run-memory-arm.sh cure_memory            # or: mem0 (uses output/LATEST)
```

- **`cure_memory`** — per instance the driver starts a dedicated roster
  proxy (MAIN lane = benchmark model, EXTRACT lane = CURE extraction LLM,
  QUERY lane = the recall-query rewriter, all recorded in one shared,
  annotated trajectory), runs mini-swe-agent with the CURE integration, and
  closes the proxy (SIGINT); the run root holds the shared run-scope SQLite
  store `runs/mini-swe-agent/cure_memory.sqlite3` plus per-instance proxy
  artifacts (`proxy.log`, `<id>/trajectory/` — paths relative to
  `runs/mini-swe-agent/<id>/`). The driver regenerates
  the recorder's `.env` from the provider `.env` each run.
- **`mem0`** — three deployment modes, selected by the anchored `mode:`
  line in `integration/mem0/configs/memory_defaults.yaml` (yaml-owned: the
  driver reads the same line and refuses `--config agent.memory.mode=`
  extras). **`platform`** (default) talks to the hosted API and needs
  `MEM0_API_KEY` in the bundle-root `.env`. **`server`** runs a per-run
  self-hosted OSS stack — two containers (pgvector plus the API server
  built from the vendored clone, engine pinned `mem0ai==2.0.19`) on one
  bridge network at `127.0.0.1:8890` under a machine-wide single-arm claim,
  store volumes under `<run-root>/mem0-server/`, and a fail-closed
  `EMBEDDING_*` quartet requirement. **`library`** runs the `mem0ai` engine
  in-process via the opt-in `mem0-library` dependency group (the driver
  carries `--group mem0-library` on every instance invocation), store under
  `<run-root>/mem0/`, same quartet requirement. Every mode runs the same
  per-instance roster proxy (MAIN lane = benchmark model; MEMORY lane =
  the memory-annotate namespace, which makes zero model calls — extraction
  runs off-trajectory: hosted by the platform, inside the server
  container, or in-process; QUERY lane = the recall-query rewriter, as on
  the cure arm); run isolation comes from a per-run user id minted from
  the timestamped run-root name.
- **`tencentdb`** — one MemoryCore container per run root
  (`agentmemory/memory-core:1.0.1-beta.1`, port `127.0.0.1:8420`, data
  volume `<run-root>/tdai/data`, credential-free generated gateway yaml
  with `${TDAI_*}` leaves interpolated from docker-injected env; removed
  on exit). The container's extraction LLM points directly at the provider
  upstream (extraction traffic is not recorded in the trajectory — the
  mem0 treatment); the arm runs the same per-instance roster proxy as mem0
  (MEMORY lane = zero-model-call annotate namespace from watermark-resolved
  API receipts; QUERY lane = the rewriter). Run isolation = per-run user id
  plus the fresh per-run volume; repo scoping rides the native `task_id`
  (the episode's repo key). Port 8420 is a per-machine single-arm lock.

Every arm lands the same core artifacts in the run root: per instance
`runs/mini-swe-agent/<id>/preds.json`, `memory.json` (the integration's
episode log), `<id>/<id>.traj.json`, and `agent.log` (the last three
relative to `runs/mini-swe-agent/<id>/`); plus
`runs/mini-swe-agent/merged-preds.json`, the evaluation report under
`local-eval/`, and `memory-arm.log`. The driver resumes by skipping
instances that already have a valid patch (a `preds.json` with a non-empty
`model_patch`). Resume is refused
when the shared store exists and a listed instance has a stale attempt
(`agent.log`) without a
valid patch — re-running it would recall memories approved during the
aborted attempt (rule 3), so start a fresh run root.

## B. Baseline arm (no memory) — the stock pipeline

```bash
./utils/setup-run.sh [IDS_FILE] [NAME]
./utils/run-predictions.sh    # stock mini-swe-agent, no bridge
./utils/merge-predictions.sh
./utils/run-evaluation.sh     # local SWE-bench Docker harness
./utils/summarize-report.sh
```

Each phase accepts the run root as an optional first argument; otherwise
`$RUN_ROOT`, else the newest root recorded in `output/LATEST`. The roster
`.env` form works here too (`API_KEY`/`BASE_URL` are mapped to
`OPENAI_API_KEY`/`OPENAI_BASE_URL` automatically). The baseline arm runs
pure mini-swe-agent code, so it keeps using `mini-swe-agent/`'s own env
(with the ephemeral `litellm[proxy]` overlay).

# Reading the results

- `summarize-report.sh` prints resolved / unresolved / error verdicts. A
  healthy run has submitted == completed == number of ids. Unresolved =
  model misses (patch applied, tests failed); errors = evaluation failures
  — read the per-instance logs it points to before judging the model.
- Memory arm specifics: `memory.json` should show `enabled: true`,
  `available: true`, `counts.extraction_errors: 0`, and
  `counts.recall_injections` > 0
  from the step after the first approved memory (with `scope=run`, the
  second instance recalls from its first model call — general memories
  run-wide, repo-bound memories only within their own repository).
  `summarize-memory.sh` aggregates every `memory.json` under a run root into
  a per-episode table (store deltas, injections, cache-hit share, rewrite
  outcomes, agent-initiated read observation, cross-episode recall share
  from the per-hit origin lists, annotation-transport degradation count).

# Rules that prevent known failures

1. Run the memory arm and the merge/summary phases through the shared
   env: `uv run --project <bundle-root> ...` (baseline predictions use
   mini-swe-agent's own env with the ephemeral `litellm[proxy]` overlay;
   evaluation runs through the SWE-bench checkout). The shared env
   already carries `litellm[proxy]`; never drop that dependency — without
   it the first model call dies with `ModuleNotFoundError: No module
   named 'fastapi'`.
2. Integrations never carry their own uv environment — see the overview:
   one env at the bundle root, integrations are workspace members, never
   run `uv` inside `integration/<name>/`.
3. Run exactly the listed instances, one at a time: anchored `^id$` filter,
   `--workers 1`; never a broad regex.
4. Fresh run root per arm. With `scope=run` the memory store is shared
   across instances in the run root (CURE: run-root SQLite; mem0: the
   per-run user id) — a dirty root contaminates the arm.
5. Never reuse a `.proxy_env_role*` from a dead proxy; SIGINT (not SIGKILL)
   the proxy so it finalizes run dirs.
6. Local evaluation always means the SWE-bench Docker harness
   (`run-evaluation.sh`), never `sb-cli`.
7. Don't evaluate until merge passes — a missing/empty patch makes the
   report denominator misleading.
8. Keep `MEM0_API_KEY` (mem0 platform mode) in the environment or the
   bundle-root `.env`, never on the command line. The cure arm's
   `EXTRACT_*` need no user provisioning at all — the driver exports them
   per instance from the EXTRACT proxy lane (they remain backend fallbacks
   outside the driver, where the same keep-them-off-the-command-line rule
   applies).

# Relationship to the original workspace

This bundle holds copies of the used files only. The surrounding
workspace's originals are archived at `../.bak`:
`../.bak/cure_memory_bridge_v2` (the pre-split bridge), `../.bak/utils`
(including the older `run-v2-memory.sh` / `run-ab-arms.sh` drivers,
superseded here by `utils/run-memory-arm.sh`),
`../.bak/CURE_memory_system` (full checkout: docs, demos, benchmarks,
git history — the bundle's copy lives at
`integration/cure_memory/src/cure_memory`), and `../.bak/.env` (the
roster `.env` this bundle's own was copied from). Still live one level
up: `../SWE-bench` and `../extension/traj-recorder` (both resolved
bundle-first by the scripts). The bundle's CURE copy
(`integration/cure_memory/src/cure_memory`) is the hardened source of truth
— it removed the upstream credential-leak defaults (the hardcoded
third-party endpoint fallback and the `$OPENAI_API_KEY` env fallback in
`extractor.py`) and carries APIs the bridge requires
(`system.py:has_unextracted_messages`). The archived checkout still has
the old code, so NEVER bulk-refresh the copy from it (a blanket
`rsync ../.bak/CURE_memory_system/cure_memory ...` would silently restore
the leak paths and trip the bridge's startup guard); port individual
changes selectively instead.

Deeper docs: `integration/cure_memory/AGENTS.md` (memory-arm internals,
schema, annotation protocol), `integration/mem0/AGENTS.md` (mem0 arm),
`integration/tencentdb/AGENTS.md` + `integration/tencentdb/VENDORING.md`
(tencentdb arm, vendored upstream clone),
`utils/README.md` (what each pipeline script does), and
`../extension/traj-recorder/AGENTS.md` (proxy).

# Commit messages

Use the following format for commit messages:

- `ci: description` for all testing related changes, and changes to github workflows etc.
- `dev: description` for development related changes, including updates to the cursor or claude rules
- `fix(component): description` for bug fixes
- `feat(component): description` for new features
- `enh(component): description` for enhancements
- `docs: description` for documentation
- `ref(component): description` for refactoring
- `chore: description` for maintenance tasks (pre-commit hooks, imports, etc.)

Generally, the description should focus on the intent of the changes, not the implementation details. The component in parentheses names the touched area — `shared-bridge`, `cure_memory`, `mem0`, `utils` — and is omitted for workspace-level changes (the shared env, the instance lists, this file).

## Style notes

Do **NOT** add "Co-authored-by: Cursor" lines to the commit message or to the trailer.

# Rules

- Do not preserve backward compatibility. Remove obsolete paths instead of adding compatibility layers, fallbacks, or migrations.
- Choose the simplest implementation that fully meets the current requirements. Avoid speculative abstractions, configuration, and indirection.
- Grow the system in layers. Start from the smallest version that works end to end, and add each new capability on top of a product that already works. Never trade a working product for unfinished complexity.
- Keep components modular and concerns clearly separated.
- Prefer established, well-maintained libraries when they reduce overall complexity or improve reliability. Do not reimplement common functionality without a clear reason.
- Lean on the dependencies already in the project before writing your own implementation or adding packages. Do not assume a library lacks a capability without checking its documentation and types.
- Make architectural decisions for the long term. Do not accept a stopgap that only works for now and is meant to be replaced later.

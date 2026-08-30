# mem0 integration — working guide

Short-form companion to `README.md` (architecture, config reference) and to
the bundle root `AGENTS.md` (pipeline, rules). This file is the what-to-run
and what-to-check sheet.

## Layout

- `src/mem0_bridge/` — the bridge package: `config`, `client` (the platform
  REST client), `stores/` (the `Mem0Store` protocol plus one store per
  mode — `platform`, `server`, `library` — consumed by BOTH the backend and
  the endpoint), `backend`, `agent`, `endpoint`, `run/swebench`.
- `configs/memory_defaults.yaml` — partial `agent.memory.*` overlay; pass
  AFTER `swebench.yaml`. Carries the mode selector (see "Modes").
- `vendor/mem0/` — gitignored vendored clone of the OSS repo (routes pin +
  server-mode Docker build context), never committed and never imported —
  see `VENDORING.md`.
- `tests/` — offline suite; runs green under
  `uv run python -m pytest integration/mem0/tests -q` from the bundle root
  (also together with the shared-bridge and cure suites in one invocation).

The memory-arm driver is bundle-level: `utils/run-memory-arm.sh mem0`
(predictions → merge → local Docker evaluation → summary). This integration
is a uv workspace member of the bundle root — it has NO uv environment of its
own; `uv sync` at `memory-bridge/` installs it into the one shared env.

## Modes

Exactly one anchored, comment-free line in `configs/memory_defaults.yaml`
selects the deployment the bridge talks to:

```text
    mode: platform | server | library
```

The mode is yaml-owned: the driver reads THIS line (`read_mem0_mode` in
`utils/run-memory-arm.sh`) and dies loudly on no/multiple matches, and
`--config agent.memory.mode=` extras are refused — driver and bridge can
never diverge. Per mode:

- `platform` (default) — the hosted API (`https://api.mem0.ai`); extraction
  is hosted. Requires `MEM0_API_KEY` in the bundle-root `.env`. Adds are
  async with event polling — `poll_budget`/`poll_interval` are
  platform-only (server/library adds are synchronous).
- `server` — a per-run self-hosted OSS server stack the driver manages: TWO
  containers on one bridge network (`pgvector/pgvector:0.8.6-pg17`, no published
  port, plus the API server built at runtime from `vendor/mem0/` with the
  engine pinned `mem0ai==2.0.19`), published at `127.0.0.1:8890` — a
  machine-wide single-arm claim (`${TMPDIR:-/tmp}/mem0-arm-claim`) makes a
  concurrent server arm die loudly. Requires Docker running and the full
  `EMBEDDING_*` quartet in the bundle-root `.env` (fail-closed: the OSS
  engine embeds on every add and every search, no lexical fallback). The
  store lives on run-root volumes under `<run-root>/mem0-server/` (pg data +
  history); containers and the network are removed on exit, and a resume
  recreates the stack over the same volumes. Note the OSS server persists
  the `/configure` payload — roster + embedding API keys included — into the
  pg volume, so those keys sit at rest under the (gitignored) run root for
  the volume's lifetime; they never reach argv or logs.
- `library` — the in-process `mem0ai` engine (`from mem0 import Memory`),
  which enters the shared env ONLY via the opt-in root dependency group
  `mem0-library`: every library-mode instance invocation carries
  `uv run --group mem0-library` (a plain `uv run` never installs it, a plain
  `uv sync` evicts it). Requires the `EMBEDDING_*` quartet (same fail-closed
  gate) plus the roster `MODEL`/`API_KEY`/`BASE_URL`; the store lives under
  `<run-root>/mem0/` (qdrant dir + history.db) via the
  `agent.memory.run_root` field the driver passes.

Extraction traffic is NEVER recorded through the trajectory proxy in any
mode (hosted by the platform, inside the server container, or in-process
against the provider upstream) — the MEMORY lane stays a zero-model-call
annotate namespace in all three.

## Run the memory arm (2-instance smoke)

```bash
cd memory-bridge
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt mem0first2
./utils/run-memory-arm.sh mem0
tail -f "$(cat output/LATEST | sed s|^|output/|)/memory-arm.log"
```

Everything resolves from the bundle: shared uv env, `mini-swe-agent/`,
`SWE-bench/` (via the bundle utils fallbacks), and the provider roster
`.env` — which also carries `MEM0_API_KEY` (platform mode) and the
`EMBEDDING_*` quartet (server/library modes).

## Checkpoints

1. Phase 1: one valid `preds.json` per instance under
   `runs/mini-swe-agent/<id>/`, plus `memory.json` and `agent.log`.
2. memory.json: `enabled: true`, `available: true`, `extraction_errors: 0`,
   `recall_injections > 0` (instance 1 after its first extraction; instance 2
   from its first model call — `scope=run` shares the user id across the run
   root). With the dirty-flag cache, `counts.search_calls` stays low (the cold
   start plus one search per extraction/rewrite boundary); most injections are
   cache-served (`recall_cache_hits`). Settings record `mode` and
   `bridge_version`; the start event carries `mode`.
3. The traj.json `info.memory` block carries the same counters per step.
4. Phases 2–4: `merged-preds.json`, the Docker evaluation report, and the
   resolved/unresolved/error summary (read `summarize-report.sh` output —
   unresolved means a model miss, errors mean harness trouble).

## Rules that prevent known failures

1. Run through the shared env (`uv run --project <bundle-root>`): this
   integration is a workspace member installed by `uv sync`, so plain
   `python -m mem0_bridge...` resolves. Never run `uv` inside this directory
   and never `pip install mem0ai` into the shared env — the SDK enters it
   ONLY through the opt-in `mem0-library` group (library mode), and a plain
   `uv sync` removes it again (litellm conflict posture).
2. One anchored instance at a time, `--workers 1`.
3. Fresh run root per arm: the driver mints the mem0 user id from the
   timestamped run-root name in every mode (server/library add fresh per-run
   stores on top), and refuses stale attempts (agent.log without valid
   preds) — rerunning those would recall the aborted attempt's memories.
4. Keep `MEM0_API_KEY` (platform mode) in the environment or the bundle-root
   `.env`, never on the command line.
5. Platform adds are async: the client polls the event; `poll_budget` is the
   total budget (default 120 s in the overlay). Server/library adds are
   synchronous (no polling). A timed-out batch is retained
   and retried at the next boundary — do not "fix" this by clearing the
   buffer on failure.
6. Pass only `user_id` (never `agent_id`) as the entity id on add — see
   README "Scoping".
7. Never edit, commit, or import the vendored clone under `vendor/mem0/` —
   it is the routes reference and the server-mode build context, nothing
   more (`VENDORING.md`).
8. The mode is yaml-owned: flip it only in `configs/memory_defaults.yaml`,
   never via `--config agent.memory.mode=` (the driver refuses the extra).

## Mode facts

Platform surface (verified against the v3 API reference + recorded runs):

- The client's add/search/get-all ride **v3** (`/v3/memories/add|search|/`);
  v1 remains only for single-memory CRUD, `/v1/ping/`, and the `/v1/event/`
  poll the add flow uses.
- v3 search rows carry `score` (the combined multi-signal score, 0–1) and
  echo the add-time `run_id` — the provenance signal `_hit_origin` reads.
  v3 get-all rows omit `run_id` (they carry the same value under
  `session_id`); the OSS surfaces promote `run_id` directly on BOTH search
  and get-all, so `_final_dump` reads both fields.
- v3 search documents `threshold` with a server-side default of **0.1**
  (the v2 spec documented 0.3): an omitted threshold is a silent relevance
  cutoff whose value drifts across versions, so the threshold is always sent
  explicitly. Its MEANING is per surface: platform 0.0 disables the cutoff;
  OSS (server/library) 0.0 is a minimal gate on the raw semantic score
  before the hybrid combine — a floor, not a switch. Both retrieval surfaces
  (the arm's `_search` and the endpoint adapter) share the mode's one call;
  the single relevance door is the shared host-side `recall_min_score` floor
  (the shipped yaml keeps `search_threshold: 0.0`).
- v3 add is ADD-only on this surface ("single-pass ADD-only extraction: one
  LLM call, no UPDATE/DELETE" — the add page's prose): the arm's receipts
  show `memories_updated: 0` / `memories_deleted: 0` on every run. The
  client's UPDATE/DELETE receipt branches stay for the other surfaces it
  serves.

Generation audit (all modes, traced runs only): the backend snapshots the
store's full user scope (`get_all` — the final dump's ceiling, except server
mode, whose one clamped page caps the walk at `SERVER_LISTING_CAP`) before and
after every extraction tick. A full page means the scope may not have
exhausted, and an incomplete snapshot can verify neither presence nor absence:
the audit degrades to `unknown` evidence for that generation rather than
accuse honest receipts of drift (the false-drift class a >1000-memory server
run root would otherwise post every tick). A completed generation's receipts
are
cross-checked against the observed before/after diff (a disagreement — the
silent-insert class, or a mutation no receipt claims — downgrades the end's
`state_evidence` to `partial` with the drift lines in the audit), and a
failed add that persisted server-side anyway is reconciled from the diff as
partial `observed_diff` changes under a `partial` generation end, instead of
the change-less `failed`/`unknown` that used to deny the landed rows. The
snapshot costs two user-scope listings per traced tick (platform: one
paginated walk each) and degrades to `unknown` evidence on any listing
error — never a raise into the native path.

OSS surfaces (server mode over HTTP, library mode in-process — wire claims
verified against the vendored tree, pin in `VENDORING.md`):

- The server's routes carry NO `/v1` prefix and run with
  `redirect_slashes=False` — a trailing slash is a 404. Adds are SYNCHRONOUS
  (`POST /memories` answers `{"results": [...]}`, no event polling); scoped
  get-all is `GET /memories?user_id=...&top_k=N` (query params, capped
  server-side at 1000); `GET /memories/{id}` answers 200 `null` for unknown
  ids — the server store maps the null to the protocol's 404 convention.
  Readiness is `GET /auth/setup-status` (the API server has no `/health`).
- OSS result rows promote `run_id` from the payload (source-verified:
  `promoted_payload_keys` in `mem0/memory/main.py`) on search AND get-all,
  so `_hit_origin` and `_final_dump` read `run_id` directly on OSS surfaces.
- The silent-insert class: a pgvector dims mismatch makes adds return ADD
  receipts while persisting NOTHING. Two driver guards cover it: the driver
  precreates the `memories` table at `vector($EMBEDDING_DIMENSIONS)` BEFORE
  the server boots (the server's DEFAULT_CONFIG has no dims channel, so its
  eager create_col would birth the collection at the 1536 default), and a
  fail-fatal canary (one verbatim add + search + delete under a scratch
  user id) runs before the arm.
- The engine's 2000-token default `max_tokens` starves reasoning-hybrid
  roster models — thinking burns the budget (finish_reason=length with 0
  output chars) and extraction stores NOTHING while answering fine (the trap
  the tencentdb gateway yaml documents). The driver POSTs `/configure` with
  `max_tokens: 32000` after boot (fail-fatal); the library store's config
  carries the same 32000.
- The driver raises `search_timeout` to 30 for server arms: one
  bridge→server HTTP call hides the embedder round-trips plus the hybrid
  CPU work, so a slow embedding upstream would otherwise surface as
  `search_errors` at the shared 10 s default. Library mode ignores
  `search_timeout` by design (in-process; the shared stance bounds network
  calls only).
- The host-side `recall_min_score` floor is calibrated on the platform's
  combined 0-1 score (the shipped yaml's 0.1); the OSS hybrid score is a
  different scale and scores are never compared across modes, so the driver
  passes `agent.memory.recall_min_score=null` for server/library arms —
  the floor stays off there until a calibration pair picks a per-mode value.
- Reranker lane (reserved, not wired): the OSS engine takes an optional
  search reranker (`reranker: {provider, config}` in the config dict,
  opt-in per search with `rerank: true`; a failed rerank logs a warning and
  falls back to the pre-rerank ranking — never fail-closed), while the
  hosted platform's v3 search has no rerank channel, so the lane is
  OSS-only. `.env` space is already reserved: `load_model_env` sources the
  roster wholesale, so future `RERANKER_*` keys flow with no structural
  change — when the lane lands, give them the `EMBEDDING_*` treatment
  (the driver's stale-export unset line, mode-gated bridge validation, and
  the mapping into the library config dict / the server `/configure`
  payload).

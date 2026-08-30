# tencentdb integration (TencentDB-Agent-Memory / MemoryCore)

- Package `tencentdb_bridge` binds the standalone **MemoryCore** gateway of
  [TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)
  to the shared bridge. Only MemoryCore is used (Docker, standalone mode,
  SQLite + FTS5 — zero external services besides an OpenAI-compatible
  extraction LLM); MemoryProxy / MemoryPanel / MemoryKnowledge are not used.
  The upstream tree is a gitignored vendored clone under
  `src/TencentDB-Agent-Memory/` (see VENDORING.md — never committed, never
  imported; the bridge talks REST over httpx, house style, no vendored SDK).
- **Deployment**: one container per run root, driver-managed
  (`utils/run-memory-arm.sh tencentdb` writes `<run-root>/tdai/tdai-gateway.yaml`
  — credential-free; `${TDAI_*}` yaml leaves are interpolated by the loader
  from `docker run -e` env — then runs `agentmemory/memory-core:1.0.1-beta.1`
  publishing `127.0.0.1:8420`, data volume `<run-root>/tdai/data`). Port 8420
  is a per-machine single-arm lock: two tencentdb run roots cannot run
  concurrently — enforced at the process level by the machine-wide arm claim
  (`${TMPDIR:-/tmp}/memory-bridge-arm-claim`, a directory lock under the per-user
  temp dir shared by EVERY integration's arm — they all regenerate the one
  recorder `.env` per run, and Docker Desktop's daemon is per-user, so one lock per user
  covers every checkout of this bundle on the machine; acquisition is the
  atomic `mkdir(2)` of the claim dir with the driver pid inside, so two
  drivers started together can never both hold it; a live holder fails the
  new arm loudly, a dead holder's claim is stale and taken over — liveness
  is EPERM-safe: `ps -p` sees a foreign live pid that `kill -0` misreads as
  dead when the claim dir is machine-wide (TMPDIR unset on Linux)). Teardown
  is a plain `docker rm -f` (data lives on the host
  volume); the EXIT trap is name-scoped to the run root's own container, and
  the next arm's pre-start sweep removes any leaked `tdai-*` container (a
  SIGKILL'd driver's trap never fires — the leak would keep 8420 bound and
  fail every later arm at `docker run`; the claim proves the sweep can never
  hit a live arm's container). The container's env rides bare `docker run -e
  NAME` forwards (never value-carrying pairs — a key on the argv would be
  ps-visible). The embedding lane is env-driven: all four of
  `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` /
  `EMBEDDING_DIMENSIONS` in the roster `.env` or none — a partial set (or a
  non-numeric DIMENSIONS) is *silently disabled* upstream, so the driver
  dies loudly on it. The quartet rides exported `TDAI_EMBEDDING_*` names
  (the yaml leaves' references — bare `docker run -e NAME` forwards), and
  the readiness loop asserts `"embeddingService":true` in the `/health`
  body when the lane is enabled: upstream disables a misconfigured remote
  provider without throwing (a stored `configError` nothing logs, HTTP
  status still 200), so only the body check catches the quartet failing to
  reach the container.
- **Extraction** runs inside the container against the provider upstream
  directly (the LLM section of the generated yaml). Extraction traffic is NOT
  recorded in the trajectory (same treatment as mem0's hosted extraction);
  memory protocol events reach the trajectory via the annotate MEMORY lane
  from API receipts. The pipeline is threshold-batched: warmup 1→2→4→then
  every 5 user-rounds; each below-threshold add arms a 30 s L1 idle timer
  (`l1IdleTimeoutSeconds: 30`, NOT the 600 s upstream default — pinned so the
  episode tail lands inside the finalize drain). The backend resolves the
  effective idle timeout exactly once per start by reading it BACK from the
  generated yaml (`<run-root>/tdai/tdai-gateway.yaml`) — the single source of
  truth; there is no host-side copy (a missing file, a missing/blank key, or
  a non-numeric value fails the start loudly), and the settings artifact
  records the resolved value with `l1_idle_timeout_source: "gateway-yaml"`.
- **Wire contract** (verified against the vendored router): the data plane is
  uniformly `/v3` (count endpoints are `/v3`-only) with exactly one `/v2`
  exception — `POST /v2/pipeline/status` (standalone-only, the L1 drain
  poll). Every request carries `Authorization: Bearer <non-empty>` +
  `x-tdai-service-id: default` (enforced by `parseV2Auth` even with gateway
  auth off; the value names the pipeline instance bucket — any other value
  splits v1/v2 pipeline state). The data plane is POST-only. Responses ride
  the envelope `{code, message, request_id, data}`; envelope codes in
  [400,600) mirror into the HTTP status, codes outside it (e.g. the 4291
  quota code) ship with HTTP 200 — the client keys on the envelope code.
  `atomic/search` caps `query` at 2048 chars (the client truncates; the
  recall query is the full task text). `scenario/read` and `core/read`
  answer 200 with null fields when nothing exists.
- **Isolation quadruple**: `team_id="minisweagent"`,
  `agent_id="memory-bridge"`, `user_id` minted from the run-root name
  (`minisweagent-tdai-<runroot>`, mem0's pattern), `task_id` = the episode's
  repo key (`shared_bridge.backend._repo_of` — cure's lattice shape): L1
  becomes the repo tier (`atomic/search` is cross-session by design but
  task-filtered), L2/L3 profiles accumulate at team+agent level (the general
  tier, upstream's own design).
- **Extraction tick** (`_perform_extraction`): chunked `conversation/add`
  (≤100 messages/call, roles folded `system`/`tool`→`user` — the zod enum is
  user/assistant only, and the pipeline counts user-rounds; every message
  carries a host-stamped `recorded_at`, the upstream schema's optional
  field, honored over the container's receive time — so the raw-message
  timestamps the agent's conversation search renders live in the host
  clock domain, consistent across episodes and immune to host-vs-container
  clock skew (Docker Desktop VM drift after sleep/wake); episode window
  starts are floored to the gateway's millisecond storage precision
  (`toISOString()`) — kept as one cheap line: the boundary now serves L1
  origin attribution alone, where a ≤ 1 ms start-edge widening is a no-op;
  recorded content also clamps at the wire's 8192-char cap in the cap's own
  unit (UTF-16 code units — zod counts JavaScript's String.length, so a
  code-point clamp could still bust it on astral-heavy text) so a
  `max_message_chars` raised above it never draws a gateway 400) → poll
  `l1.idle` within `drain_budget` (600 s in the arm overlay — raised over
  the 180 s design default because one tick can chain several L1 cycles:
  one cycle consumes at most 10 L0 rows (the 2N=20 over-fetch is backlog
  detection), and with the vector lane
  degraded one cycle was observed at ~185 s, so a 300 s budget lost a
  3-cycle chain by seconds; the wait costs nothing on the happy path.
  Never reuse `search_timeout` — one L1 extraction-LLM cycle routinely
  exceeds 10 s) → resolve produced ids
  via the watermark query (`atomic/query time_start=<watermark>`, matches
  `updated_time`, paginated limit 100 — the default 20 silently truncates).
  Row `version` 0 = create (the store zeroes fresh L1 rows —
  `writeMemory` leaves nextVersion 0 for `store` and the SQLite DDL
  defaults to 0), ≥1 = a rewrite.
  Buffer semantics: the pending buffer clears as soon as the add returns
  (L0 persisted server-side) — a re-add after a drain failure would re-feed
  the pipeline wholesale and chain L1 tasks past every drain budget. Only a
  failed ADD retains the buffer (uncertain outcome, mem0-style retry) — and
  a mid-chunk failure retains only the unconfirmed tail: the error carries
  `persisted_messages` (earlier chunks' responses came back, so their
  messages are confirmed server-side and dropped from the retry). The
  production window opens at the episode's first add and never narrows —
  the exactly-once mechanism is a per-episode (id, version) dedup set, not
  watermark consumption: a below-threshold add arms the L1 idle timer
  (invisible to the status API), so a resolve can succeed with rows still
  landing up to the L1 idle timeout later; the open window plus dedup counts
  them at whichever later resolve first sees them. The **finalize
  drain** is idle-timer-aware: poll within `finalize_drain_budget` → wait
  the yaml-resolved `l1IdleTimeoutSeconds` + margin unconditionally (the
  wait is the only way to
  know an armed timer has fired before the second poll; it ignores the
  drain deadline — the tail landing inside the episode wins over a hard
  wall at its end) → drain the tail with a FRESH per-tick `drain_budget`
  (never the deadline remainder: the sleep may have consumed it, and the
  timer-fired tail task needs a full L1 cycle), with
  `finalize_drain_budget` (600 s in the arm overlay — a chained finalize
  can need two serial L1 cycles plus the idle wait, and each cycle carries
  the vector lane's embed costs) bounding the first drain. A drain record
  closes the episode's attribution window even when the drain fails — and
  also when the episode recorded nothing at all (the readiness guard's
  no-op final still records it) or when the final tick's send/resolve
  fails before the drain runs (the except path records it;
  `_record_drain` is idempotent, so the paths never double-record).
- **Recall surface**: L1 scored hits (repo-scoped) + the L3 persona as a
  **prepended score-less pseudo-hit** + the L2 scene index as a header
  section. L0 raw conversation search is agent-initiated (see the
  agent-initiated-reads bullet), never injected. The L1 fetch is one
  `atomic/search` (cross-session by design, `task_id` repo tier) with a
  small overfetch under the wire cap (the base's floor/slice work below the
  fetch). The base owns line truncation inside rank-then-fill:
  `max_chars_per_memory` first (off by default), then truncate-to-fit
  against `max_total_recall_chars` at the 40-char floor — an over-budget L1
  fact delivers truncated (or is skipped when less than the floor remains)
  while the walk continues, so a top-ranked memory is never silently
  dropped. The persona pseudo-hit
  (`core/read`, navigation tail stripped at the exact upstream marker and
  parsed host-side for the L2 index's heat ordering — below;
  not-yet-generated persona answers 200 nulls and renders nothing) is
  prepended — load-bearing, the base slices
  `hits[:max_memories]` in list order — and is the arm's one budget-EXEMPT
  layer (`_hit_budget_exempt`): it renders unbounded in full (native
  parity — applyRecallBudget governs the memory lines only) and consumes no
  `max_total_recall_chars`, so a profile grown across episodes can no
  longer crowd out a single L1 line; it still occupies one `max_memories`
  slot (a recorded residual divergence). The L2 scene index rides
  the header (`scenario/ls`; `summary` is optional upstream) carrying a
  self-contained curl guide (`host.docker.internal:8420` — the agent's bash
  runs inside the prediction container where 127.0.0.1 is its own loopback),
  ordered heat-descending from the persona's scene-navigation tail (parsed
  host-side off the raw `core/read` content, independent of the persona
  pseudo-hit's presence): `scenario/ls` remains the existence source — a nav
  entry absent from ls never renders — nav-less entries (index lag, pre-L3
  persona) trail in ls order, and each line renders the scene's heat and
  update stamp with no local caps (every ls file, full summaries — bounded
  only by upstream's prompt-level `maxScenes` merge discipline, native
  parity).
  With L1 and persona both empty the base injects nothing at all, so the L2
  index and the conversation-search guide reach the model only once some
  hit exists. `recall_min_score` stays None: L1's native score is not one
  scale across retrieval strategies (a healthy hybrid lane yields tiny RRF
  ranks, an FTS-only lane a normalized bm25 in (0,1), a vector-only lane
  cosine similarity — lane availability is per-query and the response
  carries no strategy field), so a configured floor draws one start-time
  warning; never inherit mem0's 0.1 floor.
- **Agent-initiated reads (L2 scenes, L0 conversation search)** are
  observed, not mediated: the backend's `record()` override arms one
  pending read per assistant action containing `/v3/scenario/read` with a
  parseable `path` pair, or `/v3/conversation/search` with a parseable
  `query` pair (two separate pending maps, each keyed by `tool_call_id`, so
  a multi-action turn can arm several; a marker-mentioning command without
  the pair — e.g. grep over the guide text — never arms) and closes each on
  the matching tool observation (id-matching, not next-message — a sibling
  action's observation must not close). Closings bump `agent_scene_reads` /
  `scene_read_chars` resp. `agent_conversation_searches` /
  `conversation_search_chars` (memory.json counters + `scene_read` /
  `conversation_search` events; the schema-v6 protocol has no type for
  either). The conversation-search guide is unconditional within the recall
  header (the scene-read guide stays scene-gated) and bakes in the
  `task_id` repo tier; its `curl | jq | tee -a` pipeline renders the
  response near-verbatim to the native openclaw plugin's tool output AND
  appends every search to the episode-local `/tmp/tdai-l0-searches.md`, so
  later steps re-read it instead of re-searching. A read costs the agent
  one step (real cost of the official UX, honestly recorded) and depends on
  model compliance (part of what is measured): read
  `agent_conversation_searches` against `recall_injections` (the guide only
  reaches the model on an injecting recall), never against episode count.
- **Origin attribution**: L1 hits carry no session_id — `created_at` maps
  onto per-episode windows [start, drain] persisted in
  `<run-root>/tdai/episodes.jsonl` (loaded at start, appended by each
  episode's backend). The one cross-clock comparison left is L1
  `created_at` (stamped container-side at extraction): a host-vs-container
  skew larger than an extraction's distance from the episode start could
  misattribute a boundary row's suffix — seconds-scale and suffix-only
  (never context pollution), accepted. Unresolvable hits
  return the `"unknown"` sentinel: a
  non-null origin, so `summarize_memory.py` counts it as cross-episode (its
  unknown bucket is None-only) — the intended reading is "not this
  episode". **Merge bias**: `created_at` = `timestamp_start` = the min of
  the merged timestamp union, so after a dedup merge a hit attributes to the
  *oldest* contributing episode (the watermark path, keyed on
  `updated_time`, points at the newest — the two attribution paths disagree
  by upstream design); cross-episode recall share is biased toward earlier
  episodes.
- **Endpoint adapter** (`tencentdb_bridge.endpoint`): `add` with
  `infer=false` answers 400 (there is no verbatim insert — no `atomic/add`
  route exists; `conversation/add` always feeds extraction), and an add
  carrying `metadata` answers 400 too (the conversation schema has no
  metadata field — a silent drop would claim a write not fully made); a fresh
  session id per add (the warmup threshold starts at 1, so every add
  triggers its L1 task) — the one deliberate deviation from the contract's
  byte-for-byte session echo: the response carries the minted session id,
  because reusing a caller's session could fall sub-threshold and leave the
  add's memories unsearchable behind an armed idle timer (the immediately-
  searchable rule wins); `memory_ids` may legitimately be empty. Two more
  400s come from upstream pipeline facts: roles outside the schema's
  user/assistant enum (a caller bug, never relayed as a 500) and an add
  with no user round (the gateway notifies only on user rounds and the
  per-add session is unique, so the write could NEVER become searchable).
  A third pre-validation of the same class covers the wire's 8192-char
  content cap (zod, shared by add messages and update text): a
  contract-legal request the gateway must reject is answered 400 before
  any write, never relayed as a 500.
  The add's client timeout is its own `add_timeout` (mirroring the config
  field), not the drain budget; each drain wait's budget (300 s default)
  dominates one full L1 cycle (the extraction LLM call caps at a hardcoded
  180 s upstream — l1-extractor.ts, independent of the yaml's
  `llm.timeoutMs` — plus the vector lane's per-memory embeds), and the
  FIRST wait scales that per-cycle budget by the chained full-cycle count
  (the status stays non-idle while full cycles chain), so a multi-cycle add
  under a slow extraction lane doesn't 500 a write that already persisted.
  One add is NOT one cycle: upstream consumes at most 10 L0 rows per cycle
  (L1_BATCH_PROCESS; the 2N=20 over-fetch is backlog detection), and a
  cycle ending with a 1-9-row tail defers it to the L1 idle timer, which
  /v2/pipeline/status never exposes — so an add over 10 messages drains
  like the arm's finalize (wait, wait out the gateway's
  `l1IdleTimeoutSeconds` + margin, wait
  again) before resolving, or `memory_ids` would miss the tail and success
  would precede searchability. `update`
  is 1:1 by id (`{id, content, background?}`), any metadata-bearing update
  answers 400 (L1 carries no metadata — a text+metadata update applied
  partially would silently drop the metadata half), native 403/404 both map
  to the contract's 404
  (isolation must look like absence). `delete` wraps a single-element
  `ids[]` batch; `deleted_count == 0` maps to 404. The endpoint stays
  user-wide for retrieval (search sends no `task_id` — the arm's repo
  narrowing is arm-internal), and search is the L1 atomic layer alone: the
  contract's records are the rows `add` returns and `update`/`delete`
  address by id, while the arm's L3 persona / L2 scene index are
  episode-bound recall augmentations with no CRUD identity (updating the
  persona pseudo-id would 404). `add` tags its write with the
  minted session
  id as `task_id` purely so its resolve query returns exactly this add's
  rows (rows carry no session id on the wire; an untagged task filter never
  matches, so the tag changes no retrieval surface).
- **Extraction guidelines are not conveyed**: the stored
  `/v3/memory-prompt/*` channel is scope-stored (not per-request) so it
  cannot carry the per-episode context half, and async timer-fired
  extraction would race per-episode upserts. Settings record `""`; a
  configured override draws the standard start-time warning.
- **Counters**: `memories_added` / `memories_updated` (watermark rows'
  `version` split — `summarize_memory.py` already reads these keys),
  `agent_scene_reads` / `scene_read_chars` and
  `agent_conversation_searches` / `conversation_search_chars` (the two
  agent-initiated-read observations; all summarizer columns).
  `memories_deleted` stays un-counted — dedup's superseded-id
  deletions are invisible to the watermark query — the column prints "-".
- **Tests** (offline, no Docker): `uv run python -m pytest
  integration/tencentdb/tests -q` from the bundle root, or the full
  one-invocation suite per the root AGENTS.md. The tests dir is a
  `tencentdb.tests` package (unique name under pytest prepend import mode —
  its modules must not collide with the sibling suites' plain module
  names). Wire tests ride `httpx.MockTransport`; backend/endpoint/agent
  tests ride the scripted `FakeGatewayClient` injected through the
  `_make_client` seam; trace tests ride the shared `CaptureServer`.
- **Smoke run**: `head -2 instance-ids.txt > /tmp/ids.txt && ./utils/setup-run.sh
  /tmp/ids.txt <name> && ./utils/run-memory-arm.sh tencentdb`. memory.json
  checkpoints mirror mem0's (`enabled/available: true`,
  `extraction_errors: 0`, `recall_injections > 0` on the second same-repo
  instance from its first model call). Verify the generated yaml at
  `<run-root>/tdai/tdai-gateway.yaml`, the episode sidecar at
  `<run-root>/tdai/episodes.jsonl`, and `docker logs tdai-<runroot>` for
  the pipeline's L1/L2/L3 spans.
- **Known compromises** (recorded, accepted): the 30 s idle timer makes
  sub-threshold adds extract ~30 s after the last add (smaller, more
  frequent extractions than the 600 s default) and every finalize pays up
  to one idle timeout (~30 s; a chained finalize up to the
  `finalize_drain_budget` first drain plus a fresh per-tick
  `drain_budget` for the tail — 600 s + 600 s in the arm overlay). The
  role fold yields ~1 user round per recorded step, and each tick's
  ~25 messages chain 2-3 serial L1 cycles server-side (one L1 cycle
  consumes at most 10 L0 rows), so every extract tick blocks the
  agent's step for those cycles (extraction-LLM latency; the wait is
  `_io_duration`-exempt and `wall_time_limit_seconds: 0` means no verdict
  impact, but the wall-clock is real — tens of minutes over a full episode
  at `extract_every_n_steps: 10`). L1 extraction LLM calls bypass the
  trajectory (budget attribution invisible by design). The L1 repo tier is
  per-episode, not per-memory: a genuinely general L1 lesson never crosses
  repos (cross-scope transfer rides L2/L3's team+agent accumulation).
  The resolve watermark is host-clock (`utc_now_iso` minus the 5 s
  `WATERMARK_SKEW_SECONDS`) while L1 rows are stamped container-side, and the
  pinned gateway exposes no server-clock surface to reconcile against
  (`/health`, `/v2/pipeline/status`, and the add response carry none): a
  container clock more than ~5 s behind the host (Docker Desktop VM drift
  after a mid-arm sleep/wake) strands the affected episode's first rows from
  the watermark query — `memories_added`/`memories_updated` and the
  annotation's produced refs undercount while recall itself is unaffected.
  The tell-tale is the final dump (no time filter) holding more rows than
  the counters sum to.
  A failed `core/read` keeps the L2 index's last-known heat ordering for one
  cycle: the nav stash is derived ordering metadata, deliberately not dropped
  with the persona line — `scenario/ls` keeps existence and paths fresh on
  the same failed cycle, so stale heats misorder entries at worst, never
  invent or hide one.
  **Embedding timeouts (verified in source + run)**: the standalone store
  pool constructs the embedding service through an explicit field
  whitelist that drops `memory.embedding.timeoutMs` (`store-pool.ts`
  createSqliteStore) — every embed call caps at the 10 s
  `DEFAULT_API_TIMEOUT_MS` no matter what the yaml pins. A slow provider
  response (SiliconFlow Qwen3-Embedding-8B is bimodal: ~1 s warm, >10 s
  under load) then degrades that row to BM25-only, and the per-message
  sequential L0 embeds (whose recall consumer is the agent's
  `/v3/conversation/search`) can stretch one `conversation/add` by ~10 s
  per timed-out message. The driver's yaml still pins
  `embedding.timeoutMs` (it reaches the factory-constructed service); the
  pool-path drop is an upstream bug, and the arm never depends on the lane
  (decision 3): vector hits land whenever the provider answers inside
  10 s, verified end to end (hybrid L1-search HIT via cosine distance on
  the second episode).

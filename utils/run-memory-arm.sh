#!/usr/bin/env bash
# run-memory-arm.sh — memory arm end to end for one memory integration:
# predictions with memory ON (scope=run), then merge -> local Docker
# evaluation -> summary.
#
# Usage: run-memory-arm.sh <integration> [RUN_ROOT]
#   integration  integration/<name> to run: cure_memory | mem0 | tencentdb
#   RUN_ROOT     run root holding instance-ids.txt (default: $RUN_ROOT, else
#                the run root recorded in output/LATEST by setup-run.sh)
#
# All integrations run one roster traj-recorder proxy per instance:
# ROLE1=MAIN carries the benchmark model; ROLE2 is the memory-annotate
# namespace. For cure_memory ROLE2=EXTRACT also carries the CURE decision
# LLM's traffic; for mem0 ROLE2=MEMORY makes zero model calls in every mode
# (extraction runs off-trajectory: hosted by the platform, inside the per-run
# OSS server container, or in-process against the provider upstream —
# selected by `mode:` in integration/mem0/configs/memory_defaults.yaml) and
# serves only as the annotation lane; for tencentdb ROLE2=MEMORY is the same
# zero-model-call annotate lane (the MemoryCore container does the extraction
# against the provider upstream directly).
# Run isolation: cure_memory shares a SQLite store in the run root; mem0 uses
# a per-run user id minted from the timestamped run-root name in every mode
# (platform's store is hosted; server adds fresh per-run container volumes
# under <run-root>/mem0-server, library a fresh store dir <run-root>/mem0);
# tencentdb's store is one MemoryCore container per run root (fresh data
# volume under <run-root>/tdai/data, plus the same per-run user id).
#
# Everything else resolves from this bundle: mini-swe-agent, shared-bridge,
# and the integration — all installed in the shared uv env at the bundle root
# (`uv sync`). The provider .env is taken from the bundle when present, else
# from the parent workspace. Integrations never carry their own uv env.
set -u

# uv's default install location, for shells that do not carry it on PATH.
if ! command -v uv >/dev/null 2>&1 && [ -x "$HOME/.local/bin/uv" ]; then
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_repo

INTEGRATION_NAME="${1:-}"
case "$INTEGRATION_NAME" in
  cure_memory|mem0|tencentdb) ;;
  *) echo "usage: run-memory-arm.sh <cure_memory|mem0|tencentdb> [RUN_ROOT]" >&2; exit 1 ;;
esac
INTEGRATION="$REPO_ROOT/integration/$INTEGRATION_NAME"
[ -f "$INTEGRATION/pyproject.toml" ] || die "missing memory integration at $INTEGRATION"

resolve_run_root "${2:-}"
RUN_ROOT="$RUN_ROOT_RESOLVED"
require_instance_ids

# Stale embedding-lane exports must not leak into the tencentdb arm's
# all-or-none check below (the .env is the single source) — unset before the
# roster .env is sourced. The MEM0_* connection names get the same treatment
# (a stale MEM0_SERVER_URL from a dead server-mode arm must never reach a
# platform run). MEM0_API_KEY stays honored from the environment (rule 8:
# the environment OR the root .env); when both set it, the sourced .env wins.
unset EMBEDDING_MODEL EMBEDDING_API_KEY EMBEDDING_BASE_URL EMBEDDING_DIMENSIONS
unset MEM0_BASE_URL MEM0_SERVER_URL MEM0_SERVER_API_KEY MEM0_TELEMETRY

# load_model_env sources the roster .env, maps API_KEY/BASE_URL -> OPENAI_*,
# and sets MODEL_NAME (MSWEA_MODEL_NAME wins; else openai/$MODEL unless $MODEL
# already carries a provider prefix). MODEL stays the bare upstream model
# name — the recorder roster takes it verbatim.
load_model_env

# ---- Shared roster-proxy plumbing ----
require_flat_roster_keys() {
  # The recorder roster needs the flat provider keys verbatim.
  : "${MODEL:?MODEL must be set in $ENV_FILE}"
  : "${API_KEY:?API_KEY must be set in $ENV_FILE}"
  : "${BASE_URL:?BASE_URL must be set in $ENV_FILE}"
  : "${API:?API must be set in $ENV_FILE}"
}
# Every arm boots its per-instance proxy from the roster, and the QUERY_*
# defaults below expand $MODEL/$API_KEY/$API under set -u: validate first,
# or a non-roster .env dies with an unbound-variable error, not this diagnostic.
require_flat_roster_keys

# ---- mem0 mode plumbing ----
# read_mem0_mode — the mem0 integration's single source of truth for its
# deployment mode, read BY THE DRIVER from the same yaml the bridge loads
# (configs/memory_defaults.yaml), so driver and bridge can never diverge.
# Strictly anchored: the shipped yaml carries exactly one comment-free
# `    mode: <value>` line (any explanation lives on its own line above it),
# so no match or multiple matches is drift — die loudly, never default
# silently (an unanchored grep would miss `mode :`/quoted/commented variants
# and recreate the divergence this reader exists to kill).
read_mem0_mode() {
  local yaml="$INTEGRATION/configs/memory_defaults.yaml" count
  count="$(grep -cE '^    mode: (platform|server|library)$' "$yaml" || true)"
  if [ "$count" != "1" ]; then
    die "$yaml must carry exactly one anchored '    mode: platform|server|library' line (found $count) — fix the yaml; the driver refuses to guess the mem0 mode"
  fi
  grep -E '^    mode: (platform|server|library)$' "$yaml" | sed -E 's/^    mode: //'
}

# openai_v1_root URL — the OpenAI-compatible root form (${url%/} + /v1 unless
# present): mem0's openai LLM/embedder clients append /chat/completions or
# /embeddings to it, while the roster BASE_URL is the litellm form (with or
# without a trailing slash). The TDAI_LLM_BASE_URL recipe, shared by the mem0
# server boot env, the /configure payload, and the library-mode bridge config.
openai_v1_root() {
  local u="${1%/}"
  case "$u" in
    */v1) printf %s "$u" ;;
    *) printf %s "$u/v1" ;;
  esac
}

# require_mem0_embedding_quartet — server/library modes FAIL CLOSED without the
# full quartet (tencentdb's all-or-none check shape, but absence is FATAL
# here): the OSS engine embeds on every add and every search with no
# lexical-only fallback, so a DeepSeek-shaped roster (no embeddings endpoint)
# would otherwise boot healthy and die on the first add.
require_mem0_embedding_quartet() {
  local _v value missing=""
  for _v in EMBEDDING_MODEL EMBEDDING_API_KEY EMBEDDING_BASE_URL EMBEDDING_DIMENSIONS; do
    eval "value=\${$_v:-}"
    [ -z "$value" ] && missing="$missing $_v"
  done
  [ -z "$missing" ] || die "mem0 $MEM0_MODE mode needs the full EMBEDDING_* quartet in $ENV_FILE (missing:$missing) — the OSS engine has no lexical-only fallback"
  case "$EMBEDDING_DIMENSIONS" in
    *[!0-9]* | "") die "EMBEDDING_DIMENSIONS in $ENV_FILE must be a positive integer (got '$EMBEDDING_DIMENSIONS')" ;;
  esac
  if [ "$EMBEDDING_DIMENSIONS" -le 0 ]; then
    die "EMBEDDING_DIMENSIONS in $ENV_FILE must be a positive integer (got '$EMBEDDING_DIMENSIONS')"
  fi
}

# The mem0 deployment mode (platform|server|library), resolved before the
# resume guard below — the store-present check is mode-dependent. A pure yaml
# read, no side effect.
MEM0_MODE=""
if [ "$INTEGRATION_NAME" = "mem0" ]; then
  MEM0_MODE="$(read_mem0_mode)"
fi

# The QUERY lane (the recall-query rewriter) rides the roster proxy as ROLE3;
# each QUERY_* value defaults to the role-1/main provider value, so the
# default rewriter is the role-1 model with no user configuration. The lane
# shares the flat BASE_URL (a separate QUERY upstream would need the per-role
# BASE_URL roster form — not wired here).
QUERY_MODEL="${QUERY_MODEL:-$MODEL}"
QUERY_API_KEY="${QUERY_API_KEY:-$API_KEY}"
QUERY_API="${QUERY_API:-$API}"

# The driver manages the annotate lane URLs per instance: a stale
# MEMORY_ANNOTATE_* export (e.g. from a dead proxy, rule 5) must never leak
# into a run. The same goes for the QUERY lane's bridge env.
unset MEMORY_ANNOTATE_MAIN_URL MEMORY_ANNOTATE_MEMORY_URL
unset MEMORY_QUERY_MODEL_URL MEMORY_QUERY_MODEL MEMORY_QUERY_API_KEY

LOG="$RUN_ROOT/memory-arm.log"
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG"; }

# valid_preds ID — true iff the instance already produced a non-empty patch.
# Verdicts come from the single batched probe below (VALID_IDS): a per-id
# `uv run` would misread a transient uv/env failure as "no valid pred" —
# flagging a healthy instance DIRTY, or re-running a completed one (rule 3).
valid_preds() {
  printf '%s\n' "$VALID_IDS" | grep -qxF "$1"
}

# One batched validity probe for the whole id list, run once up front (it
# doubles as the arm's env health check): a broken shared env fails here,
# loudly, instead of being misread as "no valid pred" per instance below.
VALID_IDS="$(
  read_instance_ids "$RUN_ROOT/instance-ids.txt" | uv run --project "$REPO_ROOT" python -c '
import json
import pathlib
import sys

base = pathlib.Path(sys.argv[1])
for line in sys.stdin:
    iid = line.strip()
    if not iid:
        continue
    try:
        patch = json.loads((base / iid / "preds.json").read_text()).get(iid, {}).get("model_patch")
    except Exception:
        # Any per-id read/parse/shape failure (missing file, non-UTF-8 bytes,
        # invalid JSON, non-dict payload) reads as "no valid pred"; only a
        # process-level failure (a broken shared env) trips the FATAL below.
        continue
    if isinstance(patch, str) and patch.strip():
        print(iid)
' "$RUN_ROOT/runs/mini-swe-agent"
)" || { log "FATAL: shared-env probe failed (uv run); fix the environment and re-run"; exit 1; }

# Resume guard (rule 3), checked BEFORE any arm side effect (the arm claim,
# container sweep/start, and recorder .env regeneration all happen in the
# profile below): with scope=run the memory store is shared across the run
# root, and a rerun's fresh session id prevents message reprocessing but NOT
# recall of memories approved during an aborted attempt. An instance with a
# stale attempt (agent.log exists) but no valid patch would silently recall
# its failed attempt's memories — refuse and require a fresh run root. A
# crash between instances leaves the store clean and resumes fine.
# (cure_memory checks its run-root store file first — no file, no
# contamination possible; the mem0 platform store is hosted, always
# "present"; mem0 server mode's store is the run-root pg volume and library
# mode's the run-root store dir (the tencentdb container-volume semantics);
# tencentdb's store is the run-root container volume, likewise always present.)
STORE_PRESENT=1
if [ "$INTEGRATION_NAME" = "cure_memory" ] && [ ! -f "$RUN_ROOT/runs/mini-swe-agent/cure_memory.sqlite3" ]; then
  STORE_PRESENT=""
fi
if [ "$MEM0_MODE" = "server" ] && [ ! -d "$RUN_ROOT/mem0-server/pg" ]; then
  STORE_PRESENT=""
fi
if [ "$MEM0_MODE" = "library" ] && [ ! -d "$RUN_ROOT/mem0" ]; then
  STORE_PRESENT=""
fi
if [ -n "$STORE_PRESENT" ]; then
  DIRTY=""
  while IFS= read -r ID; do
    [ -z "$ID" ] && continue
    if [ -f "$RUN_ROOT/runs/mini-swe-agent/$ID/agent.log" ] && ! valid_preds "$ID"; then
      DIRTY="$DIRTY $ID"
    fi
  done < <(read_instance_ids "$RUN_ROOT/instance-ids.txt")
  if [ -n "$DIRTY" ]; then
    log "FATAL: stale attempt(s) without a valid preds.json:$DIRTY"
    log "FATAL: the shared run-scope memory store may hold memories approved during the aborted attempt(s); start a fresh run root with setup-run.sh (rule 3)"
    exit 1
  fi
fi

resolve_recorder() {
  RECORDER="$REPO_ROOT/extension/traj-recorder"
  [ -f "$RECORDER/pyproject.toml" ] || RECORDER="$PARENT_ROOT/extension/traj-recorder"
  [ -f "$RECORDER/pyproject.toml" ] || die "missing traj-recorder checkout at $RECORDER"
}

# write_recorder_env ROLE2_LABEL — the roster recorder .env is regenerated per
# run (mode 0600, never committed). Role2 reuses the provider values verbatim:
# the proxy refuses to boot with an incompletely declared role, even when the
# role makes no model calls (mem0's MEMORY lane). ROLE3=QUERY (the recall-query
# rewriter) rides the same flat BASE_URL; the roster's flat/per-role mutual
# exclusion is per value name, so the mixed form stays legal.
write_recorder_env() {
  umask 077
  cat > "$RECORDER/.env" <<EOF
ROLE1="MAIN"
ROLE1_MODEL="$MODEL"
ROLE1_API_KEY="$API_KEY"
ROLE1_API="$API"
ROLE2="$1"
ROLE2_MODEL="$MODEL"
ROLE2_API_KEY="$API_KEY"
ROLE2_API="$API"
ROLE3="QUERY"
ROLE3_MODEL="$QUERY_MODEL"
ROLE3_API_KEY="$QUERY_API_KEY"
ROLE3_API="$QUERY_API"
BASE_URL="$BASE_URL"
EOF
}

# start_proxy IDIR ID — start this instance's roster proxy and wait for all
# role env files; sets PROXY_PID/ENV1/ENV2/ENV3 (the caller's locals).
start_proxy() {
  local IDIR="$1" ID="$2" i
  mkdir -p "$IDIR/$ID"
  # A stale trajectory dir is from an aborted attempt; its .proxy_env_role*
  # would point at a dead trajectory ID and fail every model call.
  rm -rf "$IDIR/$ID/trajectory"

  ( cd "$RECORDER" && exec uv run trajectory \
      --output "$IDIR/$ID/trajectory" --label memory ) >"$IDIR/proxy.log" 2>&1 &
  PROXY_PID=$!

  ENV1="" ENV2="" ENV3=""
  for i in $(seq 1 90); do
    ENV1="$(find "$IDIR/$ID/trajectory" -name .proxy_env_role1 2>/dev/null | head -1)"
    ENV2="$(find "$IDIR/$ID/trajectory" -name .proxy_env_role2 2>/dev/null | head -1)"
    ENV3="$(find "$IDIR/$ID/trajectory" -name .proxy_env_role3 2>/dev/null | head -1)"
    if [ -n "$ENV1" ] && [ -n "$ENV2" ] && [ -n "$ENV3" ]; then break; fi
    kill -0 "$PROXY_PID" 2>/dev/null || break
    sleep 1
  done
  if [ -z "$ENV1" ] || [ -z "$ENV2" ] || [ -z "$ENV3" ]; then
    log "FAIL $ID: proxy did not publish roster env files (see $IDIR/proxy.log)"
    kill "$PROXY_PID" 2>/dev/null
    wait "$PROXY_PID" 2>/dev/null
    return 1
  fi
}

# stop_proxy — SIGINT (never SIGKILL) so the recorder finalizes the run dir.
stop_proxy() {
  local i
  kill -INT "$PROXY_PID" 2>/dev/null
  for i in $(seq 1 30); do kill -0 "$PROXY_PID" 2>/dev/null || break; sleep 1; done
  kill "$PROXY_PID" 2>/dev/null
  wait "$PROXY_PID" 2>/dev/null
}

# lane_base_url ENV_FILE — echo the trajectory-scoped OPENAI_BASE_URL one
# roster role env file published. The URL is read in a subshell so it never
# enters the log; the */trajectories/*/v1 guard fails loudly (non-zero, empty
# output) when the file holds no trajectory-scoped URL (e.g. a
# non-openai-chat roster publishes format-specific variables instead): an
# unchecked empty value would surface only as the backend's fail-closed
# start, after the episode already ran with memory silently off. The one
# read+guard behind every lane export (EXTRACT / MEMORY / QUERY) — a fix to
# the pattern can never leave one arm carrying the old behavior.
lane_base_url() {
  local url
  url="$(bash -c 'source "$1"; printf %s "$OPENAI_BASE_URL"' _ "$1")"
  case "$url" in
    */trajectories/*/v1) printf %s "$url" ;;
    *) return 1 ;;
  esac
}

# export_query_lane ENV3 — publish the QUERY lane's bridge env (the recall-query
# rewriter) from the role3 env file: the trajectory-scoped URL, the bare model
# name, and the placeholder credential — the same wiring as the EXTRACT lane's.
export_query_lane() {
  local base_url
  base_url="$(lane_base_url "$1")" || return 1
  MEMORY_QUERY_MODEL_URL="$base_url"
  export MEMORY_QUERY_MODEL_URL
  export MEMORY_QUERY_MODEL="$QUERY_MODEL"
  export MEMORY_QUERY_API_KEY="trajectory-proxy"
}

# ---- MemoryCore container lifecycle (tencentdb arm) ----
# One container per run root (not per instance): the host port is fixed, so
# two tencentdb run roots cannot run concurrently on one machine — enforced
# at the process level by the machine-wide arm claim taken below. Data lives
# on the host volume, so teardown is a plain `docker rm -f` (no
# graceful-shutdown requirement, unlike the proxy).
TDAI_IMAGE="agentmemory/memory-core:1.0.1-beta.1"
TDAI_PORT=8420

write_tdai_gateway_yaml() {
  mkdir -p "$RUN_ROOT/tdai"
  # Credential-free: the ${TDAI_*} leaves are interpolated by the container's
  # config loader from docker-injected env; keys never land in this file or
  # any artifact. promptMode "code" is the coding-agent extraction prompt
  # family (the default "chat" runs the chat prompt on SWE-bench traffic).
  # l1IdleTimeoutSeconds 30 (not the 600 s default) is load-bearing: the
  # episode tail must land inside the backend's finalize drain. bm25 language
  # "en" is required — the upstream default is "zh" (jieba).
  cat > "$RUN_ROOT/tdai/tdai-gateway.yaml" <<EOF
deployMode: standalone
stateBackend: local
promptMode: "code"
server:
  port: 8420
  host: "0.0.0.0"     # bound to localhost by docker -p
data:
  baseDir: "/data/tdai-memory"
llm:
  baseUrl: "\${TDAI_LLM_BASE_URL}"
  apiKey: "\${TDAI_LLM_API_KEY}"
  model: "\${TDAI_LLM_MODEL}"
  provider: "openai"
  # Reasoning-hybrid providers (deepseek-v4-flash et al.) burn the 4096-token
  # default entirely on thinking (finishReason=length with 0 output chars —
  # the extraction then stores nothing). 32k + 300 s matches the shipped
  # upstream production yaml for deepseek.
  maxTokens: 32000
  timeoutMs: 300000
memory:
  capture:
    enabled: true
  extraction:
    enabled: true
    enableDedup: true
    maxMemoriesPerSession: 20
  persona:            # server defaults, pinned
    triggerEveryN: 50
    maxScenes: 15
  pipeline:
    everyNConversations: 5
    enableWarmup: true
    l1IdleTimeoutSeconds: 30
    l2DelayAfterL1Seconds: 90
    l2MinIntervalSeconds: 900
    l2MaxIntervalSeconds: 3600
  recall:             # v1 /recall only; v3 atomic/search is unaffected
    enabled: true
    maxResults: 5
    scoreThreshold: 0.3
  storeBackend: sqlite
  embedding:
    provider: "${TDAI_EMBEDDING_PROVIDER}"
${TDAI_EMBEDDING_SECTION}  bm25:
    enabled: true
    language: "en"
EOF
}

start_tdai_container() {
  command -v docker >/dev/null 2>&1 || die "docker is required for the tencentdb arm"
  docker info >/dev/null 2>&1 || die "docker is not running (docker info fails)"
  # Idempotent pre-run removal of every tdai-* container, not just this
  # root's: the EXIT-trap teardown does not cover a SIGKILL'd driver, and a
  # leaked tdai-<oldroot> keeps 127.0.0.1:8420 bound — failing every later
  # tencentdb arm at docker run. Sweeping the prefix is safe because the
  # machine-wide arm claim was taken before this point: no other tencentdb
  # driver can be alive, so every leftover tdai-* container is a leak, never
  # a live arm's store.
  docker ps -aq --filter "name=^/tdai-" | xargs docker rm -f >/dev/null 2>&1 || true
  mkdir -p "$RUN_ROOT/tdai/data"
  write_tdai_gateway_yaml
  # The LLM credentials ride the environment, never the docker run argv
  # (rule 8): bare `-e NAME` forwards the exported host value without
  # exposing it to ps. export(1) is a builtin — the assignment itself never
  # reaches a process command line either.
  export TDAI_LLM_BASE_URL TDAI_LLM_API_KEY="$API_KEY" TDAI_LLM_MODEL="$MODEL"
  docker run -d --name "$TDAI_CONTAINER" \
    -p "127.0.0.1:${TDAI_PORT}:8420" \
    -v "$RUN_ROOT/tdai/tdai-gateway.yaml:/data/config/tdai-gateway.yaml:ro" \
    -v "$RUN_ROOT/tdai/data:/data/tdai-memory" \
    -e TDAI_LLM_BASE_URL \
    -e TDAI_LLM_API_KEY \
    -e TDAI_LLM_MODEL \
    ${TDAI_EMBEDDING_ENV[@]+"${TDAI_EMBEDDING_ENV[@]}"} \
    "$TDAI_IMAGE" >/dev/null || die "failed to start the MemoryCore container ($TDAI_IMAGE)"
  local i health
  for i in $(seq 1 120); do
    if health="$(curl -sf "http://127.0.0.1:${TDAI_PORT}/health" 2>/dev/null)"; then
      # The vector lane's only boot-time signal: a misconfigured remote
      # embedding provider is disabled SILENTLY upstream (the configError is
      # stored, never logged; the HTTP status stays 200) — the all-or-none
      # gate above refuses a partial roster set, but only this body check
      # catches the quartet failing to reach the container (a bm25-only arm
      # under a misleading embedding=openai log line).
      if [ "$TDAI_EMBEDDING_PROVIDER" = "openai" ] && ! grep -q '"embeddingService":true' <<<"$health"; then
        docker logs --tail 20 "$TDAI_CONTAINER" >&2 || true
        die "MemoryCore reports embeddingService=false with the embedding quartet configured (the TDAI_EMBEDDING_* env did not reach the container)"
      fi
      log "tdai container ready ($TDAI_CONTAINER, image $TDAI_IMAGE)"
      return 0
    fi
    sleep 1
  done
  docker logs --tail 50 "$TDAI_CONTAINER" >&2 || true
  die "MemoryCore container did not become healthy within 120s (logs above)"
}

# ---- mem0 OSS server stack lifecycle (mem0 server mode) ----
# Two containers on one bridge network per run root (the topology the vendored
# server's boot chain requires — see integration/mem0/VENDORING.md): a
# pgvector/pg vector DB (the app DB `mem0_app` PLUS the always-present default
# `postgres` database the vector store targets) and the API server built from
# the vendored tree with the engine pinned. Port 8890 is fixed, so two mem0
# server arms cannot run concurrently on one machine — enforced at the process
# level by the machine-wide claim taken in the profile (same discipline as the
# tencentdb arm's 8420 claim; see that block for the full claim rationale).
# The image tag is assigned in build_mem0_server_image (it embeds the clone's
# routes pin, read where the clone is validated — platform/library modes never
# reference it). The engine pin lives in exactly ONE place: the tag, the
# requirements rewrite, and its verify check all expand MEM0_ENGINE_PIN — a
# bump can never mislabel an image with the old pin (which the
# `docker image inspect` short-circuit would then keep reusing).
MEM0_SERVER_PORT=8890
MEM0_ENGINE_PIN=2.0.19

build_mem0_server_image() {
  local clone="$INTEGRATION/vendor/mem0"
  [ -f "$clone/server/Dockerfile" ] || die "missing the vendored mem0 clone at $clone (server mode builds from it — see integration/mem0/VENDORING.md)"
  # The tag keys on BOTH pins — the engine pin (the requirements rewrite below)
  # and the clone's HEAD (the routes): the short-circuit on `docker image
  # inspect` must rebuild, never silently reuse a stale image, when either pin
  # moves.
  local routes_pin
  routes_pin="$(git -C "$clone" rev-parse --short HEAD)" \
    || die "cannot read the routes pin of the vendored clone at $clone (git rev-parse failed)"
  MEM0_SERVER_IMAGE="mem0-oss-server:${MEM0_ENGINE_PIN}-${routes_pin}"
  if docker image inspect "$MEM0_SERVER_IMAGE" >/dev/null 2>&1; then
    return 0
  fi
  # The clone pins the ROUTES; the ENGINE is pinned here: requirements.txt
  # carries an unpinned lower bound (mem0ai>=0.1.48), so a naive build floats
  # with PyPI latest. Rewrite it in a staged copy — the clone stays pristine.
  # Also switch psycopg to its binary variant: the slim base ships no libpq
  # and the pure wheel dies at first connect ("libpq library not found") —
  # same version range, bundled driver library. The first build needs network
  # for pip.
  local ctx="$MEM0_SERVER_ROOT/build-context"
  rm -rf "$ctx"
  cp -R "$clone/server" "$ctx"
  sed -i.bak -e "s/^mem0ai>=.*\$/mem0ai==${MEM0_ENGINE_PIN}/" -e 's/^psycopg>=/psycopg[binary]>=/' "$ctx/requirements.txt" && rm -f "$ctx/requirements.txt.bak"
  grep -q "^mem0ai==${MEM0_ENGINE_PIN}\$" "$ctx/requirements.txt" || die "failed to pin mem0ai==${MEM0_ENGINE_PIN} in the staged build context"
  grep -q '^psycopg\[binary\]>=' "$ctx/requirements.txt" || die "failed to switch psycopg to the binary variant in the staged build context"
  log "building $MEM0_SERVER_IMAGE from the vendored tree (routes $routes_pin, engine mem0ai==${MEM0_ENGINE_PIN}; first build needs network)"
  docker build -q -t "$MEM0_SERVER_IMAGE" "$ctx" >/dev/null || die "failed to build $MEM0_SERVER_IMAGE"
  rm -rf "$ctx"
}

start_mem0_server_stack() {
  command -v docker >/dev/null 2>&1 || die "docker is required for the mem0 server arm"
  docker info >/dev/null 2>&1 || die "docker is not running (docker info fails)"
  # Idempotent pre-run removal of every mem0-server-* container/network, not
  # just this root's: the EXIT-trap teardown does not cover a SIGKILL'd driver,
  # and a leaked stack keeps 127.0.0.1:8890 bound. Safe for the same reason as
  # the tdai-* sweep: the machine-wide claim was taken before this point.
  docker ps -aq --filter "name=^/mem0-server-" | xargs docker rm -f >/dev/null 2>&1 || true
  # Network names are mem0-server-<run-root-basename>-net: the anchor is the
  # same mem0-server- prefix as the containers (networks carry no leading /).
  docker network ls -q --filter "name=^mem0-server-" | xargs docker network rm >/dev/null 2>&1 || true

  MEM0_SERVER_ROOT="$RUN_ROOT/mem0-server"
  # Mounted AT /app/history: the server's SQLiteManager does a bare
  # sqlite3.connect with NO makedirs and the image ships no such directory —
  # boot-fatal without the mount.
  mkdir -p "$MEM0_SERVER_ROOT/history"
  build_mem0_server_image

  local base="mem0-server-$(basename "$RUN_ROOT")"
  MEM0_PG_CONTAINER="$base-pg"
  MEM0_APP_CONTAINER="$base-app"
  MEM0_SERVER_NET="$base-net"
  # The pg password is a local throwaway credential, generated once per run
  # root and REUSED on resume: the stock postgres entrypoint applies it only
  # on first init, so a resumed run over the existing pg volume must present
  # the original password. It lives only in the run root (output/ is
  # gitignored) and rides docker -e NAME, never argv (rule 8).
  local password_file="$MEM0_SERVER_ROOT/pg_password"
  if [ -f "$password_file" ]; then
    POSTGRES_PASSWORD="$(cat "$password_file")"
  else
    ( umask 077 && openssl rand -hex 16 > "$password_file" )
    POSTGRES_PASSWORD="$(cat "$password_file")"
  fi
  export POSTGRES_PASSWORD

  docker network create "$MEM0_SERVER_NET" >/dev/null || die "failed to create the mem0 server network"
  # POSTGRES_DB=mem0_app: the entrypoint creates it on first init (the app DB
  # alembic migrates); the default `postgres` database initdb always creates
  # is the vector store's target — no init-db.sh mount needed either way.
  docker run -d --name "$MEM0_PG_CONTAINER" --network "$MEM0_SERVER_NET" \
    -e POSTGRES_USER=mem0 -e POSTGRES_PASSWORD -e POSTGRES_DB=mem0_app \
    -v "$MEM0_SERVER_ROOT/pg:/var/lib/postgresql/data" \
    pgvector/pgvector:pg17 >/dev/null || die "failed to start the pg container (pgvector/pgvector:pg17)"
  local i
  for i in $(seq 1 60); do
    docker exec "$MEM0_PG_CONTAINER" pg_isready -U mem0 -d mem0_app >/dev/null 2>&1 && break
    sleep 1
    if [ "$i" = 60 ]; then
      docker logs --tail 30 "$MEM0_PG_CONTAINER" >&2 || true
      die "the mem0 server pg container did not become ready within 60s (logs above)"
    fi
  done

  # Precreate the vector table at the roster's embedding dims BEFORE the
  # server boots: the server's DEFAULT_CONFIG has no dims channel, so its
  # eager create_col would otherwise birth the collection at the pgvector
  # default (1536) and every insert would then fail with "expected 1536
  # dimensions" — SILENTLY (the add still returns ADD receipts; only the
  # container log shows the DataException). _ensure_collection skips an
  # existing table, so this precreate is authoritative. The table DDL mirrors
  # the engine's own create_col (mem0/vector_stores/pgvector.py), minus its
  # HNSW and GIN indexes — dead weight at run-root scale; the vector store
  # connects to the default `postgres` database. The retry absorbs the stock
  # entrypoint's initdb restart gap: on a fresh volume pg_isready passes
  # against the temporary init server, which then stops before the real
  # postmaster starts.
  local attempt
  for attempt in $(seq 1 10); do
    if docker exec "$MEM0_PG_CONTAINER" psql -U mem0 -d postgres -v ON_ERROR_STOP=1 \
      -c "CREATE EXTENSION IF NOT EXISTS vector" \
      -c "CREATE TABLE IF NOT EXISTS memories (id UUID PRIMARY KEY, vector vector(${EMBEDDING_DIMENSIONS}), payload JSONB)" \
      >/dev/null 2>&1; then
      break
    fi
    if [ "$attempt" = 10 ]; then
      docker logs --tail 30 "$MEM0_PG_CONTAINER" >&2 || true
      die "failed to precreate the mem0 memories table at ${EMBEDDING_DIMENSIONS} dims (pg logs above)"
    fi
    sleep 1
  done

  # The full boot env, covering the server's three import-time fatals: LLM
  # creds (roster, /v1-normalized — honored at boot by BOTH the LLM and the
  # embedder clients), the /app/history mount (above), and mem0_app existence
  # (the pg container's POSTGRES_DB). Never enable auth without a JWT_SECRET
  # (module-level RuntimeError at import) — the arm runs AUTH_DISABLED and the
  # store sends no credentials. OPENAI_* are re-asserted from the roster here:
  # the per-instance proxy env sourcing overwrites them later, and the
  # container needs the /v1-normalized root regardless.
  export OPENAI_API_KEY="$API_KEY"
  export OPENAI_BASE_URL="$(openai_v1_root "$BASE_URL")"
  docker run -d --name "$MEM0_APP_CONTAINER" --network "$MEM0_SERVER_NET" \
    -p "127.0.0.1:${MEM0_SERVER_PORT}:8000" \
    -v "$MEM0_SERVER_ROOT/history:/app/history" \
    -e POSTGRES_HOST="$MEM0_PG_CONTAINER" -e POSTGRES_PORT=5432 -e POSTGRES_USER=mem0 -e POSTGRES_PASSWORD \
    -e APP_DB_NAME=mem0_app -e AUTH_DISABLED=true -e MEM0_TELEMETRY=false \
    -e OPENAI_API_KEY -e OPENAI_BASE_URL \
    -e MEM0_DEFAULT_LLM_MODEL="$MODEL" -e MEM0_DEFAULT_EMBEDDER_MODEL="$EMBEDDING_MODEL" \
    "$MEM0_SERVER_IMAGE" \
    sh -c "alembic upgrade head && uvicorn main:app --host 0.0.0.0 --port 8000" >/dev/null \
    || die "failed to start the mem0 server container ($MEM0_SERVER_IMAGE)"
  # The image CMD runs no migrations and carries the dev --reload flag; the
  # override above runs alembic first and drops the flag.
  for i in $(seq 1 180); do
    if curl -sf "http://127.0.0.1:${MEM0_SERVER_PORT}/auth/setup-status" >/dev/null 2>&1; then
      log "mem0 server stack ready ($MEM0_APP_CONTAINER, image $MEM0_SERVER_IMAGE)"
      return 0
    fi
    sleep 1
  done
  docker logs --tail 50 "$MEM0_APP_CONTAINER" >&2 || true
  die "mem0 server container did not become ready within 180s (logs above)"
}

configure_mem0_server() {
  # Refine llm+embedder onto the roster upstreams. Belt-and-braces for the LLM
  # leg (the boot env already points there) but LOAD-BEARING for the embedding
  # leg whenever EMBEDDING_BASE_URL differs from the roster BASE_URL (the boot
  # env carries a single OPENAI_BASE_URL). The payload deep-merges into the
  # boot config, so vector_store/history survive. FAIL-FATAL on non-2xx.
  # max_tokens 32000 (not the engine's 2000 default): reasoning-hybrid roster
  # models (deepseek-v4-flash et al.) burn a small budget entirely on thinking
  # — finish_reason=length with 0 output chars, and the extraction stores
  # NOTHING while answering 200 (the trap the tencentdb gateway yaml documents;
  # 32k matches its shipped deepseek value).
  # Credentials ride curl stdin, never argv (rule 8).
  if ! curl -sf -X POST "http://127.0.0.1:${MEM0_SERVER_PORT}/configure" -H 'Content-Type: application/json' --data-binary @- <<EOF
{"llm": {"provider": "openai", "config": {"model": "$MODEL", "api_key": "$API_KEY", "openai_base_url": "$(openai_v1_root "$BASE_URL")", "max_tokens": 32000}}, "embedder": {"provider": "openai", "config": {"model": "$EMBEDDING_MODEL", "api_key": "$EMBEDDING_API_KEY", "openai_base_url": "$(openai_v1_root "$EMBEDDING_BASE_URL")", "embedding_dims": $EMBEDDING_DIMENSIONS}}}
EOF
  then
    docker logs --tail 30 "$MEM0_APP_CONTAINER" >&2 || true
    die "POST /configure failed (mem0 server mode) — the embedder would stay aimed at the roster LLM upstream"
  fi
  log "mem0 server configured (llm=$MODEL embedder=$EMBEDDING_MODEL)"
}

# selfcheck_mem0_server — the fail-closed canary: one verbatim add + search +
# delete under a scratch user id proves the embedding leg (quartet upstream)
# and the vector insert path end to end. Without it the silent-insert class
# (e.g. a dims mismatch — the add returns ADD receipts while persisting
# NOTHING) surfaces only as an empty store mid-arm.
selfcheck_mem0_server() {
  MEM0_SERVER_URL="http://127.0.0.1:${MEM0_SERVER_PORT}" uv run --project "$REPO_ROOT" python - <<'EOF' || { docker logs --tail 30 "$MEM0_APP_CONTAINER" >&2 || true; die "mem0 server self-check failed (see logs above)"; }
import os

from mem0_bridge.stores.server import ServerStore

store = ServerStore(server_url=os.environ["MEM0_SERVER_URL"])
receipts = store.add(
    messages=[{"role": "user", "content": "canary: the mem0 server stack persists and indexes memories"}],
    user_id="mem0-canary",
    run_id="canary",
    infer=False,  # verbatim: exercises the embedder + insert path without an LLM call
)
hits = store.search(query="canary persists indexes", user_id="mem0-canary", top_k=5, threshold=0.0)
if not hits:
    raise SystemExit("canary add was not searchable — the store is silently not persisting")
for receipt in receipts:
    if receipt.get("id"):
        store.delete(receipt["id"])
print(f"mem0 server self-check ok ({len(receipts)} added, {len(hits)} searchable, cleaned up)")
EOF
  log "mem0 server self-check passed"
}

# ---- Per-integration profile ----
case "$INTEGRATION_NAME" in
  cure_memory)
    export CURE_MEMORY_REPO="$INTEGRATION/src"
    resolve_recorder
    write_recorder_env "EXTRACT"
    ;;
  mem0)
    MEM0_USER_ID="minisweagent-mem0-$(basename "$RUN_ROOT")"
    case "$MEM0_MODE" in
      platform)
        # The hosted store is persistent across run roots: run isolation comes
        # from the per-run user id minted above. MEM0_API_KEY rides the
        # environment or the root .env (rule 8; load_model_env already
        # exported the .env copy when present).
        : "${MEM0_API_KEY:?MEM0_API_KEY must be set in the environment or $ENV_FILE (mem0 platform mode)}"
        ;;
      server)
        require_mem0_embedding_quartet
        # The machine-wide single-arm claim (port 8890 already makes concurrent
        # mem0 server arms impossible; the claim enforces it at the process
        # level). Same discipline as the tencentdb arm's claim — see that block
        # for the full rationale (atomic mkdir, dead-pid takeover, per-user
        # TMPDIR). Claimed before the recorder .env is regenerated too.
        MEM0_ARM_CLAIM="${TMPDIR:-/tmp}/mem0-arm-claim"
        MEM0_CLAIM_WAITS=0
        MEM0_CLAIM_VANISHED=0
        while ! mkdir "$MEM0_ARM_CLAIM" 2>/dev/null; do
          if [ ! -e "$MEM0_ARM_CLAIM" ]; then
            # The mkdir failed yet the path is already gone: a concurrent
            # contender's stale-takeover rm -rf landed in between (the next
            # mkdir may win — retry), or the parent is genuinely unwritable
            # (a real failure — die after a bounded wait instead of spinning
            # forever silently, the same guard the tencentdb claim carries).
            MEM0_CLAIM_VANISHED=$((MEM0_CLAIM_VANISHED + 1))
            if [ "$MEM0_CLAIM_VANISHED" -ge 20 ]; then
              die "cannot create the mem0 arm claim at $MEM0_ARM_CLAIM"
            fi
            sleep 0.1
            continue
          fi
          MEM0_CLAIM_VANISHED=0
          MEM0_CLAIM_PID="$(cat "$MEM0_ARM_CLAIM/pid" 2>/dev/null || true)"
          if [ -n "$MEM0_CLAIM_PID" ]; then
            if kill -0 "$MEM0_CLAIM_PID" 2>/dev/null || ps -p "$MEM0_CLAIM_PID" >/dev/null 2>&1; then
              die "another mem0 server arm driver (pid $MEM0_CLAIM_PID) holds the machine-wide claim; port $MEM0_SERVER_PORT and the mem0-server-* containers belong to it — let it finish or stop it first (if that pid was recycled by an unrelated process after a SIGKILLed driver, remove $MEM0_ARM_CLAIM and retry)"
            fi
            rm -rf "$MEM0_ARM_CLAIM"  # dead holder: stale takeover
            continue
          fi
          MEM0_CLAIM_WAITS=$((MEM0_CLAIM_WAITS + 1))
          if [ "$MEM0_CLAIM_WAITS" -ge 50 ]; then
            rm -rf "$MEM0_ARM_CLAIM"  # a holder that never writes its pid died mid-acquire
            MEM0_CLAIM_WAITS=0
          fi
          sleep 0.1
        done
        printf '%s\n' "$$" > "$MEM0_ARM_CLAIM/pid"
        # A concurrent contender's stale-takeover rm -rf can land between our
        # winning mkdir and that write (or the write lands inside its fresh
        # dir): if the pid does not read back as ours the claim was stolen
        # mid-acquire — die rather than run two arms that would sweep each
        # other's containers. No false positive: a contender only rm's a
        # claim whose pid reads dead, and ours is live from this point.
        [ "$(cat "$MEM0_ARM_CLAIM/pid" 2>/dev/null)" = "$$" ] || die "the mem0 arm claim at $MEM0_ARM_CLAIM was stolen mid-acquire by a concurrent driver — retry the arm"
        # Registered BEFORE the stack starts: a health-timeout die must not
        # leak the containers or the claim. Plain docker rm -f on exit: the
        # store lives on the host volumes (<run-root>/mem0-server), so a resume
        # recreates the stack over the same store. The container/net names are
        # unset until start_mem0_server_stack — default-expand them so a die
        # before that point never trips set -u inside the trap.
        trap 'if [ "$(cat "$MEM0_ARM_CLAIM/pid" 2>/dev/null)" = "$$" ]; then rm -rf "$MEM0_ARM_CLAIM"; fi; docker rm -f ${MEM0_APP_CONTAINER:-} ${MEM0_PG_CONTAINER:-} >/dev/null 2>&1 || true; docker network rm ${MEM0_SERVER_NET:-} >/dev/null 2>&1 || true' EXIT
        start_mem0_server_stack
        configure_mem0_server
        selfcheck_mem0_server
        export MEM0_SERVER_URL="http://127.0.0.1:${MEM0_SERVER_PORT}"
        ;;
      library)
        require_mem0_embedding_quartet
        # In-process engine: the opt-in dependency group rides every instance
        # invocation (a plain `uv run` is inexact but NOT additive — without
        # this every library-mode instance dies on `import mem0`), telemetry is
        # off (posthog phone-home hygiene), and the store lands under the run
        # root via the agent.memory.run_root extra in run_instance_mem0.
        export MEM0_TELEMETRY=false
        ARM_UV_ARGS=(--group mem0-library)
        ;;
    esac
    resolve_recorder
    write_recorder_env "MEMORY"
    ;;
  tencentdb)
    # Embedding lane: all four keys or none. A partially-configured remote
    # provider is *silently disabled* upstream (the configError is stored in
    # a field nothing in the gateway ever logs — the only signal would be
    # degraded search), so the driver enables the lane only on the complete
    # set and the arm never depends on it.
    TDAI_EMBEDDING_FIELDS=0
    for _v in EMBEDDING_MODEL EMBEDDING_API_KEY EMBEDDING_BASE_URL EMBEDDING_DIMENSIONS; do
      eval "value=\${$_v:-}"
      [ -n "$value" ] && TDAI_EMBEDDING_FIELDS=$((TDAI_EMBEDDING_FIELDS + 1))
    done
    if [ "$TDAI_EMBEDDING_FIELDS" -ne 0 ] && [ "$TDAI_EMBEDDING_FIELDS" -ne 4 ]; then
      die "EMBEDDING_* in $ENV_FILE must be set all four (MODEL, API_KEY, BASE_URL, DIMENSIONS) or none — a partial set is silently disabled upstream"
    fi
    if [ "$TDAI_EMBEDDING_FIELDS" -eq 4 ]; then
      case "$EMBEDDING_DIMENSIONS" in
        *[!0-9]*) die "EMBEDDING_DIMENSIONS in $ENV_FILE must be a positive integer (got '$EMBEDDING_DIMENSIONS') — a non-numeric value is silently disabled upstream" ;;
      esac
      # The numeric case check still passes "0", which upstream's loader
      # treats as MISSING (dimensions <= 0 fails the remote-provider
      # requirement) — the same silently-disabled class the gate exists for.
      if [ "$EMBEDDING_DIMENSIONS" -le 0 ]; then
        die "EMBEDDING_DIMENSIONS in $ENV_FILE must be a positive integer (got '$EMBEDDING_DIMENSIONS') — a non-positive value is silently disabled upstream"
      fi
      TDAI_EMBEDDING_PROVIDER="openai"
      # Literal \${...} leaves: the container's config loader interpolates
      # them from docker-injected env; keys never land in the yaml.
      TDAI_EMBEDDING_SECTION='    apiKey: "${TDAI_EMBEDDING_API_KEY}"
    baseUrl: "${TDAI_EMBEDDING_BASE_URL}"
    model: "${TDAI_EMBEDDING_MODEL}"
    timeoutMs: 60000
'
      TDAI_EMBEDDING_SECTION="${TDAI_EMBEDDING_SECTION}    dimensions: ${EMBEDDING_DIMENSIONS}
"
      # Bare -e NAME forwards only names the shell itself exports: the
      # quartet must ride the TDAI_* names the yaml leaves reference (the
      # roster's EMBEDDING_* names are never asked for — an unexported
      # TDAI_* name reaches the container as nothing at all, and upstream
      # then disables the vector lane SILENTLY). export(1) is a builtin —
      # the values never touch the docker run argv (rule 8, ps-visibility),
      # and whitespace/glob characters survive intact.
      export TDAI_EMBEDDING_API_KEY="$EMBEDDING_API_KEY"
      export TDAI_EMBEDDING_BASE_URL="$EMBEDDING_BASE_URL"
      export TDAI_EMBEDDING_MODEL="$EMBEDDING_MODEL"
      export TDAI_EMBEDDING_DIMENSIONS="$EMBEDDING_DIMENSIONS"
      TDAI_EMBEDDING_ENV=(
        -e TDAI_EMBEDDING_API_KEY
        -e TDAI_EMBEDDING_BASE_URL
        -e TDAI_EMBEDDING_MODEL
        -e TDAI_EMBEDDING_DIMENSIONS
      )
    else
      TDAI_EMBEDDING_PROVIDER="none"
      TDAI_EMBEDDING_SECTION=""
      TDAI_EMBEDDING_ENV=()
    fi
    # The container's LLM client calls \${baseUrl}/chat/completions directly,
    # so the OpenAI-compatible root (with /v1) is required — the roster
    # BASE_URL is the litellm form (with or without a trailing slash), so
    # strip the slash first, then append /v1 only when missing.
    TDAI_LLM_BASE_URL="${BASE_URL%/}"
    case "$TDAI_LLM_BASE_URL" in
      */v1) ;;
      *) TDAI_LLM_BASE_URL="$TDAI_LLM_BASE_URL/v1" ;;
    esac
    TDAI_CONTAINER="tdai-$(basename "$RUN_ROOT")"
    TDAI_USER_ID="minisweagent-tdai-$(basename "$RUN_ROOT")"
    # The machine-wide single-arm claim (port 8420 already makes concurrent
    # tencentdb arms impossible; the claim enforces it at the process level):
    # the sweep in start_tdai_container removes every tdai-* container, so a
    # live peer arm must fail THIS arm loudly here — killing its container
    # silently would fail its episode mid-run. The claim lives under the
    # per-user temp dir, NOT the checkout: Docker Desktop's daemon is
    # per-user, so one lock per user covers every checkout of this bundle on
    # the machine (a checkout-local claim would let a second checkout sweep a
    # live arm's container). Acquisition is the atomic
    # mkdir(2) of the claim dir — a check-then-write file claim would let two
    # drivers started together both pass and sweep each other's containers.
    # The holder's pid lands in the dir a beat after its mkdir, so a claim
    # with no readable pid yet is a holder mid-acquire: retry briefly, never
    # take over (a holder dying exactly there leaves it forever; the bounded
    # wait then breaks the tie). A claim naming a dead pid is stale (a
    # SIGKILL'd driver): remove it and retry — re-acquisition is still the
    # atomic mkdir, so exactly one contender wins. Claimed
    # before the recorder .env is regenerated too, so a live peer's next
    # per-instance proxy launch never reads a foreign roster.
    TDAI_ARM_CLAIM="${TMPDIR:-/tmp}/tdai-arm-claim"
    TDAI_CLAIM_WAITS=0
    TDAI_CLAIM_VANISHED=0
    while ! mkdir "$TDAI_ARM_CLAIM" 2>/dev/null; do
      if [ ! -e "$TDAI_ARM_CLAIM" ]; then
        # The mkdir failed yet the path is already gone: a concurrent
        # contender's stale-takeover rm -rf landed in between (the next
        # mkdir wins — retry), or the parent is genuinely unwritable (a
        # real failure — die after a bounded wait instead of spinning
        # forever silently).
        TDAI_CLAIM_VANISHED=$((TDAI_CLAIM_VANISHED + 1))
        if [ "$TDAI_CLAIM_VANISHED" -ge 20 ]; then
          die "cannot create the tencentdb arm claim at $TDAI_ARM_CLAIM"
        fi
        sleep 0.1
        continue
      fi
      TDAI_CLAIM_VANISHED=0
      TDAI_CLAIM_PID="$(cat "$TDAI_ARM_CLAIM/pid" 2>/dev/null || true)"
      if [ -n "$TDAI_CLAIM_PID" ]; then
        # Liveness must see OTHER users' processes: with TMPDIR unset (typical
        # Linux) the claim dir is machine-wide, and kill -0 on a foreign live
        # pid fails with EPERM — misreading that as dead would steal the claim
        # and let the pre-run sweep remove a live peer's tdai-* container.
        # ps -p sees any pid regardless of owner.
        if kill -0 "$TDAI_CLAIM_PID" 2>/dev/null || ps -p "$TDAI_CLAIM_PID" >/dev/null 2>&1; then
          die "another tencentdb arm driver (pid $TDAI_CLAIM_PID) holds the machine-wide claim; port 8420 and the tdai-* containers belong to it — let it finish or stop it first (if that pid was recycled by an unrelated process after a SIGKILLed driver, remove $TDAI_ARM_CLAIM and retry)"
        fi
        rm -rf "$TDAI_ARM_CLAIM"  # dead holder: stale takeover
        continue
      fi
      # No pid yet: a holder mid-acquire (its pid lands a beat after its
      # mkdir). Retry briefly; if it never lands the holder died there.
      TDAI_CLAIM_WAITS=$((TDAI_CLAIM_WAITS + 1))
      if [ "$TDAI_CLAIM_WAITS" -ge 50 ]; then
        rm -rf "$TDAI_ARM_CLAIM"
        TDAI_CLAIM_WAITS=0
      fi
      sleep 0.1
    done
    printf '%s\n' "$$" > "$TDAI_ARM_CLAIM/pid"
    # Same mid-acquire theft guard as the mem0 claim: a contender's
    # stale-takeover rm -rf landing between our mkdir and this write (or the
    # write landing in its fresh dir) must not leave two drivers each
    # believing they hold the claim — both would sweep the other's
    # containers. A live pid is never read as stale, so this cannot misfire.
    [ "$(cat "$TDAI_ARM_CLAIM/pid" 2>/dev/null)" = "$$" ] || die "the tencentdb arm claim at $TDAI_ARM_CLAIM was stolen mid-acquire by a concurrent driver — retry the arm"
    resolve_recorder
    write_recorder_env "MEMORY"
    # Registered BEFORE the container starts: a health-timeout die inside
    # start_tdai_container must not leak a container bound to 127.0.0.1:8420
    # or the arm claim. The trap stays name-scoped to THIS root's container
    # (a foreign one is never ours to kill on exit) and releases the claim
    # only while this driver still holds it (the pid inside is ours — while
    # this driver is alive no contender can read the claim as stale, so the
    # check cannot race a takeover); a SIGKILL'd driver leaks both, and the
    # next arm reclaims them via the dead-pid claim takeover and the pre-run
    # sweep. Plain rm -rf on exit: data lives on the host volume
    # (<run-root>/tdai), so a resume recreates the container over the same
    # store.
    trap 'if [ "$(cat "$TDAI_ARM_CLAIM/pid" 2>/dev/null)" = "$$" ]; then rm -rf "$TDAI_ARM_CLAIM"; fi; docker rm -f "$TDAI_CONTAINER" >/dev/null 2>&1 || true' EXIT
    start_tdai_container
    ;;
esac

# ---- Per-integration instance runner ----
run_instance_cure_memory() {
  local ID="$1"
  local IDIR="$RUN_ROOT/runs/mini-swe-agent/$ID"

  # Resume support: skip instances that already produced a valid prediction.
  if valid_preds "$ID"; then
    log "SKIP $ID: valid preds.json already present"
    return 0
  fi

  local PROXY_PID ENV1 ENV2 ENV3
  start_proxy "$IDIR" "$ID" || return 1

  # MAIN lane for the benchmark model; EXTRACT lane for the CURE decision
  # client. Fail loudly when the role2 env file published no trajectory-scoped
  # URL — an unchecked value would only fail closed inside the backend,
  # leaving the episode to run with memory silently off (lane_base_url).
  source "$ENV1"
  local extract_url
  if ! extract_url="$(lane_base_url "$ENV2")"; then
    log "FAIL $ID: no trajectory-scoped EXTRACT-lane URL from the role2 env file (see $IDIR/proxy.log)"
    stop_proxy
    return 1
  fi
  export EXTRACT_BASE_URL="$extract_url"
  export EXTRACT_API_KEY="trajectory-proxy"
  export EXTRACT_MODEL="$MODEL"
  # The QUERY lane (the recall-query rewriter): same wiring as EXTRACT_* above.
  if ! export_query_lane "$ENV3"; then
    log "FAIL $ID: no trajectory-scoped QUERY-lane URL from the role3 env file (see $IDIR/proxy.log)"
    stop_proxy
    return 1
  fi

  log "RUN $ID"
  # The shared env at the bundle root carries mini-swe-agent, litellm[proxy],
  # and both bridge packages (editable), so no --with overlays are needed.
  ( cd "$MINI_SWE_AGENT" && uv run --project "$REPO_ROOT" \
      python -m cure_memory_bridge.run.swebench \
      --subset verified --split test --filter "^${ID}$" --workers 1 --redo-existing \
      --model "$MODEL_NAME" \
      --config swebench.yaml \
      --config environment.pull_timeout=900 \
      --config "$INTEGRATION/configs/memory_defaults.yaml" \
      --config agent.memory.enabled=true \
      --config agent.memory.scope=run \
      --config agent.memory.output_dir="$IDIR" \
      --output "$IDIR" ) >"$IDIR/agent.log" 2>&1
  local rc=$?

  # Close this instance's proxy (SIGINT finalizes the run dir with agent_end).
  stop_proxy

  log "DONE $ID exit=$rc"
  return $rc
}

# run_instance_annotate_lane ID MODULE USER_ID [CONFIG...] — the mem0 and
# tencentdb arms' shared instance body: the two integrations differ only in
# the runner module, the per-run user id, and extra agent.memory.* configs
# (everything layered after the shared fixed set in the same precedence
# order — extras last). One roster proxy per instance whose ROLE2 is the
# zero-model-call MEMORY annotate namespace (extraction runs off-trajectory:
# hosted for mem0, inside the MemoryCore container for tencentdb), plus the
# QUERY rewriter lane.
run_instance_annotate_lane() {
  local ID="$1" MODULE="$2" USER_ID="$3"
  shift 3
  local IDIR="$RUN_ROOT/runs/mini-swe-agent/$ID"

  # Resume support: skip instances that already produced a valid prediction.
  if valid_preds "$ID"; then
    log "SKIP $ID: valid preds.json already present"
    return 0
  fi

  local PROXY_PID ENV1 ENV2 ENV3
  start_proxy "$IDIR" "$ID" || return 1

  # MAIN lane carries the benchmark model; the MEMORY lane makes zero model
  # calls and serves only as the memory-annotate namespace. The memory lane's
  # explicit annotate URL derives from the role2 base URL (lane_base_url) —
  # failing loudly here keeps an unchecked value from surfacing deep inside
  # the backend with the whole arm silently untraced.
  source "$ENV1"
  local base_url
  if ! base_url="$(lane_base_url "$ENV2")"; then
    log "FAIL $ID: no trajectory-scoped memory-annotate URL from the role2 env file (see $IDIR/proxy.log)"
    stop_proxy
    return 1
  fi
  MEMORY_ANNOTATE_MEMORY_URL="${base_url%/v1}/annotate"
  export MEMORY_ANNOTATE_MEMORY_URL
  # The QUERY lane (the recall-query rewriter), exactly as the cure arm's.
  if ! export_query_lane "$ENV3"; then
    log "FAIL $ID: no trajectory-scoped QUERY-lane URL from the role3 env file (see $IDIR/proxy.log)"
    stop_proxy
    return 1
  fi

  log "RUN $ID"
  local -a cfg=(
      --config swebench.yaml
      --config environment.pull_timeout=900
      --config "$INTEGRATION/configs/memory_defaults.yaml"
      --config agent.memory.enabled=true
      --config agent.memory.scope=run
      --config agent.memory.user_id="$USER_ID"
      --config agent.memory.output_dir="$IDIR"
  )
  local extra
  for extra in "$@"; do
    # The mem0 mode is yaml-owned (read_mem0_mode): a --config extra carrying
    # it would override the bridge's mode while the driver still branches on
    # the yaml value — refuse the divergence loudly (extra="forbid" only
    # rejects unknown keys, not a known key overridden this way).
    case "$extra" in
      agent.memory.mode=*)
        log "FATAL $ID: agent.memory.mode is yaml-owned — never pass it via --config extras"
        stop_proxy
        return 1
        ;;
    esac
    cfg+=(--config "$extra")
  done
  # ARM_UV_ARGS: the arm profile's extra `uv run` arguments (the mem0 library
  # mode passes --group mem0-library — a plain `uv run` is inexact but NOT
  # additive, so without it every library-mode instance dies on import mem0).
  ( cd "$MINI_SWE_AGENT" && uv run --project "$REPO_ROOT" ${ARM_UV_ARGS[@]+"${ARM_UV_ARGS[@]}"} \
      python -m "$MODULE" \
      --subset verified --split test --filter "^${ID}$" --workers 1 --redo-existing \
      --model "$MODEL_NAME" \
      "${cfg[@]}" \
      --output "$IDIR" ) >"$IDIR/agent.log" 2>&1
  local rc=$?

  # Close this instance's proxy (SIGINT finalizes the run dir with agent_end).
  stop_proxy

  log "DONE $ID exit=$rc"
  return $rc
}

run_instance_mem0() {
  # All three modes share the annotate-lane body and the per-run user id.
  # platform: the hosted service does the extraction. server: the per-run
  # container does it against the provider upstream (MEM0_SERVER_URL is
  # exported at profile time); its search_timeout is raised because one
  # bridge→server HTTP call hides the embedder round-trips plus the hybrid
  # CPU work — a slow embedding upstream otherwise surfaces as search_errors
  # at the shared 10 s default. library: the in-process engine does it —
  # run_root anchors the per-run store and ARM_UV_ARGS carries the opt-in
  # dependency group.
  local -a extra=()
  case "$MEM0_MODE" in
    server) extra=("agent.memory.search_timeout=30") ;;
    library) extra=("agent.memory.run_root=$RUN_ROOT") ;;
  esac
  if [ "$MEM0_MODE" != "platform" ]; then
    # The yaml's recall_min_score floor is calibrated on the platform's
    # combined 0-1 score; the OSS hybrid score is a different scale (scores
    # are never compared across modes), so server/library arms run with the
    # host-side floor OFF until a calibration pair picks a per-mode value
    # (the --config value parses as JSON: null -> None disables the floor).
    extra+=("agent.memory.recall_min_score=null")
  fi
  run_instance_annotate_lane "$1" mem0_bridge.run.swebench "$MEM0_USER_ID" ${extra[@]+"${extra[@]}"}
}

run_instance_tencentdb() {
  # The MemoryCore container does the extraction against the provider
  # upstream directly; run_root anchors the episode sidecar and the store
  # volume, the embedding fields mirror the generated gateway config.
  run_instance_annotate_lane "$1" tencentdb_bridge.run.swebench "$TDAI_USER_ID" \
    "agent.memory.run_root=$RUN_ROOT" \
    "agent.memory.embedding_provider=$TDAI_EMBEDDING_PROVIDER" \
    "agent.memory.embedding_model=${EMBEDDING_MODEL:-}"
}

run_instance() {
  "run_instance_$INTEGRATION_NAME" "$@"
}

log "MEMORY-ARM-START root=$RUN_ROOT model=$MODEL_NAME integration=$INTEGRATION_NAME${MEM0_MODE:+ mode=$MEM0_MODE}${MEM0_USER_ID:+ user_id=$MEM0_USER_ID}${TDAI_USER_ID:+ user_id=$TDAI_USER_ID embedding=$TDAI_EMBEDDING_PROVIDER}"

# ---- Phase 1: predictions ----
# (the validity probe and the rule-3 resume guard ran up front, before any
# arm side effect — see the top of this script)
FAILED=0
while IFS= read -r ID; do
  [ -z "$ID" ] && continue
  log "=== starting $ID ==="
  run_instance "$ID" || FAILED=1
done < <(read_instance_ids "$RUN_ROOT/instance-ids.txt")
log "PHASE1-COMPLETE failed=$FAILED"

# ---- Phase 2: merge (gate — exits non-zero if any pred is missing/empty) ----
log "=== Phase 2: merge ==="
if ! "$UTILS_DIR/merge-predictions.sh" "$RUN_ROOT" >>"$LOG" 2>&1; then
  log "FATAL: merge failed - not proceeding to eval. Inspect predictions."
  exit 1
fi
log "PHASE2-COMPLETE"

# ---- Phase 3: local Docker evaluation ----
log "=== Phase 3: evaluate ==="
if ! "$UTILS_DIR/run-evaluation.sh" "$RUN_ROOT" >>"$LOG" 2>&1; then
  log "FATAL: evaluation failed"
  exit 1
fi
log "PHASE3-COMPLETE"

# ---- Phase 4: summarize ----
log "=== Phase 4: summarize ==="
SUMMARY_RC=0
"$UTILS_DIR/summarize-report.sh" "$RUN_ROOT" >>"$LOG" 2>&1 || SUMMARY_RC=$?
if [ "$FAILED" -ne 0 ] || [ "$SUMMARY_RC" -ne 0 ]; then
  # Harness-level problems (eval errors, empty patches) or failed instances:
  # the summary in the log says which — never claim ALL-COMPLETE for those.
  log "COMPLETE-WITH-FAILURES failed=$FAILED summarize_rc=$SUMMARY_RC"
  exit 1
fi
log "ALL-COMPLETE"

#!/usr/bin/env bash
# common.sh — shared helpers for the utils/*.sh pipeline scripts.
# This file is meant to be sourced, not executed directly.

die() { echo "error: $*" >&2; exit 1; }

# The bundle root is the parent of the directory holding this file, so the
# scripts work no matter where they are invoked from.
UTILS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$UTILS_DIR/.." && pwd)"
PARENT_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
MINI_SWE_AGENT="$REPO_ROOT/mini-swe-agent"
# SWE-bench and the provider .env are shared with the surrounding workspace:
# prefer an in-bundle copy, else fall back to the sibling in the parent.
SWE_BENCH="$REPO_ROOT/SWE-bench"
[ -d "$SWE_BENCH" ] || SWE_BENCH="$PARENT_ROOT/SWE-bench"
ENV_FILE="$REPO_ROOT/.env"
[ -f "$ENV_FILE" ] || ENV_FILE="$PARENT_ROOT/.env"

# require_repo: fail early if the workspace is incomplete.
require_repo() {
  [ -f "$MINI_SWE_AGENT/pyproject.toml" ] || die "missing mini-swe-agent checkout at $MINI_SWE_AGENT"
  [ -f "$SWE_BENCH/pyproject.toml" ] || die "missing SWE-bench checkout at $SWE_BENCH"
  [ -f "$ENV_FILE" ] || die "missing provider .env at $ENV_FILE"
}

# resolve_run_root [explicit_arg]
# Sets RUN_ROOT_RESOLVED. Priority: argument > $RUN_ROOT > output/LATEST.
resolve_run_root() {
  local explicit="${1:-}"
  if [ -n "$explicit" ]; then
    RUN_ROOT_RESOLVED="$explicit"
  elif [ -n "${RUN_ROOT:-}" ]; then
    RUN_ROOT_RESOLVED="$RUN_ROOT"
  elif [ -f "$REPO_ROOT/output/LATEST" ]; then
    RUN_ROOT_RESOLVED="$REPO_ROOT/output/$(cat "$REPO_ROOT/output/LATEST")"
  else
    die "no run root: pass one as an argument, export RUN_ROOT, or run utils/setup-run.sh first"
  fi
  [ -d "$RUN_ROOT_RESOLVED" ] || die "run root does not exist: $RUN_ROOT_RESOLVED"
  # Canonicalize to an absolute path: phases cd elsewhere (e.g. into the eval
  # dir), so a relative RUN_ROOT would break paths derived from it.
  RUN_ROOT_RESOLVED="$(cd "$RUN_ROOT_RESOLVED" && pwd)"
}

# require_instance_ids: phases that consume the instance list call this after
# resolve_run_root.
require_instance_ids() {
  [ -f "$RUN_ROOT_RESOLVED/instance-ids.txt" ] || die "missing instance list: $RUN_ROOT_RESOLVED/instance-ids.txt"
}

# load_model_env: source the provider .env, validate keys, and set MODEL_NAME.
# MSWEA_MODEL_NAME (if set) is the complete LiteLLM model name and overrides MODEL;
# otherwise MODEL is prefixed with openai/ unless it already has a provider prefix.
load_model_env() {
  # The .env is the sole provider source: clear the ambient names first so a
  # stale shell export (e.g. a personal OPENAI_API_KEY left over in the shell)
  # can never shadow the roster keys — a shadowing key fails auth on every
  # provider call of the run.
  unset API_KEY BASE_URL API MODEL OPENAI_API_KEY OPENAI_BASE_URL
  unset QUERY_MODEL QUERY_API_KEY QUERY_API QUERY_BASE_URL
  set -a
  source "$ENV_FILE"
  set +a
  # Roster form (MODEL/API_KEY/BASE_URL/API, as used by the traj-recorder)
  # carries the same credential under different names; map it so the stock
  # OpenAI-style runners work from the same .env.
  export OPENAI_API_KEY="${OPENAI_API_KEY:-${API_KEY:-}}"
  export OPENAI_BASE_URL="${OPENAI_BASE_URL:-${BASE_URL:-}}"
  export MSWEA_COST_TRACKING="${MSWEA_COST_TRACKING:-ignore_errors}"
  : "${OPENAI_API_KEY:?Set OPENAI_API_KEY (or roster API_KEY) in $ENV_FILE}"
  : "${OPENAI_BASE_URL:?Set OPENAI_BASE_URL (or roster BASE_URL) in $ENV_FILE}"
  if [ -n "${MSWEA_MODEL_NAME:-}" ]; then
    MODEL_NAME="$MSWEA_MODEL_NAME"
  else
    : "${MODEL:?Set MSWEA_MODEL_NAME or MODEL in $ENV_FILE}"
    case "$MODEL" in
      */*) MODEL_NAME="$MODEL" ;;
      *) MODEL_NAME="openai/$MODEL" ;;
    esac
  fi
}

# read_instance_ids IDS_FILE — print one id per line, skipping blank lines,
# comments (# ...), and duplicates (order-preserving), and trimming stray
# whitespace/carriage returns. Trimming matters: the Python consumers strip
# (merge_predictions.py, the driver's validity probe), so an untrimmed id
# would anchor-match zero instances while the bookkeeping sees a clean id.
# Dedupe matters: a repeated id must never run twice in one
# arm — the rerun would recall the memories its first attempt approved.
read_instance_ids() {
  tr -d '\r' < "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -v '^#' | grep -v '^$' | awk '!seen[$0]++'
}

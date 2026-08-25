#!/usr/bin/env bash
# summarize-memory.sh — aggregate every memory.json under a run root into a
# per-episode table (store deltas, injections, cache hits, rewrite outcomes,
# cross-episode recall share). Read-only: no model calls, no Docker.
#
# Usage: summarize-memory.sh [RUN_ROOT]
#   RUN_ROOT  timestamped run directory (default: $RUN_ROOT, else output/LATEST)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_repo
resolve_run_root "${1:-}"

uv run --project "$REPO_ROOT" python "$UTILS_DIR/summarize_memory.py" \
  --run-root "$RUN_ROOT_RESOLVED"

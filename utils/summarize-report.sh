#!/usr/bin/env bash
# summarize-report.sh — Phase 4: print the newest evaluation report for a run root
# and point at per-instance logs when the harness itself failed.
#
# Usage: summarize-report.sh [RUN_ROOT]
#   RUN_ROOT  timestamped run directory (default: $RUN_ROOT, else output/LATEST)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_repo
resolve_run_root "${1:-}"

uv run --project "$REPO_ROOT" python "$UTILS_DIR/summarize_report.py" \
  --run-root "$RUN_ROOT_RESOLVED"

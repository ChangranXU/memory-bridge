#!/usr/bin/env bash
# merge-predictions.sh — Phase 2: validate and merge predictions into merged-preds.json.
#
# Usage: merge-predictions.sh [RUN_ROOT]
#   RUN_ROOT  timestamped run directory (default: $RUN_ROOT, else output/LATEST)
#
# Exits non-zero with a clear message if any instance is missing a preds.json,
# has an empty model_patch, or uses a different model_name_or_path — in that
# case go back to Phase 1 instead of evaluating.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_repo
resolve_run_root "${1:-}"
require_instance_ids

uv run --project "$REPO_ROOT" python "$UTILS_DIR/merge_predictions.py" \
  --run-root "$RUN_ROOT_RESOLVED"

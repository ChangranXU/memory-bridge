#!/usr/bin/env bash
# run-predictions.sh — Phase 1: generate one mini-swe-agent prediction per instance.
#
# Usage: run-predictions.sh [RUN_ROOT]
#   RUN_ROOT  timestamped run directory (default: $RUN_ROOT, else output/LATEST)
#
# Loops over the run root's instance-ids.txt; each invocation stays in batch mode
# but the anchored filter selects exactly one instance and --workers 1 prevents
# parallel cases. Always uses the ephemeral litellm[proxy] overlay via python -m:
# without it the run dies at the first model request (e.g. ModuleNotFoundError:
# No module named 'fastapi'). Finishes by validating every preds.json.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_repo
resolve_run_root "${1:-}"
require_instance_ids
load_model_env

docker info >/dev/null 2>&1 || die "Docker is not available"

echo "run root: $RUN_ROOT_RESOLVED"
echo "model:    $MODEL_NAME"

FAILED=0
cd "$MINI_SWE_AGENT"
while IFS= read -r INSTANCE_ID; do
  echo "=== starting $INSTANCE_ID at $(date -u +%H:%M:%S) ==="
  if ! uv run --project "$MINI_SWE_AGENT" --with 'litellm[proxy]' \
      python -m minisweagent.run.utilities.mini_extra swebench \
      --subset verified \
      --split test \
      --filter "^${INSTANCE_ID}$" \
      --workers 1 \
      --redo-existing \
      --model "$MODEL_NAME" \
      --config swebench.yaml \
      --config environment.pull_timeout=900 \
      --output "$RUN_ROOT_RESOLVED/runs/mini-swe-agent/${INSTANCE_ID}"; then
    echo "WARNING: prediction run failed for $INSTANCE_ID" >&2
    FAILED=1
  fi
  echo "=== finished $INSTANCE_ID at $(date -u +%H:%M:%S) ==="
done < <(read_instance_ids "$RUN_ROOT_RESOLVED/instance-ids.txt")

echo "all instances processed; validating predictions..."
uv run --project "$REPO_ROOT" python "$UTILS_DIR/merge_predictions.py" \
  --check --run-root "$RUN_ROOT_RESOLVED"

if [ "$FAILED" -ne 0 ]; then
  die "one or more prediction runs failed; inspect the logs above and rerun"
fi

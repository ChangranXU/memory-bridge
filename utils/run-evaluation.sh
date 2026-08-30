#!/usr/bin/env bash
# run-evaluation.sh — Phase 3: grade merged predictions with the local SWE-bench
# Docker harness.
#
# Usage: run-evaluation.sh [RUN_ROOT]
#   RUN_ROOT  timestamped run directory (default: $RUN_ROOT, else output/LATEST)
#
# The harness writes logs/run_evaluation/<run_id>/... and the final
# <model>.<run_id>.json report relative to the current working directory, so this
# script cds into the timestamped evaluation directory first, and points
# PYTHONPATH at the SWE-bench checkout so source-tree resources are used.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_repo
resolve_run_root "${1:-}"
require_instance_ids

docker info >/dev/null 2>&1 || die "Docker is not available"

PREDS="$RUN_ROOT_RESOLVED/runs/mini-swe-agent/merged-preds.json"
[ -f "$PREDS" ] || die "missing merged predictions: $PREDS (run utils/merge-predictions.sh first)"

RUN_ID="$(basename "$RUN_ROOT_RESOLVED")-local"
EVAL_DIR="$RUN_ROOT_RESOLVED/local-eval/$RUN_ID"
mkdir -p "$EVAL_DIR"

INSTANCE_IDS=()
while IFS= read -r INSTANCE_ID; do
  INSTANCE_IDS+=("$INSTANCE_ID")
done < <(read_instance_ids "$RUN_ROOT_RESOLVED/instance-ids.txt")
# An empty list under set -u dies cryptically on "${INSTANCE_IDS[@]}" with
# macOS's stock bash 3.2 — fail here with the reason instead.
[ "${#INSTANCE_IDS[@]}" -gt 0 ] || die "no instance ids in $RUN_ROOT_RESOLVED/instance-ids.txt"

cd "$EVAL_DIR"
PYTHONPATH="$SWE_BENCH" \
uv run --project "$SWE_BENCH" python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Verified \
  --split test \
  --predictions_path "$PREDS" \
  --instance_ids "${INSTANCE_IDS[@]}" \
  --max_workers 1 \
  --run_id "$RUN_ID" \
  --cache_level instance \
  --clean True \
  --timeout 900

#!/usr/bin/env bash
# setup-run.sh — Phase 0: create a timestamped run root from an instance-ids file.
#
# Usage: setup-run.sh [IDS_FILE] [NAME]
#   IDS_FILE  one instance id per line (default: <repo>/instance-ids.txt)
#   NAME      run name prefix (default: the IDS_FILE basename without extension)
#
# Creates output/<NAME>-<utc-timestamp>/ with runs/mini-swe-agent/, local-eval/,
# and instance-ids.txt, then records the new run root in output/LATEST so the
# later phases can find it without arguments.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_repo

IDS_FILE="${1:-$REPO_ROOT/instance-ids.txt}"
[ -f "$IDS_FILE" ] || die "instance ids file not found: $IDS_FILE"
NAME="${2:-$(basename "$IDS_FILE" .txt)}"
# NAME becomes a path segment under output/; keep it a plain safe name so the
# mkdir/cleanup below can never escape the output directory.
case "$NAME" in
  ""|*[!A-Za-z0-9._-]*) die "invalid NAME: ${NAME:-<empty>} (allowed: letters, digits, . _ -)" ;;
esac

RUN_UID="${NAME}-$(date -u +%Y%m%d-%H%M%Sz)"
RUN_ROOT_NEW="$REPO_ROOT/output/$RUN_UID"
# The timestamp resolves to one second: two invocations with the same NAME
# inside one second must not silently share a run root (rule 4's fresh-root
# invariant) — plain mkdir fails on the existing directory.
mkdir -p "$REPO_ROOT/output"
if ! mkdir "$RUN_ROOT_NEW"; then
  die "run root already exists: $RUN_ROOT_NEW (same NAME within one second — pick another NAME)"
fi
mkdir -p "$RUN_ROOT_NEW/runs/mini-swe-agent" "$RUN_ROOT_NEW/local-eval"

if ! read_instance_ids "$IDS_FILE" > "$RUN_ROOT_NEW/instance-ids.txt" || [ ! -s "$RUN_ROOT_NEW/instance-ids.txt" ]; then
  case "$RUN_ROOT_NEW" in "$REPO_ROOT"/output/*) rm -rf "$RUN_ROOT_NEW" ;; esac
  die "no instance ids found in $IDS_FILE"
fi

echo "$RUN_UID" > "$REPO_ROOT/output/LATEST"
echo "run root: $RUN_ROOT_NEW"
echo "instances:"
cat "$RUN_ROOT_NEW/instance-ids.txt"

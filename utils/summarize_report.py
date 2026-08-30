"""Summarize the newest SWE-bench evaluation report under a run root.

Prints the report counters and id lists, then a verdict: unresolved instances
are model misses (valid outcomes), while error / incomplete / empty-patch
instances mean the evaluation infrastructure failed and the per-instance logs
must be inspected before judging model quality.

Usage:
    python summarize_report.py --run-root PATH
"""

import argparse
import json
import pathlib
import sys

COUNTERS = [
    "total_instances",
    "submitted_instances",
    "completed_instances",
    "resolved_instances",
    "unresolved_instances",
    "empty_patch_instances",
    "error_instances",
]
ID_LISTS = ["resolved_ids", "unresolved_ids", "incomplete_ids", "empty_patch_ids", "error_ids"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=pathlib.Path)
    args = parser.parse_args()

    root: pathlib.Path = args.run_root
    # The harness report is <model>.<run_id>.json inside local-eval/<run_id>/ —
    # matching the parent dir's name keeps a stray JSON dropped beside the
    # reports (a copied older report, scratch notes) from shadowing them.
    reports = sorted(
        (p for p in root.glob("local-eval/*/*.json") if p.stem.endswith(f".{p.parent.name}")),
        key=lambda p: p.stat().st_mtime,
    )
    if not reports:
        raise SystemExit(
            f"no evaluation report found under {root}/local-eval "
            "(run utils/run-evaluation.sh first)"
        )
    report_path = reports[-1]
    try:
        report = json.loads(report_path.read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"unreadable evaluation report {report_path}: {e}")
    if not isinstance(report, dict) or not isinstance(report.get("total_instances"), int):
        # The name-shape filter alone can still crown a stray same-suffix JSON
        # dropped INSIDE the run dir (scratch notes) — without this gate it
        # would print all-None counters under a false-success verdict.
        raise SystemExit(
            f"{report_path} is not a SWE-bench harness report (no integer total_instances) — "
            "a stray same-suffix JSON is shadowing the real report; remove it"
        )

    print(f"report: {report_path}")
    for field in COUNTERS:
        print(f"  {field}: {report.get(field)}")
    for key in ID_LISTS:
        ids = report.get(key) or []
        if ids:
            print(f"  {key}: {', '.join(ids)}")

    problems = (
        (report.get("error_instances") or 0)
        + len(report.get("incomplete_ids") or [])
        + (report.get("empty_patch_instances") or 0)
    )
    if problems:
        eval_dir = report_path.parent
        logs = eval_dir / "logs" / "run_evaluation" / eval_dir.name
        print(
            "ATTENTION: harness-level problems detected; inspect the per-instance "
            f"logs under {logs}/<model>/<instance_id>/ before judging model quality"
        )
        return 1
    print(
        "ok: evaluation completed without harness errors; any unresolved ids are "
        "model misses, not infrastructure failures"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

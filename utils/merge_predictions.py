"""Merge per-instance mini-swe-agent predictions into one harness-ready file.

Every instance listed in the run root's instance-ids.txt must have
runs/mini-swe-agent/<instance_id>/preds.json with a non-empty model_patch, and
all predictions must share one model_name_or_path (a SWE-bench harness
requirement for a single run).

Usage:
    python merge_predictions.py --run-root PATH [--ids-file PATH] [--output PATH] [--check]

--check validates without writing the merged file (used as the Phase 1 checkpoint).
"""

import argparse
import json
import pathlib
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=pathlib.Path)
    parser.add_argument("--ids-file", type=pathlib.Path, default=None,
                        help="default: <run-root>/instance-ids.txt")
    parser.add_argument("--output", type=pathlib.Path, default=None,
                        help="default: <run-root>/runs/mini-swe-agent/merged-preds.json")
    parser.add_argument("--check", action="store_true",
                        help="validate only; do not write the merged file")
    args = parser.parse_args()

    root: pathlib.Path = args.run_root
    ids_file: pathlib.Path = args.ids_file or root / "instance-ids.txt"
    base = root / "runs" / "mini-swe-agent"
    output: pathlib.Path = args.output or base / "merged-preds.json"

    if not ids_file.exists():
        raise SystemExit(f"missing instance list: {ids_file}")
    # The ids-file format contract (utils/common.sh read_instance_ids): blank
    # lines and comments (# ...) are skipped, duplicates dropped
    # order-preserving — a hand-edited run-root list must gate on the same set
    # the bash phases ran.
    instances = []
    for line in ids_file.read_text().splitlines():
        instance_id = line.strip()
        if not instance_id or instance_id.startswith("#") or instance_id in instances:
            continue
        instances.append(instance_id)
    combined = {}
    models = set()
    missing = []
    empty = []
    for instance_id in instances:
        pred_path = base / instance_id / "preds.json"
        if not pred_path.exists():
            missing.append(instance_id)
            continue
        try:
            payload = json.loads(pred_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"unreadable preds.json for {instance_id}: {e}")
        if not isinstance(payload, dict):
            raise SystemExit(f"preds.json for {instance_id} is not a JSON object")
        pred = payload.get(instance_id)
        if pred is None:
            missing.append(instance_id)
            continue
        if not isinstance(pred, dict):
            raise SystemExit(f"preds.json entry for {instance_id} is not a JSON object")
        patch = pred.get("model_patch")
        if not isinstance(patch, str) or not patch.strip():
            empty.append(instance_id)
            continue
        model = pred.get("model_name_or_path")
        if not isinstance(model, str) or not model.strip():
            # A null/missing model name would otherwise pass the single-model
            # gate uniformly (models == {None}) and surface inside the Docker
            # harness — fail here, at the gate.
            raise SystemExit(f"preds.json for {instance_id} carries no usable model_name_or_path")
        combined[instance_id] = {
            "instance_id": instance_id,
            "model_patch": patch,
            "model_name_or_path": model,
        }
        models.add(model)

    if missing:
        raise SystemExit("missing predictions for: " + ", ".join(missing))
    if empty:
        raise SystemExit(
            "empty model_patch for: " + ", ".join(empty)
            + "; inspect trajectories and rerun before local evaluation"
        )
    if len(combined) != len(instances):
        raise SystemExit(f"expected {len(instances)} predictions, got {len(combined)}")
    if len(models) != 1:
        raise SystemExit(
            f"local SWE-bench evaluation requires one model_name_or_path, got {sorted(models)}"
        )

    model = next(iter(models))
    if args.check:
        print(f"ok: {len(combined)} predictions present, non-empty, single model ({model})")
        return 0
    output.write_text(json.dumps(combined, indent=2) + "\n")
    print(f"wrote {output} ({len(combined)} predictions, model {model})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

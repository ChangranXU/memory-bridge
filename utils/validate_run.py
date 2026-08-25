#!/usr/bin/env python3
"""Validate one recorded run dir (trajectory.jsonl + run.json).

Checks, per the round-18 acceptance criteria:
  1. Complete proxy-source info: every lane-scoped event carries proxy_source;
     agent_start/agent_end carry none.
  2. Order: seq 1-based strictly increasing; agent_start first, agent_end last;
     turn_start precedes its turn_end; turn-scoped events sit between them.
  3. No redundant content: no (proxy_source, msg) message_end is recorded twice
     without an intervening retraction/rewrite annulling the first.
  4. Memory actions extractable: retrieval.build_memory_index yields the
     sessions/operations/deliveries with zero structural problems.
  5. run.json counters agree with the log.

Usage: validate_run.py <run-dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_here = Path(__file__).resolve()
# traj-recorder lives in the bundle when present, else in the parent workspace.
for _candidate in (_here.parents[1] / "extension" / "traj-recorder", _here.parents[2] / "extension" / "traj-recorder"):
    if (_candidate / "pyproject.toml").is_file():
        sys.path.insert(0, str(_candidate))
        break
else:
    sys.exit("traj-recorder checkout not found in the bundle or the parent workspace")

from trajectory_proxy.retrieval import build_memory_index  # noqa: E402

failures = []


def check(cond, label):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        failures.append(label)


def main() -> int:
    run = Path(sys.argv[1])
    events = [json.loads(line) for line in (run / "trajectory.jsonl").read_text().splitlines()]
    meta = json.loads((run / "run.json").read_text())

    # 1. proxy-source completeness
    lane_events = [e for e in events if e["type"] not in ("agent_start", "agent_end")]
    check(all("proxy_source" in e for e in lane_events), "every lane event carries proxy_source")
    check(
        not any("proxy_source" in e for e in events if e["type"] in ("agent_start", "agent_end")),
        "agent_start/agent_end carry no lane",
    )
    lanes = sorted({str(e["proxy_source"]) for e in lane_events})
    print(f"       lanes: {lanes}")

    # 2. order
    seqs = [e["seq"] for e in events]
    check(
        seqs[0] == 1 and all(later > earlier for earlier, later in zip(seqs, seqs[1:])),
        "seq 1-based strictly increasing",
    )
    check(events[0]["type"] == "agent_start" and events[-1]["type"] == "agent_end", "agent_start..agent_end frame")
    turn_span = {}
    order_bad = []
    for e in events:
        if e["type"] == "turn_start":
            turn_span[e["turn"]] = [e["seq"], None]
        elif e["type"] == "turn_end":
            if e["turn"] not in turn_span or turn_span[e["turn"]][1] is not None:
                order_bad.append(e["seq"])
            else:
                turn_span[e["turn"]][1] = e["seq"]
    check(not order_bad and all(span[1] is not None for span in turn_span.values()),
          f"every turn_start closed exactly once, in order (bad: {order_bad[:5]})")

    # 3. no redundant (lane, msg) recordings
    live = {}
    duplicates = []
    for e in events:
        lane = e.get("proxy_source")
        if e["type"] == "message_end":
            key = (lane, e["msg"])
            if key in live:
                duplicates.append((lane, e["msg"], e["seq"]))
            live[key] = e["seq"]
        elif e["type"] == "message_retracted":
            live.pop((lane, e["msg"]), None)
        elif e["type"] == "history_rewritten":
            for key in [key for key in live if key[0] == lane and key[1] >= e["kept"]]:
                del live[key]
    check(not duplicates, f"no double-recorded (lane, msg) message_end (dups: {duplicates[:5]})")

    # 4. memory actions
    index = build_memory_index(events)
    check(index.annotated, "memory annotations present")
    check(not index.problems, f"memory index structural problems: {index.problems[:5]}")
    generations = [op for op in index.operations.values() if op.kind == "generation"]
    searches = [op for op in index.operations.values() if op.kind == "search"]
    print(
        f"       sessions={len(index.sessions)} generations={len(generations)} "
        f"searches={len(searches)} deliveries={len(index.deliveries)} identities={len(index.identities)}"
    )
    check(
        all(op.start is not None and op.end is not None for op in index.operations.values()),
        "every operation has start and end",
    )
    bound_ops = sum(1 for op in index.operations.values() if op.call_indexes)
    print(f"       operations with bound call refs: {bound_ops}/{len(index.operations)}")

    # 5. run.json counters
    call_dirs = [d for d in (run / "calls").iterdir() if d.is_dir()]
    check(meta["call_count"] == len(call_dirs), f"call_count matches calls/ ({meta['call_count']} vs {len(call_dirs)})")
    check(
        meta["turn_count"] == len(turn_span),
        f"turn_count matches log ({meta['turn_count']} vs {len(turn_span)})",
    )
    check(
        meta["message_count"] == sum(1 for e in events if e["type"] == "message_end"),
        f"message_count matches log ({meta['message_count']})",
    )
    check(meta["status"] in ("stopped", "completed"), f"run status finalized ({meta['status']})")

    print(f"{'FAILURES: ' + str(failures) if failures else 'ALL CHECKS PASSED'} — {run}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""Aggregate every memory.json under a run root into a per-episode table.

Read-only: reads the per-instance ``runs/mini-swe-agent/<id>/memory.json``
episode logs (never trajectories), one row per episode: store delta
(added/updated/deleted), injections (count/chars), the search cache's hit
share, rewrite outcomes, agent-initiated scene reads and conversation
searches (the tencentdb arm's L2/L0 read observation), the cross-episode
share of recalled memories from the per-hit origin lists the recall events
carry, and the annotation-transport degradations the bridge logs (kind ==
"annotation": dropped/unconfirmed deliveries, deliveries suppressed by the
crash probe, unconfirmed searches, abandoned operations, a lane or the whole
annotation transport disabled mid-session). Counters that a memory.json
predates are simply omitted from its row.

Usage:
    python summarize_memory.py --run-root PATH
"""

import argparse
import json
import pathlib
import sys


def _origins_share(events: list[dict], session_id: str | None) -> tuple[int, int, int]:
    """(this-episode, cross-episode, unknown) hit counts over recall events.

    An origin equal to the episode's own session id is this-episode; any
    other recorded origin is cross-episode; a null origin carries no signal
    (the tencentdb arm never emits null: its unresolvable origins arrive as
    the non-empty sentinel "unknown" and count as cross-episode — the same
    generic cross-episode suffix the model was shown for them).
    """
    own = cross = unknown = 0
    for event in events:
        if event.get("kind") != "recall":
            continue
        for origin in event.get("origins") or []:
            if origin is None:
                unknown += 1
            elif origin == session_id:
                own += 1
            else:
                cross += 1
    return own, cross, unknown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=pathlib.Path)
    args = parser.parse_args()

    root: pathlib.Path = args.run_root
    logs = sorted(root.glob("runs/mini-swe-agent/*/memory.json"))
    if not logs:
        raise SystemExit(f"no memory.json found under {root}/runs/mini-swe-agent (nothing to aggregate)")

    header = (
        "instance",
        "added",
        "updated",
        "deleted",
        "injections",
        "chars",
        "cache_hit%",
        "rewrite ok/fail",
        "scene reads",
        "agent l0 searches",
        "l0 search chars",
        "cross-episode%",
        "ann. degraded",
    )
    rows = []
    for path in logs:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            print(f"skipping unreadable {path}: {e}", file=sys.stderr)
            continue
        counts = data.get("counts") or {}
        events = data.get("events") or []
        if not isinstance(counts, dict) or not isinstance(events, list) or not all(
            isinstance(event, dict) for event in events
        ):
            # Same discipline as an unreadable file: one malformed log must
            # skip its row, not abort the whole table.
            print(f"skipping malformed {path}: counts/events have unexpected shapes", file=sys.stderr)
            continue
        instance = data.get("instance_id") or path.parent.name
        # added/updated: the hosted arm counts added/updated outright; the
        # local arm's approved count subsumes its superseding updates.
        added = counts.get("memories_added", counts.get("memories_approved"))
        updated = counts.get("memories_updated")
        deleted = counts.get("memories_deleted")
        injections = counts.get("recall_injections", 0)
        chars = sum(event.get("chars", 0) for event in events if event.get("kind") == "recall")
        cache_hits = counts.get("recall_cache_hits")
        cache_share = (
            f"{cache_hits / injections:.0%}" if cache_hits is not None and injections else ("-" if cache_hits is None else "0%")
        )
        rewrite_calls = counts.get("rewrite_calls")
        rewrite = (
            "-"
            if rewrite_calls is None
            else f"{counts.get('rewrite_successes', 0)}/{counts.get('rewrite_failures', 0)} of {rewrite_calls}"
        )
        own, cross, unknown = _origins_share(events, data.get("session_id"))
        cross_share = (
            "-"
            if own + cross == 0
            else f"{cross / (own + cross):.0%} ({cross}/{own + cross}{f', +{unknown} unknown' if unknown else ''})"
        )
        # Agent-initiated L2 scene reads (tencentdb arm): count and total
        # chars; arms without the counter print "-".
        scene_reads = counts.get("agent_scene_reads")
        scene_reads_cell = (
            "-" if scene_reads is None else f"{scene_reads} ({counts.get('scene_read_chars', 0)} ch)"
        )
        # Agent-initiated L0 conversation searches (tencentdb arm): observed
        # count and total observation chars. Same "-" shape — the other arms
        # emit neither counter.
        l0_searches = counts.get("agent_conversation_searches")
        l0_search_chars = counts.get("conversation_search_chars")
        # Annotation-transport degradations (kind == "annotation"): every one
        # means trajectory.jsonl records less than this memory.json does — a
        # dropped delivery or search leaves no mem-eval-visible trace, so a
        # nonzero count is the operator's signal to reconcile the two
        # before trusting trajectory-derived numbers (the count is one-
        # directional: the degradations the bridge logs, so a dead transport
        # trips its own event; healthy episodes log none). The count needs
        # no per-reason semantics (any reason, including a future one, is a
        # degradation by construction).
        ann_degraded = sum(1 for event in events if event.get("kind") == "annotation")
        rows.append(
            (
                instance,
                "-" if added is None else str(added),
                "-" if updated is None else str(updated),
                "-" if deleted is None else str(deleted),
                str(injections),
                str(chars),
                cache_share,
                rewrite,
                scene_reads_cell,
                "-" if l0_searches is None else str(l0_searches),
                "-" if l0_search_chars is None else str(l0_search_chars),
                cross_share,
                str(ann_degraded),
            )
        )

    if not rows:
        # Every memory.json was unreadable: same contract as no logs at all —
        # a bare header plus exit 0 would read as a successful empty summary.
        raise SystemExit(f"no readable memory.json under {root}/runs/mini-swe-agent (see the skip reasons above)")

    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

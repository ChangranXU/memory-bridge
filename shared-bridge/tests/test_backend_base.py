"""Fake-base suite for ``shared_bridge.backend``: the minimal reference
integration (conftest's FakeBackend) pins the skeleton's behavior —
start/finalize failure envelopes, artifact shapes on the failure path, the
record/extract/recall loops, and the counter-ownership split. The traced
protocol surface is pinned in test_trace.py against the capture server.
"""

import json
import logging
import re
from pathlib import Path

import pytest

from fake_integration import FakeBackend, _config, _started

from shared_bridge.backend import (
    RECALL_LINE_TRUNCATION,
    TRUNCATION_MARKER,
    _BackendUnavailable,
    _new_session_id,
    _repo_of,
    _truncate,
)
from shared_bridge.prompts import (
    EXTRACTION_GUIDELINES_DEFAULT,
    RECALL_POLICY_DEFAULT,
    extraction_episode_context,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_truncate_caps_including_marker():
    assert _truncate("abc", 10) == "abc"
    out = _truncate("x" * 50, 30)
    assert len(out) == 30 and out.endswith(TRUNCATION_MARKER)
    assert _truncate("x" * 50, 5) == TRUNCATION_MARKER[:5]


def test_new_session_id_is_instance_prefixed_and_unique():
    first, second = _new_session_id("inst"), _new_session_id("inst")
    assert first != second
    for session_id in (first, second):
        assert session_id.startswith("inst-")
        suffix = session_id[len("inst-"):]
        assert len(suffix) == 32 and all(c in "0123456789abcdef" for c in suffix)


# ---------------------------------------------------------------------------
# Start: failure-path artifact pins
# ---------------------------------------------------------------------------
def test_failed_start_writes_full_literal_and_null_session(tmp_path):
    backend = FakeBackend(_config(tmp_path))
    backend.fail_resolve = True
    backend.start()  # contained
    assert backend._available is False
    data = json.loads((tmp_path / "memory.json").read_text())
    assert data["available"] is False
    assert data["session_id"] is None
    assert data["settings"] == {"api_base_url": "", "strict": False}  # the full literal
    assert data["counts"] == {
        "messages_recorded": 0,
        "extraction_calls": 0,
        "extraction_errors": 0,
        "recall_injections": 0,
        "backend_errors": 0,
        "search_errors": 0,
        "recall_cache_hits": 0,
        "rewrite_calls": 0,
        "rewrite_successes": 0,
        "rewrite_failures": 0,
        "widgets": 0,
    }
    assert [event["kind"] for event in data["events"]] == ["error"]
    assert data["events"][0]["op"] == "start"


def test_failed_restart_writes_null_session_again(tmp_path):
    """The reset nulls ``_session_id``, so the pin holds for re-starts too."""
    backend = _started(tmp_path)
    assert backend._session_id is not None
    backend.fail_resolve = True
    backend.start()
    assert backend._available is False
    data = json.loads((tmp_path / "memory.json").read_text())
    assert data["available"] is False
    assert data["session_id"] is None


def test_restart_closes_previous_handle(tmp_path):
    """A successful re-start must not leak the prior episode's handle."""
    backend = _started(tmp_path)
    first = backend.system
    backend.start()
    assert backend._available is True
    assert backend.system is not first
    assert first.closed is True


def test_strict_start_failure_raises(tmp_path):
    backend = FakeBackend(_config(tmp_path, strict=True))
    backend.fail_resolve = True
    with pytest.raises(_BackendUnavailable, match="fake settings missing"):
        backend.start()
    assert backend._available is False


# ---------------------------------------------------------------------------
# Record loop
# ---------------------------------------------------------------------------
def test_record_defaults_passthrough_roles_and_filtering(tmp_path):
    backend = _started(tmp_path)
    backend.record(
        [
            {"role": "tool", "content": "kept verbatim"},
            "not-a-dict",
            {"role": "user", "content": "skip me", "extra": {"transient_recall": True}},
        ],
        step=3,
    )
    assert backend.system.pending == [{"role": "tool", "content": "kept verbatim", "step": 3}]
    assert backend._counts["messages_recorded"] == 1
    assert backend._counts["widgets"] == 1


def test_record_fail_closed_and_strict_raise(tmp_path):
    backend = _started(tmp_path)
    backend.system.store_error = RuntimeError("store boom")
    backend.record([{"role": "user", "content": "x"}], step=1)  # contained
    assert backend._counts["backend_errors"] == 1
    assert backend._events[-1]["op"] == "record"

    strict = _started(tmp_path / "strict", strict=True)
    strict.system.store_error = RuntimeError("store boom")
    with pytest.raises(RuntimeError, match="store boom"):
        strict.record([{"role": "user", "content": "x"}], step=1)


def test_record_unavailable_is_a_no_op(tmp_path):
    backend = FakeBackend(_config(tmp_path))  # never started
    backend.record([{"role": "user", "content": "x"}], step=1)
    assert backend._counts == {}


# ---------------------------------------------------------------------------
# Extraction: scheduling, shell, breaker
# ---------------------------------------------------------------------------
def test_high_water_catchup(tmp_path):
    backend = _started(tmp_path, extract_every_n_steps=10)
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.maybe_extract(9)  # bucket 0: no fire
    assert backend._counts["extraction_calls"] == 0
    backend.maybe_extract(15)  # bucket 1 fires late (missed boundary serviced)
    assert backend._counts["extraction_calls"] == 1
    backend.maybe_extract(19)  # same bucket: no refire
    assert backend._counts["extraction_calls"] == 1
    backend.record([{"role": "user", "content": "m2"}], step=19)  # the first extraction drained the buffer
    backend.maybe_extract(20)  # bucket 2
    assert backend._counts["extraction_calls"] == 2


def test_empty_tick_is_not_counted(tmp_path):
    backend = _started(tmp_path, extract_every_n_steps=1)
    backend.maybe_extract(1)  # nothing pending: the readiness guard returns first
    assert backend._counts["extraction_calls"] == 0
    assert [e for e in backend._events if e["kind"] == "extraction"] == []


def test_hard_failure_counted_once_and_breaker_trips(tmp_path):
    backend = _started(tmp_path, extract_every_n_steps=1, extract_max_consecutive_errors=2)
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.system.extract_error = RuntimeError("extract boom")
    backend.maybe_extract(1)
    assert backend._counts["extraction_calls"] == 1
    assert backend._counts["extraction_errors"] == 1
    assert backend._consecutive_errors == 1
    assert len([e for e in backend._events if e.get("op") == "extract"]) == 1  # exactly once

    backend.maybe_extract(2)
    assert backend._extract_breaker is True
    assert any(e.get("op") == "extract_breaker" for e in backend._events)

    backend.maybe_extract(3)  # breaker: periodic ticks disabled
    assert backend._counts["extraction_calls"] == 2
    backend._extract("final")  # the final flush bypasses the breaker by design
    assert backend._counts["extraction_calls"] == 3


def test_strict_extraction_failure_raises(tmp_path):
    backend = _started(tmp_path, extract_every_n_steps=1, strict=True)
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.system.extract_error = RuntimeError("extract boom")
    with pytest.raises(RuntimeError, match="extract boom"):
        backend.maybe_extract(1)


# ---------------------------------------------------------------------------
# Recall loop
# ---------------------------------------------------------------------------
def test_recall_success_payload(tmp_path):
    backend = _started(tmp_path)
    backend.set_task("task")
    backend.system.hits = ["one", "two"]
    recall = backend.recall_context(planned_step=3)
    header = f"{RECALL_POLICY_DEFAULT}\n\n## Fake Memories"
    assert recall["content"] == f"{header}\n- one\n- two"
    assert recall["n_memories"] == 2
    assert recall["chars"] == len(recall["content"])
    assert recall["memories"] == ["one", "two"]  # the native hit objects
    assert recall["origins"] == [None, None]  # no origin signal scripted


def test_recall_guards_return_none(tmp_path):
    never_started = FakeBackend(_config(tmp_path / "down"))
    assert never_started.recall_context() is None

    off = _started(tmp_path / "off", inject_recall=False)
    off.set_task("t")
    off.system.hits = ["x"]
    assert off.recall_context() is None

    no_task = _started(tmp_path / "no-task")
    no_task.system.hits = ["x"]
    assert no_task.recall_context() is None


def test_recall_rank_then_fill_edges(tmp_path):
    empty = _started(tmp_path / "empty")
    empty.set_task("t")
    assert empty.recall_context() is None  # empty hit set

    # A budget too small for any line injects nothing at all.
    tight = _started(tmp_path / "tight", max_total_recall_chars=len("- one") - 1)
    tight.set_task("t")
    tight.system.hits = ["one"]
    assert tight.recall_context() is None

    # Empty lines are skipped without consuming the budget.
    skip = _started(tmp_path / "skip")
    skip.set_task("t")
    skip.system.hits = ["", "real"]
    recall = skip.recall_context()
    assert recall["n_memories"] == 1
    assert recall["content"] == f"{RECALL_POLICY_DEFAULT}\n\n## Fake Memories\n- real"


def test_recall_budget_counts_lines_only_not_the_header(tmp_path):
    """max_total_recall_chars bounds the rendered memory lines alone: the header
    (however long the shared policy grows) never consumes the memory budget."""
    backend = _started(tmp_path, max_total_recall_chars=len("- one"))
    backend.set_task("t")
    backend.system.hits = ["one", "two"]
    recall = backend.recall_context()
    header = f"{RECALL_POLICY_DEFAULT}\n\n## Fake Memories"
    assert len(header) > backend.config.max_total_recall_chars  # the header alone exceeds the cap
    assert recall is not None
    assert recall["content"] == f"{header}\n- one"  # exactly one line fits the lines budget
    assert recall["chars"] == len(recall["content"])  # chars still means "what was placed"


def test_recall_skips_an_unfittable_rank_and_walks_on(tmp_path):
    """An over-budget line with less than the 40-char floor remaining is
    skipped, not a stop-all — and the walk CONTINUES (a recorded divergence
    from native's break-on-exhaustion): a single oversized memory must not
    starve the smaller below-rank lines, nor get the empty result cached."""
    first = "f" * 30  # "- " + 30 = 32 chars
    big = "x" * 200  # far over any remaining budget
    small = "y" * 5  # "- " + 5 = 7 chars
    # After "first", 39 chars remain: below the floor, so "big" is skipped
    # whole — and "small" still fits that remainder.
    backend = _started(tmp_path, max_total_recall_chars=32 + 1 + 39)
    backend.set_task("t")
    # The oversized memory ranks BETWEEN the two fitting ones.
    backend.system.hits = [first, big, small]
    recall = backend.recall_context()
    header = f"{RECALL_POLICY_DEFAULT}\n\n## Fake Memories"
    assert recall is not None  # not starved into a cached empty answer
    assert recall["memories"] == [first, small]
    assert recall["content"] == f"{header}\n- {first}\n- {small}"
    assert recall["n_memories"] == 2


def test_recall_truncates_to_fit_at_the_floor_boundary(tmp_path):
    """At exactly the 40-char floor the over-budget line is truncated into the
    remaining budget (suffix-bearing); one char below the floor it is skipped
    whole. Truncation consumes the rest of the budget, so nothing ranked below
    the truncated line delivers."""
    big = "x" * 200
    small = "y" * 5
    fit = _started(tmp_path / "fit", max_total_recall_chars=40)
    fit.set_task("t")
    fit.system.hits = [big, small]
    recall = fit.recall_context()
    header = f"{RECALL_POLICY_DEFAULT}\n\n## Fake Memories"
    line = recall["content"][len(header) + 1 :]
    assert line == "- " + "x" * 23 + RECALL_LINE_TRUNCATION
    assert len(line) == 40
    assert recall["memories"] == [big]  # the truncation ate the whole budget
    assert recall["n_memories"] == 1

    skip = _started(tmp_path / "skip", max_total_recall_chars=39)
    skip.set_task("t")
    skip.system.hits = [big, small]
    recall = skip.recall_context()
    assert recall["memories"] == [small]  # big skipped below the floor; the walk continues
    assert recall["content"] == f"{header}\n- {small}"


def test_max_chars_per_memory_truncates_content_and_provenance_together(tmp_path):
    """The per-memory cap truncates the COMPOSED line (content + provenance
    suffix), suffix-bearing: the provenance is part of the line, so it is cut
    together with its content — never left standing alone."""
    backend = _started(tmp_path, max_chars_per_memory=30)
    backend.set_task("t")
    long_hit = "a" * 100
    backend.system.hits = [long_hit, "short"]
    backend.system.origins = {long_hit: backend._session_id}
    recall = backend.recall_context()
    header = f"{RECALL_POLICY_DEFAULT}\n\n## Fake Memories"
    lines = recall["content"][len(header) + 1 :].splitlines()
    assert lines[0] == "- " + "a" * 13 + RECALL_LINE_TRUNCATION  # 30 chars, "(from this episode)" cut too
    assert len(lines[0]) == 30
    assert lines[1] == "- short"  # under the cap: whole
    assert recall["n_memories"] == 2


def test_max_chars_per_memory_below_the_marker_length_is_honored_exactly(tmp_path):
    """No silent floor under the per-memory cap: a configured cap too small to
    fit the truncation marker takes a plain cut, so a delivered line never
    exceeds the configured cap even below len(RECALL_LINE_TRUNCATION)."""
    backend = _started(tmp_path, max_chars_per_memory=5)
    backend.set_task("t")
    backend.system.hits = ["a" * 12]  # "- " + 12 = 14 chars over the 5-char cap
    recall = backend.recall_context()
    header = f"{RECALL_POLICY_DEFAULT}\n\n## Fake Memories"
    lines = recall["content"][len(header) + 1 :].splitlines()
    assert lines[0] == "- aaa"  # exactly the cap, plain cut: the marker cannot fit
    assert RECALL_LINE_TRUNCATION not in lines[0]


def test_max_total_recall_chars_zero_disables_the_total_bound(tmp_path):
    """0 disables the total budget: every selected line delivers (per-memory
    truncation only); max_memories stays the count bound."""
    backend = _started(tmp_path, max_total_recall_chars=0, max_memories=3)
    backend.set_task("t")
    backend.system.hits = ["x" * 500, "y" * 600, "z" * 700, "w" * 800]
    recall = backend.recall_context()
    assert recall["n_memories"] == 3  # the slice, not a budget
    body = recall["content"].split("## Fake Memories\n", 1)[1]
    assert len(body) == 502 + 1 + 602 + 1 + 702  # no cap
    assert RECALL_LINE_TRUNCATION not in body


def test_default_budgets_reproduce_the_shipped_2000_skip_bound(tmp_path):
    """Default equivalence (per-memory 0 / total 2000 = the shipped arm
    behavior): whole lines fill the budget exactly as the old 2000-char skip
    bound did — the one rendering delta is a line the old bound SKIPPED now
    landing truncated when the floor's worth of budget remains (recorded)."""
    backend = _started(tmp_path, max_memories=20)
    assert backend.config.max_chars_per_memory == 0  # the native default: off
    assert backend.config.max_total_recall_chars == 2000  # the shipped bound
    backend.set_task("t")
    backend.system.hits = ["x" * 122] * 20  # "- " + 122 = 124 chars per line
    recall = backend.recall_context()
    # 16 lines fill the budget (16 x 124 + 15 separators = 1999); the 17th
    # finds 0 chars remaining — below the floor, skipped whole like the old
    # skip bound dropped it.
    assert recall["n_memories"] == 16
    assert RECALL_LINE_TRUNCATION not in recall["content"]
    body = recall["content"].split("## Fake Memories\n", 1)[1]
    assert len(body) == 1999

    # The recorded delta: an over-budget line the old skip bound dropped now
    # lands truncated when >= the 40-char floor remains.
    delta = _started(tmp_path / "delta", max_memories=20)
    delta.set_task("t")
    delta.system.hits = ["x" * 122] * 15 + ["y" * 500]
    recall = delta.recall_context()
    assert recall["n_memories"] == 16  # 15 whole + the truncated tail
    lines = recall["content"].split("## Fake Memories\n", 1)[1].splitlines()
    assert len(lines[-1]) == 125  # 2000 - (15 x 124 + 14 separators) - 1
    assert lines[-1].endswith(RECALL_LINE_TRUNCATION)


def test_hit_budget_exempt_defaults_to_false(tmp_path):
    backend = _started(tmp_path)
    assert backend._hit_budget_exempt("anything") is False


def test_budget_exempt_hit_renders_full_consumes_no_budget_keeps_its_slot(tmp_path):
    """An exempt hit delivers in full outside both budgets (the native scope
    rule: the budget governs memory lines only) — never truncated by either
    knob, excluded from the total accounting — but it still occupies one
    max_memories slot, so the slice can starve a non-exempt line of a slot."""

    class ExemptFake(FakeBackend):
        def _hit_budget_exempt(self, hit):
            return str(hit).startswith("EXEMPT ")

    backend = ExemptFake(_config(tmp_path, max_memories=2, max_total_recall_chars=20, max_chars_per_memory=16))
    backend.start()
    assert backend._available
    backend.set_task("t")
    exempt = "EXEMPT " + "x" * 500
    backend.system.hits = [exempt, "small one", "small two"]
    recall = backend.recall_context()
    header = f"{RECALL_POLICY_DEFAULT}\n\n## Fake Memories"
    lines = recall["content"][len(header) + 1 :].splitlines()
    # Full exempt line: no per-memory truncation (the 16-char cap would gut
    # it), no total-budget truncation, no suffix.
    assert lines[0] == f"- {exempt}"
    # The exempt line consumed no budget: the small line still fits the full
    # 20-char total and renders whole.
    assert lines[1] == "- small one"
    # The exempt hit kept its slice slot: max_memories=2 leaves no slot for
    # "small two".
    assert recall["memories"] == [exempt, "small one"]
    assert recall["n_memories"] == 2


def test_budget_exempt_boundary_separator_counts_against_the_total(tmp_path):
    """The "\\n" joining a budget-exempt line to the first budgeted line is
    charged against max_total_recall_chars (the separator keys on the rendered
    body, not on the budget): the placed non-exempt block never exceeds the
    total by that one char."""

    class ExemptFake(FakeBackend):
        def _hit_budget_exempt(self, hit):
            return str(hit).startswith("EXEMPT ")

    exempt = "EXEMPT " + "x" * 50
    # total=11: the 11-char line fits the total alone, but its joining
    # separator pushes the cost to 12 — over budget with less than the
    # 40-char truncate floor remaining, so the line is skipped whole rather
    # than delivered one char past the total.
    skipped = ExemptFake(_config(tmp_path / "skipped", max_memories=5, max_total_recall_chars=11))
    skipped.start()
    skipped.set_task("t")
    skipped.system.hits = [exempt, "y" * 9]  # "- " + 9 = 11 chars
    recall = skipped.recall_context()
    assert recall["memories"] == [exempt]

    # total=12: the same line now fits exactly (11 chars + 1 separator).
    fits = ExemptFake(_config(tmp_path / "fits", max_memories=5, max_total_recall_chars=12))
    fits.start()
    fits.set_task("t")
    fits.system.hits = [exempt, "y" * 9]
    recall = fits.recall_context()
    assert recall["memories"] == [exempt, "y" * 9]
    exempt_line = f"- {exempt}"
    body = recall["content"].split("## Fake Memories\n", 1)[1]
    assert body == f"{exempt_line}\n- {'y' * 9}"
    # The non-exempt block (the line plus its joining separator) == the total.
    assert len(body) - len(exempt_line) == 12


def test_recall_min_score_filters_before_the_slice(tmp_path):
    """The floor drops score-less and below-floor hits BEFORE the max_memories
    slice (filter -> slice -> rank-then-fill): a floor applied after the slice
    could only under-fill."""
    backend = _started(tmp_path, recall_min_score=0.5, max_memories=2)
    backend.set_task("t")
    backend.system.hits = ["low", "none", "high", "also-high"]
    backend.system.scores = {"low": 0.2, "high": 0.9, "also-high": 0.7}  # "none" carries no score
    recall = backend.recall_context()
    assert recall["memories"] == ["high", "also-high"]
    assert "- low" not in recall["content"] and "- none" not in recall["content"]


def test_recall_min_score_none_disables_the_floor(tmp_path):
    backend = _started(tmp_path, recall_min_score=None)
    backend.set_task("t")
    backend.system.hits = ["one"]  # no score, but no floor either
    assert backend.recall_context()["n_memories"] == 1


def test_provenance_suffix_marks_this_and_earlier_episodes(tmp_path):
    backend = _started(tmp_path)
    backend.set_task("t")
    own = backend._session_id
    other = _new_session_id("pydata__xarray-2905")
    backend.system.hits = ["own", "other", "junk", "silent"]
    backend.system.origins = {"own": own, "other": other, "junk": "session_0123"}  # "silent": no origin
    recall = backend.recall_context()
    lines = recall["content"].splitlines()
    assert lines[-4].endswith("- own (from this episode)")
    assert lines[-3].endswith("- other (from earlier episode pydata__xarray-2905)")
    assert lines[-2].endswith("- junk (from an earlier episode)")  # unparseable: no registry to resolve through
    assert lines[-1].endswith("- silent")  # no origin signal: no suffix
    assert recall["origins"] == [own, other, "session_0123", None]


def test_note_recall_logs_the_origin_list(tmp_path):
    backend = _started(tmp_path)
    backend.set_task("t")
    backend.system.hits = ["one"]
    backend.system.origins = {"one": backend._session_id}
    recall = backend.recall_context()
    backend.note_recall(recall, step=2)
    event = [e for e in backend._events if e["kind"] == "recall"][-1]
    assert event["origins"] == [backend._session_id]


def test_recall_envelope_owns_backend_errors(tmp_path):
    backend = _started(tmp_path)
    backend.set_task("t")
    backend.system.search_error = RuntimeError("search boom")
    assert backend.recall_context() is None  # contained
    assert backend._counts["backend_errors"] == 1
    # The integration's private counting the base contract describes: both
    # grains — the per-op failure and the envelope's generic op failure.
    assert backend._counts["search_errors"] == 1
    assert backend._events[-1]["op"] == "recall"
    # No private counters were invented by the base.
    assert set(backend._counts) == {
        "messages_recorded",
        "extraction_calls",
        "extraction_errors",
        "recall_injections",
        "backend_errors",
        "search_errors",
        "recall_cache_hits",
        "rewrite_calls",
        "rewrite_successes",
        "rewrite_failures",
        "widgets",
    }

    strict = _started(tmp_path / "strict", strict=True)
    strict.set_task("t")
    strict.system.search_error = RuntimeError("search boom")
    with pytest.raises(RuntimeError, match="search boom"):
        strict.recall_context()


def test_recall_render_stage_containment(tmp_path):
    """A rendering failure is a recall failure: contained and counted."""
    backend = _started(tmp_path)
    backend.set_task("t")
    backend.system.hits = ["one"]
    backend.raise_in_render = True
    assert backend.recall_context() is None
    assert backend._counts["backend_errors"] == 1
    assert backend._events[-1]["op"] == "recall"

    strict = _started(tmp_path / "strict", strict=True)
    strict.set_task("t")
    strict.system.hits = ["one"]
    strict.raise_in_render = True
    with pytest.raises(RuntimeError, match="render boom"):
        strict.recall_context()


def test_note_recall_counts_and_never_raises(tmp_path):
    backend = _started(tmp_path, strict=True)
    backend.note_recall({"n_memories": 2, "chars": 10}, step=1)
    backend.note_recall({}, step=2)
    assert backend._counts["recall_injections"] == 2
    assert [e["kind"] for e in backend._events if e["kind"] == "recall"] == ["recall", "recall"]


# ---------------------------------------------------------------------------
# Dirty-flag search cache
# ---------------------------------------------------------------------------
def test_clean_steps_serve_the_cache_without_searching(tmp_path):
    backend = _started(tmp_path)
    backend.set_task("t")
    backend.system.hits = ["one"]
    fresh = backend.recall_context(planned_step=1)
    assert fresh is not None and "cached" not in fresh
    assert backend.system.search_calls == 1
    backend.system.hits = ["changed-under-a-clean-flag"]  # no store write: never re-searched
    cached = backend.recall_context(planned_step=2)
    assert cached["cached"] is True
    assert cached["content"] == fresh["content"]  # the memoized block, byte-identical
    assert backend.system.search_calls == 1  # no second search
    # The hit counts at delivery (note_recall), the same point recall_injections
    # counts — a rendered-but-undelivered serve inflates neither counter.
    assert backend._counts["recall_cache_hits"] == 0
    backend.note_recall(cached, step=2)
    assert backend._counts["recall_cache_hits"] == 1
    assert backend._counts["recall_injections"] == 1


def test_episode_start_resets_query_cache_and_flag(tmp_path):
    backend = _started(tmp_path)
    backend.set_task("first task")
    backend.system.hits = ["one"]
    assert backend.recall_context() is not None
    assert backend.system.search_calls == 1
    backend.system.hits = []
    backend.set_task("second task")  # a fresh episode must search, never reuse "nothing"
    assert backend._current_query == "second task"
    assert backend._search_dirty is True and backend._cached_recall is None
    assert backend.recall_context() is None  # searched again (empty now)
    assert backend.system.search_calls == 2


def test_extract_ticks_mark_dirty_on_every_counted_call(tmp_path):
    backend = _started(tmp_path, extract_every_n_steps=1)
    backend.set_task("t")
    backend.system.hits = ["one"]
    backend.recall_context()
    assert backend._search_dirty is False

    # A readiness-guard skip (nothing pending) is not a counted call: no mark.
    backend.maybe_extract(1)
    assert backend._search_dirty is False

    # A successful counted tick marks, even when it extracts zero new rows.
    backend.record([{"role": "user", "content": "note"}], step=1)
    backend.maybe_extract(2)
    assert backend._counts["extraction_calls"] == 1
    assert backend._search_dirty is True
    backend.recall_context()
    assert backend._search_dirty is False

    # A FAILED counted tick marks too: the extraction may have written before
    # raising (a hosted write-then-poll-timeout stores server-side; a local
    # mid-write failure leaves partial rows), so the next recall must
    # re-search rather than serve the memoized pre-write payload for the rest
    # of the episode.
    backend.record([{"role": "user", "content": "boom note"}], step=2)
    backend.system.extract_error = RuntimeError("extract boom")
    backend.maybe_extract(3)
    assert backend._counts["extraction_calls"] == 2
    assert backend._counts["extraction_errors"] == 1
    assert backend._search_dirty is True
    backend.recall_context()
    assert backend.system.search_calls == 3  # the failed tick's mark forced a re-search


def test_empty_result_is_cached_and_a_failed_search_is_not(tmp_path):
    backend = _started(tmp_path)
    backend.set_task("t")
    assert backend.recall_context() is None  # empty hit set
    assert backend.system.search_calls == 1
    backend.system.hits = ["appeared-without-a-write"]  # cached empty must not notice
    assert backend.recall_context() is None
    assert backend.system.search_calls == 1  # the empty answer was cached
    assert backend._counts["recall_cache_hits"] == 0  # a cached empty injects nothing

    backend._mark_store_changed()
    backend.system.search_error = RuntimeError("search boom")
    assert backend.recall_context() is None  # failure contained
    assert backend._search_dirty is True  # never cached: the next step retries
    assert backend._counts["search_errors"] == 1
    backend.system.search_error = None
    assert backend.recall_context() is not None  # the retry searches
    assert backend.system.search_calls == 3


def test_search_seconds_accrue_to_the_backend_owned_exemption(tmp_path, monkeypatch):
    """The wall-clock exemption is backend-owned: the native search's seconds
    accrue to the backend accumulator and drain through the same consume_*
    entry point — with annotate=false, where no annotator exists at all."""
    backend = _started(tmp_path)  # untraced: _trace is None
    backend.set_task("t")
    backend.system.hits = ["one"]
    clock = iter([100.0, 100.25])
    monkeypatch.setattr("shared_bridge.backend.time.monotonic", lambda: next(clock))
    backend.recall_context()
    assert backend._io_duration == 0.25  # the native search's measured seconds
    assert backend.consume_annotation_duration() == 0.25  # drained through the same consume_*
    assert backend.consume_annotation_duration() == 0.0  # drained once


# ---------------------------------------------------------------------------
# Extraction guidelines (the shared policy layer)
# ---------------------------------------------------------------------------
def test_repo_of_parses_instance_ids_conservatively():
    """The repository key: `<owner>__<repo>-<number>` keeps its repo prefix;
    an id without a trailing plain number keeps the full id, so such an
    instance keeps its own project scope instead of colliding with another."""
    assert _repo_of("astropy__astropy-14995") == "astropy__astropy"
    assert _repo_of("django__django-11099") == "django__django"
    assert _repo_of("plain-id") == "plain-id"
    assert _repo_of("owner__repo-v2") == "owner__repo-v2"


def test_effective_user_id_scope_split(tmp_path):
    """The store-side user id is the run-isolation tier every integration
    shares: scope=run passes the configured id through, scope=instance
    suffixes the instance id."""
    run_backend = FakeBackend(_config(tmp_path / "run", user_id="u1"), "owner__repo-42")
    assert run_backend.effective_user_id() == "u1"
    instance_backend = FakeBackend(_config(tmp_path / "inst", user_id="u1", scope="instance"), "owner__repo-42")
    assert instance_backend.effective_user_id() == "u1:owner__repo-42"


def test_extraction_guidelines_default_when_unset(tmp_path):
    """No override: the conveyed text is the shared default policy with the
    base-composed episode context appended — instance and repository key
    distinct, so a swap between them cannot pass."""
    backend = FakeBackend(_config(tmp_path), "owner__repo-42")
    backend.start()
    conveyed = (
        f"{EXTRACTION_GUIDELINES_DEFAULT}\n\n"
        f"{extraction_episode_context('owner__repo-42', 'owner__repo')}"
    )
    assert backend._extraction_guidelines() == conveyed
    assert backend.system.extraction_guidelines == conveyed
    assert backend._core_initial_settings()["extraction_guidelines"] == conveyed


def test_extraction_guidelines_override_replaces_the_default(tmp_path):
    """A configured override IS the policy (replaces the default wholesale,
    stripped) — never default + override concatenated. The episode context is
    still appended: it is per-episode fact, not policy, so an override must
    not blind the extractor to the current repository."""
    backend = FakeBackend(
        _config(tmp_path, extraction_guidelines="  prefer operational facts  "), "owner__repo-42"
    )
    backend.start()
    conveyed = backend.system.extraction_guidelines
    assert conveyed == (
        f"prefer operational facts\n\n"
        f"{extraction_episode_context('owner__repo-42', 'owner__repo')}"
    )
    assert EXTRACTION_GUIDELINES_DEFAULT not in conveyed
    assert backend._core_initial_settings()["extraction_guidelines"] == conveyed


def test_extraction_guidelines_incapable_integration_conveys_nothing(tmp_path, caplog):
    """An integration whose extraction engine accepts no custom prompt rules
    declares the capability off: nothing is conveyed (a pure no-op), and only a
    configured override draws the mismatch warning — the default being
    unconveyable is the integration's normal state, not a misconfiguration."""

    class IncapableFake(FakeBackend):
        _CONVEYS_EXTRACTION_GUIDELINES = False

    with caplog.at_level(logging.WARNING, logger="shared_bridge.backend"):
        plain = IncapableFake(_config(tmp_path))
        plain.start()
        assert plain.system.extraction_guidelines == ""
        assert plain._core_initial_settings()["extraction_guidelines"] == ""
        assert not caplog.records  # no override: no warning
        overridden = IncapableFake(_config(tmp_path / "two", extraction_guidelines="no channel"))
        overridden.start()
    assert overridden.system.extraction_guidelines == ""
    assert [record for record in caplog.records if "extraction_guidelines" in record.message]


# ---------------------------------------------------------------------------
# Query rewrite (the QUERY lane)
# ---------------------------------------------------------------------------
def _rewriting(output_dir, capture_server=None, **overrides):
    overrides.setdefault("rewrite_every_n_steps", 1)
    overrides.setdefault("rewrite_model", "q-model")
    overrides.setdefault("rewrite_base_url", capture_server.url if capture_server else "http://q.invalid")
    overrides.setdefault("rewrite_api_key", "k")
    return _config(output_dir, **overrides)


def test_rewrite_start_guard_requires_the_full_connection(tmp_path, monkeypatch):
    """rewrite_every_n_steps > 0 with an incomplete connection fails the
    episode start (fail-closed) instead of failing closed at every boundary."""
    for var in ("MEMORY_QUERY_MODEL", "MEMORY_QUERY_MODEL_URL", "MEMORY_QUERY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    backend = FakeBackend(_config(tmp_path, rewrite_every_n_steps=5, rewrite_base_url="http://q.invalid"))
    backend.start()  # contained (non-strict): unavailable, memory.json written
    assert backend._available is False
    data = json.loads((tmp_path / "memory.json").read_text())
    assert any("rewrite settings" in e.get("error", "") for e in data["events"])
    # Disabled rewriting never consults the connection at all.
    plain = FakeBackend(_config(tmp_path / "plain"))
    plain.start()
    assert plain._available is True and plain._rewrite_settings is None


def test_rewrite_settings_resolve_from_env_and_persist_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_QUERY_MODEL", "env-q-model")
    monkeypatch.setenv("MEMORY_QUERY_MODEL_URL", "http://127.0.0.1:9/QUERY/trajectories/bearer-id-123/v1")
    monkeypatch.setenv("MEMORY_QUERY_API_KEY", "env-key")
    backend = FakeBackend(_config(tmp_path, rewrite_every_n_steps=5))
    backend.start()
    assert backend._available is True
    assert backend._rewrite_settings["model"] == "env-q-model"
    backend.finalize()
    settings = json.loads((tmp_path / "memory.json").read_text())["settings"]
    assert settings["rewrite_model"] == "env-q-model"
    # Only the sanitized form persists: the bearer trajectory ID is hashed.
    assert "bearer-id-123" not in settings["rewrite_base_url"]
    assert "env-key" not in json.dumps(settings)


def test_rewrite_success_replaces_query_and_marks_dirty(tmp_path, capture_server):
    capture_server.responder = lambda path, events: (
        200,
        {"choices": [{"message": {"content": json.dumps({"query": "what the agent needs now"})}, "finish_reason": "stop"}]},
    )
    backend = FakeBackend(_rewriting(tmp_path, capture_server))
    backend.start()
    backend.set_task("the original task")
    backend.record([{"role": "user", "content": "progress so far"}], step=1)
    backend.recall_context()  # cold search clears the flag
    assert backend._search_dirty is False
    backend.maybe_rewrite(1)
    assert backend._current_query == "what the agent needs now"
    assert backend._query_source == "rewritten"
    assert backend._search_dirty is True  # the query changed: the next recall re-searches
    assert backend._counts["rewrite_calls"] == 1
    assert backend._counts["rewrite_successes"] == 1
    assert backend._counts["rewrite_failures"] == 0
    # The rewriter saw the task and the ring buffer's recent messages.
    user_message = backend._rewrite_messages()[1]["content"]
    assert "the original task" in user_message and "progress so far" in user_message
    backend.finalize()


def test_rewrite_failure_keeps_query_and_flag(tmp_path, capture_server):
    capture_server.responder = lambda path, events: (500, {"error": "down"})
    backend = FakeBackend(_rewriting(tmp_path, capture_server))
    backend.start()
    backend.set_task("the original task")
    backend.recall_context()
    assert backend._search_dirty is False
    backend.maybe_rewrite(1)  # transport/HTTP failure: fail closed
    assert backend._current_query == "the original task"  # the old query stays
    assert backend._query_source == "task"
    assert backend._search_dirty is False  # the flag is left untouched
    assert backend._counts["rewrite_failures"] == 1
    assert "error" in [e for e in backend._events if e["kind"] == "rewrite"][-1]
    backend.maybe_rewrite(1)  # same bucket: no second attempt
    assert backend._counts["rewrite_calls"] == 1
    backend.finalize()


def test_rewrite_breaker_stops_a_dead_lane(tmp_path, capture_server):
    """A permanently dead QUERY lane must not keep paying rewrite_timeout every
    boundary to the episode's end — the breaker mirrors the extraction one's."""
    capture_server.responder = lambda path, events: (500, {"error": "down"})
    backend = FakeBackend(_rewriting(tmp_path, capture_server, rewrite_max_consecutive_errors=2))
    backend.start()
    backend.set_task("the original task")
    backend.maybe_rewrite(1)
    assert backend._rewrite_breaker is False
    backend.maybe_rewrite(2)
    assert backend._counts["rewrite_calls"] == 2
    assert backend._rewrite_breaker is True
    assert any(e.get("op") == "rewrite_breaker" for e in backend._events)
    backend.maybe_rewrite(3)  # breaker: periodic ticks disabled, the query stays
    assert backend._counts["rewrite_calls"] == 2
    assert backend._current_query == "the original task"
    backend.finalize()


def test_rewrite_breaker_resets_on_success_and_zero_disables(tmp_path, capture_server):
    """A flaky lane never trips (the streak resets on success); limit 0 never breaks."""
    fail = (500, {"error": "down"})
    ok = (200, {"choices": [{"message": {"content": json.dumps({"query": "fresh query"})}, "finish_reason": "stop"}]})
    answers = iter([fail, fail, ok, fail, fail])
    capture_server.responder = lambda path, events: next(answers)
    backend = FakeBackend(_rewriting(tmp_path, capture_server, rewrite_max_consecutive_errors=3))
    backend.start()
    backend.set_task("the original task")
    for step in range(1, 6):
        backend.maybe_rewrite(step)
    assert backend._counts["rewrite_calls"] == 5
    assert backend._counts["rewrite_successes"] == 1
    assert backend._rewrite_breaker is False
    backend.finalize()

    capture_server.responder = lambda path, events: fail
    unbreakable = FakeBackend(_rewriting(tmp_path / "zero", capture_server, rewrite_max_consecutive_errors=0))
    unbreakable.start()
    unbreakable.set_task("t")
    for step in range(1, 6):
        unbreakable.maybe_rewrite(step)
    assert unbreakable._counts["rewrite_calls"] == 5
    assert unbreakable._rewrite_breaker is False
    unbreakable.finalize()


def test_ring_buffer_keeps_the_last_six_recorded_pairs(tmp_path):
    backend = _started(tmp_path)
    backend.set_task("t")
    backend.record([{"role": "user", "content": f"m{i}"} for i in range(8)], step=1)
    assert [text for _, text in backend._recent_messages] == [f"m{i}" for i in range(2, 8)]
    messages = backend._rewrite_messages()
    assert messages[0]["role"] == "system"
    assert "m2" in messages[1]["content"] and "m7" in messages[1]["content"]
    assert "m1" not in messages[1]["content"]  # dropped oldest
    backend.start()  # a re-start clears the ring with the rest of the episode state
    assert list(backend._recent_messages) == []


def test_rewrite_messages_task_uses_the_recording_cap(tmp_path):
    """The rewriter's Task: block rides the same cap + truncation marker as
    record() — never a silent fixed slice — while the stored task stays
    full-length."""
    backend = _started(tmp_path)  # default max_message_chars=4000
    small = "x" * 100
    backend.set_task(small)
    assert f"Task:\n{small}\n" in backend._rewrite_messages()[1]["content"]  # under the cap: full text

    exact = "y" * 2135
    backend.set_task(exact)
    assert f"Task:\n{exact}\n" in backend._rewrite_messages()[1]["content"]  # 2135 < 4000: uncut

    capped = _started(tmp_path / "capped", max_message_chars=50)
    long_task = "z" * 5000
    capped.set_task(long_task)
    body = capped._rewrite_messages()[1]["content"].split("Task:\n", 1)[1].split("\n\nRecent progress", 1)[0]
    assert len(body) == 50 and body.endswith(TRUNCATION_MARKER)
    # The cap touches only the prompt view: task, query, and session stay full-length.
    assert capped._task == long_task and capped._current_query == long_task


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------
def test_finalize_happy_path_and_idempotency(tmp_path):
    backend = _started(tmp_path)
    backend.set_task("t")
    backend.record([{"role": "user", "content": "tail"}], step=1)
    backend.system.dump_rows = [{"id": "m1"}]
    backend.finalize()
    backend.finalize()  # guarded no-op: no second close, no rewrite
    assert backend.system.close_calls == 1
    data = json.loads((tmp_path / "memory.json").read_text())
    assert data["available"] is True
    assert data["session_id"] == backend._session_id
    assert data["final_memories"] == [{"id": "m1"}]
    assert data["counts"]["extraction_calls"] == 1  # the final flush
    assert data["settings"] == {"api_base_url": "https://fake.invalid", "strict": False}


def test_finalize_close_error_reraises_under_strict(tmp_path):
    """A close failure joins first_error (re-raised under strict); the close
    still ran, and memory.json was written first."""
    backend = _started(tmp_path, strict=True)
    boom = RuntimeError("close boom")
    backend.system.close_error = boom
    with pytest.raises(RuntimeError) as excinfo:
        backend.finalize()
    assert excinfo.value is boom
    assert backend.system.closed is True
    assert (tmp_path / "memory.json").exists()


def test_finalize_close_error_contained_non_strict(tmp_path):
    backend = _started(tmp_path)
    backend.system.close_error = RuntimeError("close boom")
    backend.finalize()  # no raise
    assert backend.system.closed is True


def test_finalize_first_error_is_not_masked(tmp_path):
    """The strict re-raise surfaces the FIRST failure (the flush), not the
    later dump/close failures."""
    backend = _started(tmp_path, strict=True)
    backend.record([{"role": "user", "content": "tail"}], step=1)
    flush_boom = RuntimeError("flush boom")
    backend.system.extract_error = flush_boom
    backend.system.dump_error = RuntimeError("dump boom")
    backend.system.close_error = RuntimeError("close boom")
    with pytest.raises(RuntimeError) as excinfo:
        backend.finalize()
    assert excinfo.value is flush_boom


def test_post_finalize_work_surface_is_dormant(tmp_path):
    """After finalize, record/maybe_extract/recall are silent no-ops: nothing
    is stored, extracted, searched, counted, or logged — the handle is closed,
    and fail-closed counts real backend failures, not lifecycle misuse."""
    backend = _started(tmp_path, extract_every_n_steps=1)
    backend.set_task("t")
    backend.record([{"role": "user", "content": "m"}], step=1)
    backend.system.hits = ["one"]
    backend.finalize()
    counts = dict(backend._counts)
    n_events = len(backend._events)

    backend.record([{"role": "user", "content": "late"}], step=2)
    backend.maybe_extract(2)
    assert backend.recall_context() is None
    assert backend._counts == counts
    assert len(backend._events) == n_events
    assert backend.system.pending == []  # the final flush drained it; nothing new stored


def test_stats_shape(tmp_path):
    backend = _started(tmp_path)
    stats = backend.stats()
    assert stats["enabled"] is True
    assert stats["available"] is True
    assert stats["session_id"] == backend._session_id
    assert stats["counts"]["widgets"] == 0


# ---------------------------------------------------------------------------
# Zero integration naming in the shared layer (mechanically enforced)
# ---------------------------------------------------------------------------
def test_shared_layer_names_no_integration():
    """Nothing under shared_bridge/src may carry a specific integration's name:
    shared sources stay integration-agnostic, and the scan's name list (one
    compound per known integration) is the only deliberate shared-tests edit
    a new integration brings.
    The compounds are load-bearing (word-bounded pattern): `tencentdb` is
    caught as a word while `tencentdb_bridge` is not — exactly as `mem0` vs
    `mem0_bridge`; `tdai` also catches hyphen-delimited ids like
    `x-tdai-service-id`."""
    src = Path(__file__).resolve().parents[1] / "src"
    pattern = re.compile(r"\b(cure|mem0|tencentdb|tdai)\b", re.IGNORECASE)
    offenders = []
    for path in sorted(src.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "integration names leaked into shared_bridge:\n" + "\n".join(offenders)

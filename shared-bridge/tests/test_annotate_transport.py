"""Annotation transport unit tests (offline): constructor validation, cursor
decoding, and the HTTP error path. The live round-trip against a real capture
server is covered by integration/cure_memory/tests/test_annotate.py."""

import io
import urllib.error
from unittest import mock

import pytest

from shared_bridge.annotate import Annotator, PostResult, normalize_score, resolve_lane_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.87, 0.87),
        (0.0328, 0.0328),
        (1, 1.0),
        ("0.87", 0.87),
        ("high", None),
        ("", None),
        (float("nan"), None),
        (float("inf"), None),
        (True, None),
        (None, None),
        ([0.9], None),
        # A raw int past float range (JSON parses a 400-digit integer to one):
        # dropped, never an OverflowError escaping into the whole result set.
        (10**400, None),
    ],
)
def test_normalize_score(value, expected):
    """Only real finite numbers survive: string scores parse, everything else
    drops instead of fabricating 0.0."""
    assert normalize_score(value) == expected


def test_annotator_rejects_invalid_retry_and_breaker_parameters():
    # A negative retries would leave _attempt_loop's result unbound, surfacing
    # as a swallowed UnboundLocalError instead of a clear construction error.
    with pytest.raises(ValueError):
        Annotator(timeout=1.0, retries=-1, max_consecutive_errors=3)
    with pytest.raises(ValueError):
        Annotator(timeout=1.0, retries=0, max_consecutive_errors=0)
    annotator = Annotator(timeout=1.0, retries=0, max_consecutive_errors=1)
    assert annotator.breaker_open is False


def test_cursor_rejects_bool_and_non_int():
    assert PostResult(ok=True, body={"role_call_cursor": 3}).cursor == 3
    # bool is an int subclass: a stray true/false must not bind at cursor 1/0.
    assert PostResult(ok=True, body={"role_call_cursor": True}).cursor is None
    assert PostResult(ok=True, body={"role_call_cursor": "3"}).cursor is None
    assert PostResult(ok=True, body={}).cursor is None
    assert PostResult(ok=True).cursor is None


def test_http_error_returns_status_and_closes_the_body():
    annotator = Annotator(timeout=1.0, retries=0, max_consecutive_errors=3)
    fp = io.BytesIO(b'{"error": "boom"}')

    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, fp)

    with mock.patch("urllib.request.urlopen", fail):
        result = annotator.post("http://h/MAIN/trajectories/abc/annotate", [])
    assert result.ok is False and result.status == 500
    assert fp.closed  # the error body is not left leaking the socket


# ---------------------------------------------------------------------------
# resolve_lane_url: the no-model-URL lane (a lane that carries no model
# traffic at all resolves from the explicit config/env URL alone — but only
# when the caller opts in; a model-carrying lane never does)
# ---------------------------------------------------------------------------
def test_explicit_url_accepted_for_a_lane_without_model_url():
    explicit = "http://h:1/MEMORY/trajectories/abc/annotate"
    assert resolve_lane_url(explicit, "", "", allow_no_model_url=True) == explicit
    assert resolve_lane_url("", explicit, "", allow_no_model_url=True) == explicit  # the env override resolves too
    assert resolve_lane_url(" ", explicit, "  ", allow_no_model_url=True) == explicit
    # No explicit anywhere and no model URL: nothing to derive from.
    assert resolve_lane_url("", "", "", allow_no_model_url=True) is None


def test_explicit_url_rejected_for_a_model_carrying_lane_without_model_url():
    """The default (main-lane) rule: with no model URL to check the explicit
    URL against, it cannot be validated and is ignored — a stale
    MEMORY_ANNOTATE_MAIN_URL export must never bind a lane blindly."""
    explicit = "http://h:1/MAIN/trajectories/abc/annotate"
    assert resolve_lane_url(explicit, "", "") is None
    assert resolve_lane_url("", explicit, "") is None


def test_non_trajectory_model_url_keeps_ignoring_the_explicit_url():
    """A lane whose model URL exists but carries no trajectory scope keeps
    today's semantics: the explicit URL cannot be validated against it and is
    ignored (the provider-URL fallback is unchanged)."""
    explicit = "http://h:1/MAIN/trajectories/abc/annotate"
    assert resolve_lane_url(explicit, "", "https://api.deepseek.com/v1") is None

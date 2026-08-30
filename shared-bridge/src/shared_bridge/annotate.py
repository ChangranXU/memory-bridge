"""Annotation transport for the memory bridge (PLAN §6.2/§6.3).

Resolves each lane's annotate endpoint (explicit config, then the matching
``MEMORY_ANNOTATE_*`` env override, then derivation from the lane's effective
model base URL), validates the pair, sanitizes URLs for artifacts and logs,
and posts batches over stdlib ``urllib`` with timeout, limited retries, and a
session circuit breaker. Nothing here raises into the memory/agent path: a
disabled or broken endpoint only means untraced native behavior, and logged
URLs never carry a bearer trajectory ID.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("shared_bridge.annotate")

_TRAJECTORY_SEGMENT = re.compile(r"^(?P<prefix>.*/trajectories/(?P<id>[^/]+))(?P<rest>/.*)?$")


def canonical_json_sha256(value) -> str:
    """SHA-256 over canonical JSON: sorted keys, compact separators, Unicode
    unescaped, non-finite numbers rejected. Pinned to the recorder's canonical
    form (PLAN §4.5) — the two implementations must never diverge. Like the
    recorder, ``surrogatepass`` keeps lone-surrogate strings (valid JSON,
    invalid strict UTF-8) digestible instead of raising."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def text_sha256(text: str) -> str:
    """SHA-256 over exact UTF-8 bytes (surrogatepass, as the recorder does)."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def inline_text_ref(text: str) -> dict:
    """An inline ContentRef carrying the exact text and its digest."""
    return {
        "availability": "inline",
        "media_type": "text/plain; charset=utf-8",
        "sha256": text_sha256(text),
        "bytes": len(text.encode("utf-8", "surrogatepass")),
        "chars": len(text),
        "text": text,
    }


def sanitize_url(url: str) -> str:
    """Artifact/log-safe form: userinfo, query, and fragment stripped; any
    trajectory-ID path segment replaced by the 16-hex SHA-256 prefix that
    ``run.json.trajectory_id_hash`` records, never the bearer ID itself."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return "<unparseable-url>"
    netloc = parts.hostname or ""
    if ":" in netloc:
        netloc = f"[{netloc}]"  # urlsplit strips an IPv6 literal's brackets
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = re.sub(
        r"(/trajectories/)([^/]+)",
        lambda m: m.group(1) + hashlib.sha256(m.group(2).encode()).hexdigest()[:16],
        parts.path,
    )
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def normalize_score(value) -> float | None:
    """A native relevance score as a finite float, or None when the payload
    carries no usable number (numeric strings parse; anything else drops).
    Coercing a missing/malformed score to 0.0 would fabricate ranking evidence
    the recorded refs and the endpoint contract both consume, and a malformed
    row must never fail the whole result set. Score scales are
    integration-defined and never comparable across integrations."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        # A raw int past float range (JSON parses a 400-digit integer to one).
        return None
    return result if math.isfinite(result) else None


def _normalize(url: str) -> str:
    """Comparison form: scheme/host case-folded, no userinfo/query/fragment or
    trailing slash. Two URLs naming the same endpoint compare equal."""
    parts = urlsplit(url.strip())
    netloc = parts.hostname or ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc.lower(), parts.path.rstrip("/"), "", ""))


def derive_annotate_url(model_base_url: str) -> str | None:
    """``.../trajectories/<id>[/v1]`` -> ``.../trajectories/<id>/annotate``,
    retaining every preceding path segment (reverse-proxy prefixes and the role
    segment, which is deliberately never parsed). None for URLs without a
    trajectory scope — normal provider URLs never receive annotations."""
    match = _TRAJECTORY_SEGMENT.match(model_base_url.strip())
    if not match:
        return None
    return match.group("prefix") + "/annotate"


def resolve_lane_url(explicit: str, env_value: str, model_base_url: str, *, allow_no_model_url: bool = False) -> str | None:
    """One lane's annotate endpoint, or None when the lane cannot be traced.

    An explicit (config or env) URL is authoritative but must name the same
    endpoint the lane's model URL derives — matching only the origin could
    bind the wrong lane behind a reverse proxy, so the complete normalized
    prefix through the trajectory ID must agree. Only a lane that carries no
    model traffic by design (``allow_no_model_url`` — the memory lane of a
    hosted-extraction integration) accepts the explicit URL with no model URL
    to check it against; a model-carrying lane (always the main lane) without
    a model URL proves nothing about where its traffic went, so an unchecked
    explicit URL is ignored. ``endpoints_compatible`` still guards the pair
    downstream (same trajectory, distinct lanes)."""
    chosen = explicit.strip() or env_value.strip()
    model_url = model_base_url.strip()
    if not chosen:
        return derive_annotate_url(model_url)
    if not model_url and allow_no_model_url:
        return chosen
    derived = derive_annotate_url(model_url)
    if derived is None:
        logger.debug("explicit annotate URL %s ignored: the lane's model URL has no trajectory scope", sanitize_url(chosen))
        return None
    try:
        if _normalize(chosen) != _normalize(derived):
            logger.debug(
                "explicit annotate URL %s ignored: does not match the derived %s",
                sanitize_url(chosen),
                sanitize_url(derived),
            )
            return None
    except ValueError:
        logger.debug("explicit annotate URL ignored: %s", sanitize_url(chosen))
        return None
    return chosen


def _trajectory_id(url: str) -> str | None:
    match = _TRAJECTORY_SEGMENT.match(url.strip())
    return match.group("id") if match else None


def endpoints_compatible(main_url: str | None, memory_url: str | None) -> str | None:
    """Error string when two resolved endpoints cannot carry one session:
    both must address the same trajectory as two distinct lane paths."""
    if main_url is None or memory_url is None:
        return None
    main_id = _trajectory_id(main_url)
    memory_id = _trajectory_id(memory_url)
    if main_id is None or memory_id is None:
        return "annotation endpoints are not trajectory-scoped"
    if main_id != memory_id:
        return "main and memory annotation endpoints address different trajectories"
    if _normalize(main_url) == _normalize(memory_url):
        return "main and memory annotation endpoints must be distinct lanes"
    return None


@dataclass(slots=True)
class PostResult:
    """Outcome of one logical annotation request. ``ok`` carries the decoded
    202 body; ``status`` distinguishes definitive rejections (4xx/409/413)
    from ambiguous transport failures (None or 5xx after retries), and
    ``definitive`` marks a status-less outcome that must not be retried."""

    ok: bool
    status: int | None = None
    body: dict | None = None
    skipped: bool = False  # breaker already open — no I/O attempted
    definitive: bool = False  # e.g. a syntactically successful but malformed response

    @property
    def cursor(self) -> int | None:
        if not self.body:
            return None
        cursor = self.body.get("role_call_cursor")
        # bool is an int subclass: a stray true/false is not a lane cursor.
        return cursor if isinstance(cursor, int) and not isinstance(cursor, bool) else None


class Annotator:
    """Synchronous stdlib annotation client (the bridge is synchronous).

    Retries only connection failures and 5xx, reusing the caller's annotation
    IDs, so an identical-id replay response is plain success; validation
    4xx/409 are never retried. One failure is counted per exhausted logical
    request, successes reset the count, and at the configured limit the
    breaker disables annotation I/O for the rest of the trace session —
    required wall-time protection, never a behavior change: native work
    continues untraced. The optional ``on_breaker`` callback fires once at
    that transition so the bridge can record the lane's death. Monotonic
    time in annotation I/O accumulates for the agent's wall-time exclusion.
    """

    def __init__(self, *, timeout: float, retries: int, max_consecutive_errors: int,
                 on_breaker: Callable[[], None] | None = None):
        if retries < 0 or max_consecutive_errors < 1:
            raise ValueError(
                f"retries must be >= 0 and max_consecutive_errors >= 1 (got {retries=}, {max_consecutive_errors=})"
            )
        self._timeout = timeout
        self._retries = retries
        self._max_consecutive_errors = max_consecutive_errors
        self._on_breaker = on_breaker
        self._consecutive_failures = 0
        self.breaker_open = False
        self.duration = 0.0

    def consume_duration(self) -> float:
        """Newly accumulated annotation seconds since the last consume."""
        value, self.duration = self.duration, 0.0
        return value

    def post(self, url: str, events: list[dict]) -> PostResult:
        """One logical request; never raises, whatever the endpoint does."""
        if self.breaker_open:
            return PostResult(ok=False, skipped=True)
        started = time.monotonic()
        try:
            # The serialization rides the guard too: a non-JSON-serializable
            # event leaf (an integration bug) must degrade to untraced native
            # work like any transport failure, never escape the "never raises"
            # contract into a caller that would misclassify it.
            body = json.dumps({"events": events}).encode("utf-8")
            result = self._attempt_loop(url, body)
        except Exception as error:
            # No exc_info: exception strings can embed the request URL, which
            # carries the bearer trajectory ID (PLAN §6.3).
            logger.warning(
                "annotation request failed unexpectedly for %s (%s)",
                sanitize_url(url),
                type(error).__name__,
            )
            result = PostResult(ok=False)
        self.duration += time.monotonic() - started
        if result.ok:
            self._consecutive_failures = 0
        else:
            self._register_failure(url)
        return result

    def _attempt_loop(self, url: str, body: bytes) -> PostResult:
        for _ in range(1 + self._retries):
            result = self._attempt(url, body)
            # Retry only connection failures and 5xx; validation 4xx/409 and
            # syntactically successful but malformed responses are definitive.
            if result.ok or result.definitive or (result.status is not None and result.status < 500):
                return result
        return result

    def _attempt(self, url: str, body: bytes) -> PostResult:
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    raise ValueError("annotation response is not a JSON object")
                return PostResult(ok=True, status=response.status, body=payload)
        except urllib.error.HTTPError as e:
            # The caller logs 409/413 with their protocol meaning; anything
            # else HTTP-level is one debug line per attempt.
            logger.debug("annotation attempt got HTTP %s from %s", e.code, sanitize_url(url))
            e.close()  # release the socket; an unread error body leaks it
            return PostResult(ok=False, status=e.code)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead, http.client.BadStatusLine) as e:
            # IncompleteRead/BadStatusLine cover the truncated/dropped-response
            # classes urlopen lets escape raw (response.read() runs in the
            # caller; RemoteDisconnected already rides OSError via
            # ConnectionResetError) — connection failures like the rest of the
            # tuple, so they take the configured retries instead of falling to
            # the blanket catch with zero retries and a full breaker strike.
            # Deterministic HTTPException subclasses (e.g. InvalidURL) stay in
            # the blanket catch: retrying them would just replay the failure.
            logger.debug("annotation attempt failed (%s) for %s", type(e).__name__, sanitize_url(url))
            return PostResult(ok=False)
        except ValueError as e:
            # A syntactically successful response with a malformed body is not
            # a retry class (connection/5xx only): retrying would just replay
            # the same parse failure.
            logger.debug("annotation response malformed from %s (%s)", sanitize_url(url), type(e).__name__)
            return PostResult(ok=False, definitive=True)

    def _register_failure(self, url: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "annotation request failed for %s (%d consecutive)",
            sanitize_url(url),
            self._consecutive_failures,
        )
        if not self.breaker_open and self._consecutive_failures >= self._max_consecutive_errors:
            self.breaker_open = True
            logger.error(
                "annotation circuit breaker tripped after %d consecutive failures; "
                "annotation I/O is disabled for the rest of this trace session",
                self._consecutive_failures,
            )
            if self._on_breaker is not None:
                # Fires exactly once, at the open transition: the bridge's
                # memory.json record that everything after this point ran
                # untraced (the trajectory cannot say it itself).
                self._on_breaker()

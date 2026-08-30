"""Reusable fixed-format side-model calls (stdlib urllib only).

Side-model calls that need a fixed output format share one machinery here:
transport, format enforcement, and parsing, with a **pydantic-model-based
contract** — the envelope has a single source of truth (the declared model)
and validation is unified behind ``model_validate``; any format restatement
in a prompt is prose in the prompts module, never a second normative copy.
The query rewriter is the first (and currently only) client.

One POST with ``response_format: {"type": "json_object"}`` — no strict
json_schema tier, no degrade memo, no corrective retry. Every failure —
transport, timeout, non-JSON output, envelope violation, empty content,
``finish_reason=length`` — is a fail-closed error result: the caller keeps
its current state.

Failure discipline: ``call_structured`` never raises, and its error strings
never embed the request URL — a trajectory-scoped lane URL carries the bearer
trajectory ID (rule 4), so errors name status codes and exception types only.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger("shared_bridge.side_model")

T = TypeVar("T", bound=BaseModel)


class SideModelConfig(BaseModel):
    """One side-model connection. The api key is a credential field (rule 4):
    excluded from dumps and never repr'd."""

    model: str
    base_url: str
    api_key: str = Field(exclude=True, repr=False)
    timeout: float = 20.0
    max_tokens: int = 1600


@dataclass(slots=True)
class StructuredCall(Generic[T]):
    """One fixed-format call: ``model`` is the pydantic envelope (the parsed
    result type), ``messages`` the chat messages."""

    model: type[T]
    messages: list[dict]


@dataclass(slots=True)
class StructuredResult(Generic[T]):
    """The fail-closed outcome: ``value`` on success, else ``error`` names the
    failure class (never the URL, never the key)."""

    value: T | None = None
    error: str | None = None


class RewrittenQuery(BaseModel):
    """The query rewriter's envelope: one single-line query of at most 300
    characters. This model is the shape's only source of truth."""

    model_config = ConfigDict(extra="forbid")

    query: str

    @field_validator("query")
    @classmethod
    def _single_line_capped(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value or len(value) > 300:
            raise ValueError("query must be one non-empty line of at most 300 characters")
        return value


def call_structured(cfg: SideModelConfig, call: StructuredCall[T]) -> StructuredResult[T]:
    """POST one chat completion and validate the answer against the envelope.

    Fail-closed on everything: a transport/HTTP/parse/validation problem is a
    ``StructuredResult`` error, never an exception — the caller decides what
    keeping its current state means.
    """
    try:
        return _call(cfg, call)
    except Exception as e:  # a bug in this module must not escape either
        logger.warning("structured side-model call failed unexpectedly (%s)", type(e).__name__)
        return StructuredResult(error=type(e).__name__)


def _call(cfg: SideModelConfig, call: StructuredCall[T]) -> StructuredResult[T]:
    payload = {
        "model": cfg.model,
        "messages": call.messages,
        "max_completion_tokens": cfg.max_tokens,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        f"{cfg.base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        e.close()  # release the socket; an unread error body leaks it
        return StructuredResult(error=f"http_{e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return StructuredResult(error=type(e).__name__)
    except ValueError:
        return StructuredResult(error="response_not_json")

    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return StructuredResult(error="missing_choices")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        # A cut-off answer can never be trusted to hold a complete envelope.
        return StructuredResult(error="truncated_length")
    content = (choice.get("message") or {}).get("content")
    if isinstance(content, list):  # content-part answers: join the text parts
        content = "".join(
            part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if not isinstance(content, str) or not content.strip():
        return StructuredResult(error="empty_content")
    try:
        parsed = json.loads(content)
    except ValueError:
        return StructuredResult(error="content_not_json")
    try:
        return StructuredResult(value=call.model.model_validate(parsed))
    except ValueError as e:
        # A valid-JSON answer with the wrong shape is an envelope violation.
        logger.debug("envelope violation: %s", e.__class__.__name__)
        return StructuredResult(error="envelope_violation")

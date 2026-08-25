"""The shipped CONVO_SEARCH_GUIDE's jq formatter, executed for real against
captured wire envelopes (offline; skipped when jq is absent on the test host).

The jq program is parsed out of the shipped constant — never a hand copy that
could drift. Parity target: the openclaw plugin variant's render loop (its
``lines.join("\\n")``), the one native variant the wire response can reproduce
(the core variant's extra Session segment is unreproducible from the wire).
The message fixture uses ROUND-STABLE scores (fourth decimal < 5): the jq
program truncates to three decimals where native ``toFixed(3)`` rounds — the
accepted, display-only divergence — so the two agree exactly here.
"""

import json
import shutil
import subprocess

import pytest

from tencentdb_bridge.prompts import CONVO_SEARCH_GUIDE

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not on the test host")

MESSAGES = [
    {
        "id": "msg-1",
        "role": "user",
        "content": "first raw message",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "score": 0.0324,  # fourth decimal < 5: truncation == toFixed(3)
    },
    {
        "id": "msg-2",
        "role": "assistant",
        "content": "second raw message",
        "timestamp": "2026-01-02T00:00:00.000Z",
        "score": 0.0161,
    },
]


def _jq_program() -> str:
    guide = CONVO_SEARCH_GUIDE.format(team_id="t", agent_id="a", user_id="u", task_id="r", limit=5)
    return guide.split("jq -r '", 1)[1].split("' | tee", 1)[0]


def _run_jq(envelope: dict) -> str:
    proc = subprocess.run(
        ["jq", "-r", _jq_program()],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _native_render(messages: list[dict]) -> str:
    # The openclaw plugin variant's render loop (conversation-search.ts):
    # header line + blank, then per message ---/header/blank/content/blank,
    # all joined with "\n".
    lines = [f"Found {len(messages)} matching message(s):", ""]
    for message in messages:
        lines.append("---")
        lines.append(f"**[{message['role']}]** [{message['timestamp']}] (score: {message['score']:.3f})")
        lines.append("")
        lines.append(message["content"])
        lines.append("")
    return "\n".join(lines)


def _envelope(messages: list[dict]) -> dict:
    return {"code": 0, "message": "ok", "request_id": "req-1", "data": {"messages": messages}}


def test_message_list_render_is_byte_identical_to_native():
    assert _run_jq(_envelope(MESSAGES)) == _native_render(MESSAGES)


def test_empty_result_is_the_native_string_plus_jqs_newline():
    # Native early-returns the string with no trailing newline; jq -r appends
    # exactly one per emitted output.
    assert _run_jq(_envelope([])) == "No matching conversation messages found.\n"


def test_error_envelope_reconstructs_the_native_failure_line():
    # Error envelopes carry no data: the null-first branch relays the
    # envelope's own message (a false "No matching conversation messages
    # found." would be a lie).
    envelope = {"code": 400, "message": "Validation failed: query is required", "request_id": "req-2"}
    assert _run_jq(envelope) == "Conversation search failed: Validation failed: query is required\n"

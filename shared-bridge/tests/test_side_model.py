"""The side-model structured-call client: the pydantic envelope is the single
source of truth, and every failure class is a fail-closed error result
(never an exception, never a URL or key in the error). Offline against the
shared capture server as a scripted chat endpoint."""

import json

from shared_bridge.side_model import (
    RewrittenQuery,
    SideModelConfig,
    StructuredCall,
    call_structured,
)


def _cfg(server, **overrides):
    return SideModelConfig(model="q-model", base_url=server.url, api_key="sk-side-model-sentinel", **overrides)


def _call(query="what the agent needs now"):
    return StructuredCall(model=RewrittenQuery, messages=[{"role": "user", "content": "rewrite"}])


def _chat_response(content, finish_reason="stop"):
    return 200, {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def test_success_roundtrip(capture_server):
    capture_server.responder = lambda path, events: _chat_response(json.dumps({"query": "what the agent needs now"}))
    result = call_structured(_cfg(capture_server), _call())
    assert result.error is None
    assert result.value == RewrittenQuery(query="what the agent needs now")
    # one POST to the lane's chat endpoint, bearer auth, json_object format
    (request,) = capture_server.requests
    assert request["path"] == "/chat/completions"


def test_envelope_violations_fail_closed(capture_server):
    for bad in (
        json.dumps({"query": "ok", "extra": 1}),  # an extra field is a violation
        json.dumps({"query": "line one\nline two"}),  # multi-line
        json.dumps({"query": "x" * 301}),  # over the 300-char cap
        json.dumps({"query": "   "}),  # blank
        json.dumps({"not_query": "x"}),
    ):
        capture_server.responder = lambda path, events, body=bad: _chat_response(body)
        result = call_structured(_cfg(capture_server), _call())
        assert result.value is None and result.error == "envelope_violation", bad


def test_declared_model_validates_through_model_validate(capture_server):
    """The envelope IS the model: a wrong-shape answer fails closed even when
    it is valid JSON."""
    capture_server.responder = lambda path, events: _chat_response('["not", "an", "object"]')
    result = call_structured(_cfg(capture_server), _call())
    assert result.value is None and result.error == "envelope_violation"


def test_failure_classes_fail_closed(capture_server):
    # HTTP error status
    capture_server.responder = lambda path, events: (500, {"error": "down"})
    assert call_structured(_cfg(capture_server), _call()).error == "http_500"
    # non-JSON response body
    capture_server.responder = lambda path, events: (200, b"not json")
    assert call_structured(_cfg(capture_server), _call()).error == "response_not_json"
    # empty content
    capture_server.responder = lambda path, events: _chat_response("")
    assert call_structured(_cfg(capture_server), _call()).error == "empty_content"
    # finish_reason=length (a cut-off answer can never hold a complete envelope)
    capture_server.responder = lambda path, events: _chat_response('{"query": "cut', finish_reason="length")
    assert call_structured(_cfg(capture_server), _call()).error == "truncated_length"
    # transport failure (nothing listening) — never raises
    down = SideModelConfig(model="m", base_url="http://127.0.0.1:1", api_key="k", timeout=0.5)
    result = call_structured(down, _call())
    assert result.value is None and result.error
    # error strings never embed the URL or the key (rule 4)
    assert "127.0.0.1" not in (result.error or "") and "sentinel" not in (result.error or "")


def test_api_key_never_serialized(capture_server):
    cfg = _cfg(capture_server)
    assert "sk-side-model-sentinel" not in repr(cfg)
    assert "sk-side-model-sentinel" not in json.dumps(cfg.model_dump())
    assert "sk-side-model-sentinel" not in json.dumps(cfg.model_dump(mode="json"))
    assert "api_key" not in cfg.model_dump()

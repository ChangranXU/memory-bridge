---
description: How memory-bridge degrades gracefully when things go wrong.
---

# Failure Discipline

memory-bridge is built on a single principle: **a failing memory system must never change what the agent does.** The bridge fails closed — when memory is off or broken, the agent runs identically to stock.

## Core rules

{% hint style="danger" %}
These rules are invariants, not guidelines. Breaking them is a bug.
{% endhint %}

1. **Nothing raises into the agent loop unless `strict: true`** — failures are contained, counted (`backend_errors`), and logged.
2. **`note_recall` never raises** — observability must not mask a model exception.
3. **Annotation failures degrade to untraced native behavior** — they never change memory state or model traffic.
4. **A strict startup failure inside `run()` still finalizes cleanly** — any SQLite handle opened before the failure is closed properly.

## Circuit breakers

Three independent circuit breakers prevent runaway failures:

| Breaker | Config key | Default | Scope |
|---|---|---|---|
| Extraction | `extract_max_consecutive_errors` | `3` | Stops periodic extraction ticks; the final flush always runs. |
| Query rewrite | `rewrite_max_consecutive_errors` | `3` | Stops the rewriter lane; the old query is kept. |
| Annotation | `annotate_max_consecutive_errors` | `3` | Stops annotation I/O for the episode; memory behavior is unchanged. |

Setting the extraction or rewrite breaker to `0` disables it (retries forever); the annotation breaker requires at least 1. Each breaker resets on a successful operation.

## Failure containment by layer

### Backend failures

When `strict=false` (the default), any exception from a backend operation is:

1. Caught and logged
2. Counted in `backend_errors`
3. The agent proceeds as if memory returned nothing

The model never sees evidence of a memory failure.

### Extraction failures

A failed extraction is counted in `extraction_errors` and triggers the circuit breaker. The extraction schedule continues on subsequent steps — a missed boundary is serviced on the next clean step.

### Recall failures

A failed search counts in both `search_errors` (the per-operation grain) and `backend_errors` (the envelope grain). The recall returns `None` for that step, so no memory message is injected. A failed search is never cached — the dirty flag stays set so the next step retries.

### Annotation failures

Annotation is pure observability. Each failure mode degrades precisely:

| Condition | Effect |
|---|---|
| Start post oversize (413) | That operation is not traced |
| Recovery conflict (409) | Memory lane disabled for the episode |
| Mid-operation rejection | No further events for that operation; memory-lane tracing is disabled for the session |
| Unreadable main-lane cursor | That delivery is skipped |
| Exhausted retries | Annotation I/O stops for the episode |

## Credential safety

Credentials are protected at every layer:

* Pydantic fields carry `exclude=True, repr=False`
* Only sanitized URLs reach artifacts and logs
* Userinfo, query strings, and fragments are stripped
* Trajectory IDs are replaced by their 16-hex SHA-256 hash prefix

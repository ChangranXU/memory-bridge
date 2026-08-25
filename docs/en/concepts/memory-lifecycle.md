---
description: The runtime contract between MemoryAgent and BaseMemoryBackend.
---

# Memory Lifecycle

This page specifies the runtime contract between `shared_bridge.agent` (`MemoryAgent`) and `shared_bridge.backend` (`BaseMemoryBackend`). It covers when each hook fires, what the shared skeleton owns, and what an integration must implement.

## The agent hook shell

`MemoryAgent` subclasses mini-swe-agent's `ProgressTrackingAgent` and hooks four points in the agent loop. When `agent.memory.enabled=false`, every hook is a pass-through and the model-visible trajectory is byte-identical to baseline.

| Hook | When it fires | What it does |
|---|---|---|
| `run(task)` | Episode start and end | Constructs the backend inside `run()` (SQLite connections are thread-affine), then runs `start()` → `set_task()` → the stock episode → `finalize()` in a `finally` block. |
| `add_messages(*msgs)` | Every trajectory message | Passes each added message to `backend.record(msgs, step=n_calls)`. |
| `step()` | After each clean agent step | Calls `backend.maybe_extract(n_calls)` and `backend.maybe_rewrite(n_calls)`. |
| `query()` | Before every model call | Calls `backend.recall_context(...)`; if content is returned, appends a **transient** user message (bypassing `add_messages`), calls the model, then removes the message by identity in a `finally` block. |

{% hint style="info" %}
Annotation I/O time is excluded from the inherited wall-time preflight by shifting the agent's episode start time forward, ensuring that a slow recorder never changes whether the next benchmark model call is attempted.
{% endhint %}

## The backend skeleton

The base class owns the control flow that every integration runs:

```text
start → set_task → record* → maybe_extract* → maybe_rewrite* → recall_context* → finalize → stats
                     (per msg)   (per N steps)   (per M steps)    (per model call)
```

### Lifecycle phases

{% stepper %}
{% step %}
### start()

Closes any previous episode's handle, resets counters, resolves settings (`_resolve_settings`; raise for expected unavailability), starts the integration system (`_startup`), mints a unique session id, and sets up tracing.
{% endstep %}

{% step %}
### set_task(task)

Stores the task text and opens the trace session.
{% endstep %}

{% step %}
### record(messages, step)

Normalizes each message (`_message_text` + `max_message_chars` truncation), filters (`_should_store`), maps roles (`_normalize_role`), stores (`_store_message`), and feeds the tracing pending-input list.
{% endstep %}

{% step %}
### maybe_extract(step)

High-water bucket schedule over `extract_every_n_steps`; `0` means final-flush-only. The circuit breaker (`extract_max_consecutive_errors` consecutive failures) stops periodic ticks; the final flush always runs.
{% endstep %}

{% step %}
### maybe_rewrite(step)

When `rewrite_every_n_steps > 0` and the cadence boundary hits, the QUERY-lane side model rewrites the recall query (fail-closed: any error keeps the previous query). A successful rewrite marks the search cache dirty.
{% endstep %}

{% step %}
### recall_context(planned_step)

Fronted by a dirty-flag cache: the search runs only when a new episode begins, an extraction tick was counted (success or failure — a failed extraction may still have written), or the recall query was rewritten. Hits are filtered through `recall_min_score`, then rank-then-fill render is bounded by `max_memories`, `max_chars_per_memory`, and `max_total_recall_chars` (truncate-to-fit with a 40-char floor; the walk continues past an unfittable line); a `_hit_budget_exempt` line renders in full outside both budgets but keeps its `max_memories` slot. Returns the rendered block or `None`.
{% endstep %}

{% step %}
### finalize()

Runs the final extraction flush, calls the integration dump (`_final_dump`), writes `memory.json`, and closes the store (`_close`). After finalize, the work surface is a silent no-op.
{% endstep %}
{% endstepper %}

## Hook contract

Integrations implement the abstract hooks and may override the optional ones. The base class never branches on integration identity.

### Abstract hooks (required)

| Hook | Responsibility |
|---|---|
| `_resolve_settings()` / `_startup(settings)` | Validate config and environment, then construct the memory system. |
| `_initial_settings()` | Settings literal for `memory.json` (splice integration keys around `_core_initial_settings()`). |
| `_store_message(role, text, step)` | Persist one normalized message. |
| `_perform_extraction(step)` | Run one extraction cycle (LLM or platform call, store mutations). |
| `_search()` | Return recall hits for the current query. |
| `_render_line(hit)` | Render one hit as a single recall line. |
| `_recall_sections()` | Integration's recall-header section text — the base-owned `_recall_header()` composes it with the shared policy preamble (compose, never override). |
| `_adapter_meta(...)` / `_memory_ref(...)` / `_trace_namespace()` | Tracing adapter trio. |
| `_final_dump()` / `_close()` | Produce the final memory dump for `memory.json`; close handles. |

### Optional hooks

| Hook | Purpose |
|---|---|
| `_COUNTERS` | Extra counter names for `memory.json`. |
| `_should_store` / `_normalize_role` / `_message_text` | Record-phase filtering and normalization. |
| `_hit_score(hit)` / `_hit_origin(hit)` | Relevance score and provenance origin of a hit (safe `None` defaults). |
| `_hit_budget_exempt(hit)` | Render the hit's line in full outside both char budgets (the line still occupies a `max_memories` slot). |
| `_snapshot_memory_state` / `_attribute_changes` | Generation-change audit. |
| `_stats_extras` / `_memory_json_fields` | Artifact extension. |

## The `memory.json` artifact

Each instance produces a `memory.json` file alongside the trajectory. The base writes the fields shown below; integrations splice extra top-level fields via `_memory_json_fields()` (CURE adds `project_id` / `db_path` / `cure_system_path`):

```json
{
  "instance_id": "...",
  "scope": "run",
  "user_id": "...",
  "session_id": "...",
  "enabled": true,
  "available": true,
  "settings": { "..." : "sanitized model names + URLs, never keys" },
  "counts": {
    "messages_recorded": 0,
    "extraction_calls": 0,
    "extraction_errors": 0,
    "recall_injections": 0,
    "backend_errors": 0,
    "search_errors": 0,
    "recall_cache_hits": 0,
    "rewrite_calls": 0,
    "rewrite_successes": 0,
    "rewrite_failures": 0
  },
  "events": [],
  "final_memories": []
}
```

{% hint style="success" %}
A healthy memory-arm instance shows `enabled: true`, `available: true`, `counts.extraction_errors: 0`, and `counts.recall_injections > 0` after the first approved memory.
{% endhint %}

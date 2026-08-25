---
description: All shared configuration keys for memory-bridge.
---

# Configuration

All memory settings live under `agent.memory.*` in `MemoryConfig`. Unknown keys fail validation (`extra="forbid"`), so typos surface immediately. Integrations subclass `MemoryConfig` to add their own fields.

## Core settings

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master on/off switch. When `false`, the agent is a no-op wrapper — byte-identical to baseline. |
| `scope` | `"run" \| "instance"` | `"run"` | `run`: store shared across the run root's instances. `instance`: fresh store per task. |
| `user_id` | `str` | `"minisweagent"` | Retrieval identity. Must not be blank. |
| `output_dir` | `str` | `""` | Per-instance artifact root. **Required** when `enabled=true`. |
| `strict` | `bool` | `false` | `true`: backend errors raise into the agent loop (for debugging). `false`: fail-closed. |

## Recording

| Key | Type | Default | Description |
|---|---|---|---|
| `max_message_chars` | `int` | `4000` | Per-message recording cap. Truncation marker included in the count. |

## Extraction

| Key | Type | Default | Description |
|---|---|---|---|
| `extract_every_n_steps` | `int` | `10` | Extraction cadence. `0` disables periodic extraction (final flush only). |
| `extract_max_consecutive_errors` | `int` | `3` | Extraction circuit breaker. `0` disables the breaker. |
| `extraction_guidelines` | `str` | `""` | Extraction policy text. `""` uses the shared default; non-empty replaces it wholesale. |

{% hint style="info" %}
Extraction guidelines are conveyed through the integration's native channel. The CURE integration appends them to its `MEMORY_POLICY_PROMPT`; mem0 sends them as `custom_instructions` on the add endpoint. Integrations whose engine accepts no custom prompt rules ignore the field (the tencentdb backend declares no channel: its stored-prompt route is scope-keyed and its extraction is async, so per-episode context cannot ride it).
{% endhint %}

## Recall

| Key | Type | Default | Description |
|---|---|---|---|
| `inject_recall` | `bool` | `true` | Master switch for recall injection. |
| `max_memories` | `int` | `10` | Upper bound on delivered memory lines. |
| `max_total_recall_chars` | `int` | `2000` | Total budget over the rendered memory lines (header not counted); 0 = off. Enforced by rank-then-fill with truncate-to-fit at a 40-char floor. |
| `max_chars_per_memory` | `int` | `0` | Per-line cap on one rendered memory line (content + provenance suffix), truncation suffix included; 0 = off (the native default). |
| `search_timeout` | `float` | `10.0` | Bound on one native search call (seconds). Only effective for network-based searches. |
| `recall_min_score` | `float \| None` | `None` | Relevance floor. Hits scoring below it are dropped before any quantity bound. `None` disables. Scale is integration-defined. |

## Query rewrite

| Key | Type | Default | Description |
|---|---|---|---|
| `rewrite_every_n_steps` | `int` | `0` | Query-rewrite cadence. `0` disables rewriting (query stays the task text). |
| `rewrite_max_consecutive_errors` | `int` | `3` | Rewrite circuit breaker. `0` disables the breaker. |
| `rewrite_model` | `str` | `""` | Rewriter model name. Falls back to `MEMORY_QUERY_MODEL` env. |
| `rewrite_base_url` | `str` | `""` | Rewriter base URL. Falls back to `MEMORY_QUERY_MODEL_URL` env. |
| `rewrite_api_key` | `str` | `""` | Rewriter API key. Falls back to `MEMORY_QUERY_API_KEY` env. |
| `rewrite_timeout` | `float` | `20.0` | Per-rewrite HTTP timeout (seconds). |
| `rewrite_max_tokens` | `int` | `1600` | Max completion tokens for the rewriter. Large default accommodates reasoning-style models. |

## Annotation

| Key | Type | Default | Description |
|---|---|---|---|
| `annotate` | `bool` | `true` | Master switch for trajectory annotation. |
| `annotate_main_url` | `str` | `""` | Explicit annotate endpoint for the MAIN lane. Falls back to env, then derivation. |
| `annotate_memory_url` | `str` | `""` | Explicit annotate endpoint for the memory lane. Falls back to env, then derivation. |
| `annotate_timeout` | `float` | `0.5` | Per-attempt HTTP timeout. |
| `annotate_retries` | `int` | `1` | Retries for connection failures and 5xx only. |
| `annotate_max_consecutive_errors` | `int` | `3` | Annotation circuit breaker. |

## Environment variables

The provider `.env` at the bundle root supplies the primary model connection:

```dotenv
MODEL=deepseek-v4-flash
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
API=openai-chat
```

### Optional environment variables

| Variable | Purpose |
|---|---|
| `QUERY_MODEL` | Recall-query rewriter model (defaults to `MODEL`). |
| `QUERY_API_KEY` | Rewriter API key (defaults to `API_KEY`). |
| `QUERY_API` | Rewriter API type (defaults to `API`). |
| `EXTRACT_MODEL` | CURE extraction model. Backend fallback only: the memory-arm driver overrides all three `EXTRACT_*` values per instance from the EXTRACT proxy lane, and the `agent.memory.extract_*` config fields take precedence over these env vars. |
| `EXTRACT_BASE_URL` | CURE extraction endpoint (same fallback semantics as `EXTRACT_MODEL`). |
| `EXTRACT_API_KEY` | CURE extraction API key (same fallback semantics as `EXTRACT_MODEL`). |
| `EMBEDDING_MODEL` | tencentdb embedding model. Part of an all-or-none quartet with `EMBEDDING_API_KEY`, `EMBEDDING_BASE_URL`, and `EMBEDDING_DIMENSIONS` — the memory-arm driver refuses a partial set (upstream would silently disable it). |
| `EMBEDDING_API_KEY` | tencentdb embedding API key (part of the all-or-none quartet — see `EMBEDDING_MODEL`). |
| `EMBEDDING_BASE_URL` | tencentdb embedding endpoint (part of the all-or-none quartet — see `EMBEDDING_MODEL`). |
| `EMBEDDING_DIMENSIONS` | tencentdb embedding dimensions (part of the all-or-none quartet — see `EMBEDDING_MODEL`). |
| `MEM0_API_KEY` | mem0 Platform API key (in `integration/mem0/.env`). |

{% hint style="warning" %}
Credentials stay in pydantic fields with `exclude=True, repr=False`. Only sanitized URLs reach artifacts and logs — userinfo/query/fragment stripped, trajectory IDs replaced by their 16-hex hash prefix.
{% endhint %}

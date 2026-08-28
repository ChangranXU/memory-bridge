---
description: Schema-v6 memory_* annotation events and lane wiring.
---

# Tracing Protocol

During a memory-arm run, the bridge annotates the recording of the [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) proxy with schema-v6 `memory_*` events, so every memory action lands in the shared trajectory as a first-class, correlated record.

The protocol is implemented once in `shared_bridge.backend`; integrations plug in through the tracing adapter hooks (`_adapter_meta` / `_memory_ref` / `_trace_namespace`).

{% hint style="info" %}
Tracing is pure observability: it never changes model traffic, memory state, or `memory.json` counters — a degraded operation leaves only a credential-free `annotation` event in the `memory.json` log — and every failure mode degrades to untraced native behavior.
{% endhint %}

## Lane wiring

Each memory-arm instance runs behind a single roster proxy with three lanes (MAIN, EXTRACT/MEMORY, QUERY):

| Lane | cure\_memory arm | mem0 arm | tencentdb arm | Traffic |
|---|---|---|---|---|
| **MAIN** | Benchmark model | Benchmark model | Benchmark model | Every model call (recorded) |
| **Secondary** | EXTRACT — CURE's extraction LLM | MEMORY — zero model calls | MEMORY — zero model calls | Extraction decisions and annotation namespace (mem0's extraction runs off-trajectory in every mode — hosted by the platform, inside the server-mode container, or in-process in library mode: the lane carries no model calls, the bridge posts the protocol from the mode's receipts; tencentdb's extraction runs inside the container, unrecorded) |
| **QUERY** | Recall-query rewriter | Recall-query rewriter | Recall-query rewriter | Rewrite calls only (recorded as raw model traffic); carries no `memory_role_bind` and emits no `memory_*` events |

The bridge resolves each lane's annotate endpoint using the following precedence:

1. Explicit `annotate_main_url` / `annotate_memory_url` config
2. `MEMORY_ANNOTATE_MAIN_URL` / `MEMORY_ANNOTATE_MEMORY_URL` environment overrides
3. Derivation from the lane's effective model base URL

An explicit URL that does not match its lane's derived prefix disables tracing for that lane with log entries — never a raise.

## Event families

| Event | When emitted | Payload |
|---|---|---|
| `memory_session` | Once per episode | Task text as an inline `ContentRef` (text + SHA-256 + size) |
| `memory_role_bind` | Once per logical role | Binds `main` and `memory` to proxy-stamped lanes |
| `memory_generate_start` | Each extraction | Exact normalized inputs (inline refs) |
| `memory_change` | Each store mutation | `create` / `update` / `noop` / `delete`, audited against snapshots |
| `memory_generate_end` | End of extraction | Produced refs, checkpoint, and mutation audit |
| `memory_search_start` | Each native search (a cache-served recall posts no search events) | Exact query text |
| `memory_search_end` | End of each native search (nothing posts for a cache-served recall or an untraced episode) | Exact ordered **rendered** refs (post floor/slice/budget) plus the portable `matched_count` (`{value, precision}` — the raw match count before floor/slice/budget, carried on completed searches only; `exact` where the native search is an unbounded full scan like CURE's, `lower_bound` for top-k/limit-bounded native searches), with `matched`/`selected`/`rendered` counts in the adapter extensions |
| `memory_delivery` | Per placed recall block | Binding to exact main-lane call(s) with placement proof |

{% hint style="warning" %}
**Delivery rule:** a recall block whose model call failed client-side before any request reached the lane records _no_ delivery. A provable `placed` claim must bind a real call interval, never `no_call` against an empty interval.
{% endhint %}

## Memory identity

Every `memory_change` and search result carries a portable `native_stable` identity, so a memory created in one instance can be joined to deliveries in a later instance using artifacts alone.

| Integration | Scheme | Format |
|---|---|---|
| CURE | `cure-sqlite-row-version-v1` | `store_id:semantic_digest` |
| mem0 | Platform memory id | Stable across UPDATE versions |
| tencentdb | `tencentdb-memorycore-l1-v1` | Gateway row id + content digest (the persona pseudo-hit rides `persona` as one evolving item) |

The tencentdb arm's agent-initiated L0 conversation searches are observed as counters only — they never produce traced refs.

## Transport and batching

Posts are batched according to the recorder's caps:

* At most **256 events** per request
* Approximately **1 MiB** per body
* A larger body gets a definitive 413
* Retries apply to connection failures and 5xx only
* Client errors (4xx/409) are never retried

## Degradation semantics

| Condition | Effect |
|---|---|
| 413 on start post | That operation is not traced |
| 409 on recovery | Memory lane disabled for the episode |
| Definitive mid-operation rejection | No further events for that operation; memory-lane tracing is disabled for the session |
| Unreadable main-lane cursor | That delivery is skipped |
| Unconfirmed/rejected delivery | Deliveries stop for the episode |
| Exhausted retries past breaker limit | Annotation I/O stops for the episode |

Annotation I/O time is excluded from the agent's wall-time budget, so a slow recorder never changes whether the next model call is attempted.

## Downstream consumers

| Tool | Purpose |
|---|---|
| `utils/validate_run.py` | Offline validation of one recorded trajectory directory (any memory arm) |
| Shared test suites | Pin the protocol through a local capture server (`shared_bridge.testing.CaptureServer`) |

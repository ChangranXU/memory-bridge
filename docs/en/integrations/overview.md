---
description: How memory systems plug into the generic bridge.
---

# Integrations

memory-bridge ships three bundled integrations and supports adding new ones as self-contained packages under `integration/`.

## How integrations work

An integration is a single package under `integration/<name>/` that provides three things:

1. **A backend** — subclasses `BaseMemoryBackend` and implements the lifecycle hooks (start, record, extract, recall, finalize)
2. **An agent** — subclasses `MemoryAgent` and binds the backend via `backend_class` / `config_class`
3. **An endpoint adapter** — implements the `MemoryEndpoint` contract for the standardized HTTP surface

The shared layer (`shared-bridge/`) never names a specific integration — this invariant is mechanically enforced by a test that scans the shared sources for integration names.

## Bundled integrations

<table data-view="cards"><thead><tr><th></th><th></th><th></th><th data-hidden data-card-target data-type="content-ref"></th></tr></thead><tbody>
<tr>
  <td><strong>CURE Memory</strong></td>
  <td>Local SQLite store with a dedicated extraction LLM. Full control over the extraction process.</td>
  <td><code>cure_memory</code></td>
  <td><a href="cure-memory.md">cure-memory</a></td>
</tr>
<tr>
  <td><strong>mem0 Platform</strong></td>
  <td>Hosted extraction via the mem0 Platform REST API. Zero-infrastructure memory management.</td>
  <td><code>mem0</code></td>
  <td><a href="mem0-platform.md">mem0-platform</a></td>
</tr>
<tr>
  <td><strong>TencentDB Agent Memory</strong></td>
  <td>Standalone MemoryCore container per run root. Server-side threshold-batched extraction, three injected recall layers (L1/L2/L3) plus an on-demand L0 conversation search.</td>
  <td><code>tencentdb</code></td>
  <td><a href="tencentdb.md">tencentdb</a></td>
</tr>
</tbody></table>

## Comparison

| Feature | CURE | mem0 | tencentdb |
|---|---|---|---|
| **Storage** | Local SQLite | Hosted platform | Per-run MemoryCore container (SQLite + FTS5) |
| **Extraction** | Dedicated LLM (EXTRACT lane) | Platform-side | Server-side pipeline (direct provider upstream) |
| **Proxy lanes** | MAIN + EXTRACT + QUERY | MAIN + MEMORY (zero model calls) + QUERY | MAIN + MEMORY (zero model calls) + QUERY |
| **Run isolation** | Run-root SQLite file | Per-run `user_id` | Per-run `user_id` + fresh container volume |
| **Scope support** | Two-layer lattice (repo-bound + general) | User-wide (no bridge-side scope) | Native two-tier (`task_id` repo + team/agent general) |
| **Endpoint adapter** | `CureMemoryEndpoint` | `Mem0Endpoint` | `TencentDBEndpoint` |
| **Dependencies** | Stdlib + pydantic | httpx (no `mem0ai` SDK) | httpx + pyyaml (no vendored SDK; Docker) |

## Search semantics

An integration implements retrieval exactly twice:

1. The backend's `_search()` — used during the evaluation arm
2. `MemoryEndpoint.search` — used for the standardized HTTP surface

Both must share one semantics (one native call, same ranking/filter behavior). The arm's measured surface is the reference; the endpoint adopts it.

{% hint style="info" %}
One deliberate carve-out: an integration may narrow the arm's `_search()` by its own storage-internal applicability layer (e.g. cure\_memory's repo/general lattice under `scope=run`). The endpoint keeps user-wide semantics since `SearchRequest` carries no project field.
{% endhint %}

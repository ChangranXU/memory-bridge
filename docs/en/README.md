---
description: A pluggable memory layer for coding agents with fail-closed semantics.
---

# memory-bridge

A pluggable memory layer for coding agents — attach a memory system to your agent's loop with fail-closed semantics: when memory is off or failing, the agent runs identically to stock.

Currently integrated with [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) and evaluated on [SWE-bench Verified](https://www.swebench.com/); the backend lifecycle and endpoint contract are agent-agnostic by design.

## Highlights

<table data-view="cards"><thead><tr><th></th><th></th><th data-hidden data-card-target data-type="content-ref"></th></tr></thead><tbody>
<tr>
  <td><strong>Fail-closed by design</strong></td>
  <td>A failing memory system degrades to untraced stock behavior. Nothing raises into the agent loop unless <code>strict: true</code>.</td>
  <td><a href="concepts/failure-discipline.md">failure-discipline</a></td>
</tr>
<tr>
  <td><strong>Generic bridge</strong></td>
  <td>The shared layer owns the entire memory lifecycle and never names a specific integration — mechanically enforced by a test.</td>
  <td><a href="concepts/architecture.md">architecture</a></td>
</tr>
<tr>
  <td><strong>Standardized endpoint</strong></td>
  <td>A unified <code>add</code> / <code>search</code> / <code>update</code> / <code>delete</code> contract with synchronous writes and <code>user_id</code> isolation.</td>
  <td><a href="api-reference/overview.md">overview</a></td>
</tr>
<tr>
  <td><strong>Three integrations</strong></td>
  <td>CURE (local SQLite + extraction LLM), mem0 (hosted Platform, self-hosted OSS server, or in-process library), and TencentDB-Agent-Memory (MemoryCore container, server-side extraction, three injected recall layers plus an on-demand conversation search). Add your own with one package.</td>
  <td><a href="integrations/overview.md">overview</a></td>
</tr>
<tr>
  <td><strong>Built-in A/B evaluation</strong></td>
  <td>Run baseline and memory arms over the same SWE-bench instances, graded by the same Docker harness.</td>
  <td><a href="pipeline/overview.md">overview</a></td>
</tr>
<tr>
  <td><strong>First-class tracing</strong></td>
  <td>Schema-v6 <code>memory_*</code> annotation events for full observability of every memory action.</td>
  <td><a href="concepts/tracing-protocol.md">tracing-protocol</a></td>
</tr>
</tbody></table>

## Key features

- **Query rewriter** — an optional side-model rewrites the recall query from raw task context into a focused search query at a configurable cadence.
- **Dirty-flag search cache** — a recall search runs only when the store has changed or the query has been rewritten, eliminating redundant hosted-search calls.

## Related projects

| Project | Role |
|---|---|
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | Prediction runner driving benchmark instances |
| [SWE-bench](https://www.swebench.com/) | Benchmark suite and local Docker evaluation harness |
| [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) | Recording proxy with roster lanes and the annotate endpoint |
| [CURE memory system](https://github.com/staymylove/CURE_memory_system) | The upstream memory system embedded in the `cure_memory` integration |
| [mem0](https://github.com/mem0ai/mem0) | The memory system behind the `mem0` integration (hosted Platform, OSS server, or in-process library) |
| [TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | The upstream memory system behind the `tencentdb` integration (MemoryCore gateway) |

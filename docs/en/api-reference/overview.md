---
description: The standardized memory endpoint contract for all integrations.
---

# API Reference

`shared_bridge.endpoint` defines a single wire contract for memory actions that every integration implements. The contract reconciles two public APIs:

* The [Agent Memory Leaderboard](https://agentmemories.ai) synchronous Add/Search semantics
* A hosted memory platform's v1 CRUD API

`shared_bridge.serve` exposes any implementation over HTTP with no web framework (stdlib `http.server`, pydantic validation).

## Base URL

```
http://127.0.0.1:8080
```

The server is intended for local or trusted-network use. There is no built-in authentication — place an authenticating reverse proxy in front before exposing it more broadly.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | [`/health`](health-check.md) | Health check |
| `POST` | [`/v1/memories/`](add-memories.md) | Add memories from messages |
| `POST` | [`/v1/memories/search/`](search-memories.md) | Search memories by query |
| `PUT` | [`/v1/memories/{id}`](update-memory.md) | Update a memory |
| `DELETE` | [`/v1/memories/{id}`](delete-memory.md) | Delete a memory |

## Contract rules

These rules apply uniformly to every adapter:

{% hint style="warning" %}
These are invariants — violating them in an adapter is a bug.
{% endhint %}

1. **Writes are synchronous** — `add`/`update`/`delete` return success only after the write is persisted and immediately searchable.
2. **`user_id` is the sole retrieval-isolation boundary** — search must only return records stored under the exact same `user_id`.
3. Search results carry at least `id` and `content`; callers ignore any undeclared extra fields.
4. Unknown memory ids raise `MemoryEndpointError(status_code=404)`; contract violations raise `MemoryEndpointError(status_code=400)`.

## Error format

All errors return a JSON body with the following shape:

```json
{
  "detail": {
    "reason": "human-readable error description"
  }
}
```

| Status code | Meaning |
|---|---|
| `400` | Invalid request body (malformed JSON, validation failure) |
| `404` | Unknown memory id or unknown route |
| `500` | Internal integration failure |

## Server architecture

The server is deliberately **single-threaded**: handlers run on the serving thread, so thread-affine stores (such as SQLite) are always accessed from the thread that created them.

```python
from shared_bridge.serve import serve_in_thread

# Factory called on the serving thread — safe for SQLite-backed endpoints
server = serve_in_thread(MyEndpoint, "127.0.0.1", 8080)
```

## Implementing the interface

```python
from shared_bridge.endpoint import (
    MemoryEndpoint,
    MemoryEndpointError,
    AddRequest, AddResponse,
    SearchRequest, SearchResponse,
    UpdateRequest, UpdateResponse,
    DeleteResponse,
)

class MyEndpoint(MemoryEndpoint):
    def add(self, request: AddRequest) -> AddResponse: ...
    def search(self, request: SearchRequest) -> SearchResponse: ...
    def update(self, memory_id: str, request: UpdateRequest,
               *, user_id: str | None = None) -> UpdateResponse: ...
    def delete(self, memory_id: str,
               *, user_id: str | None = None) -> DeleteResponse: ...
```

## Bundled adapters

| Adapter | Backs onto |
|---|---|
| `cure_memory_bridge.endpoint.CureMemoryEndpoint` | CURE SQLite store |
| `mem0_bridge.endpoint.Mem0Endpoint` | mem0 store (platform / server / library mode) |
| `tencentdb_bridge.endpoint.TencentDBEndpoint` | MemoryCore gateway REST client |

Individual adapters narrow the uniform contract where their engine requires it — for example, the tencentdb adapter rejects `infer: false`, metadata-bearing adds, and any metadata-bearing update, and the CURE adapter rejects metadata-bearing updates and metadata on verbatim (`infer: false`) adds (see [Integrations](../integrations/overview.md)).

All adapters wrap the same machinery their backends use, so the endpoint and the benchmark-time `_search` share semantics.

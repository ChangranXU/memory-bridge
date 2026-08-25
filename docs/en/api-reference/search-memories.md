---
description: Search stored memories by semantic query.
---

# Search Memories

{% hint style="info" %}
**POST** `/v1/memories/search/`
{% endhint %}

Search for memories stored under a specific `user_id`. Results are returned in relevance order, capped at `top_k`.

## Request body

```json
{
  "query": "How to run the test suite in this project",
  "user_id": "minisweagent",
  "top_k": 5
}
```

### Parameters

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | `string` | **Yes** | — | The search query. Must not be empty. |
| `user_id` | `string` | **Yes** | — | Only returns memories stored under this exact `user_id`. |
| `top_k` | `integer` | No | `10` | Maximum number of results to return (1–1000). |
| `options` | `string[]` | No | `null` | Answer choices for choice questions (context only, not used for filtering). |

## Response

```json
{
  "data": [
    {
      "id": "mem_001",
      "content": "The test suite in this project uses pytest with the -q flag for compact output.",
      "score": 0.92,
      "user_id": "minisweagent",
      "metadata": {"source": "swe-bench"},
      "created_at": "2025-08-22T10:30:00Z",
      "updated_at": "2025-08-22T10:30:00Z"
    },
    {
      "id": "mem_002",
      "content": "Tests should be run from the repository root directory.",
      "score": 0.85,
      "user_id": "minisweagent",
      "metadata": null,
      "created_at": "2025-08-22T10:25:00Z",
      "updated_at": null
    }
  ]
}
```

### MemoryRecord object

| Field | Type | Description |
|---|---|---|
| `id` | `string` | Unique memory identifier. |
| `content` | `string` | The memory text. |
| `score` | `float \| null` | Relevance score (integration-defined scale). |
| `user_id` | `string \| null` | The owning user. |
| `metadata` | `object \| null` | Arbitrary metadata. |
| `created_at` | `string \| null` | ISO 8601 creation timestamp. |
| `updated_at` | `string \| null` | ISO 8601 last-update timestamp. |

All seven fields are always present on the wire; unset values are serialized as `null`.

{% hint style="warning" %}
Score scales are integration-defined and **never comparable across integrations**. A CURE score of 0.8 and a mem0 score of 0.8 have no relation.
{% endhint %}

## Example

{% tabs %}
{% tab title="cURL" %}
```bash
curl -X POST http://127.0.0.1:8080/v1/memories/search/ \
  -H "Content-Type: application/json" \
  -d '{
    "query": "fix failing test",
    "user_id": "minisweagent",
    "top_k": 3
  }'
```
{% endtab %}

{% tab title="Python" %}
```python
import httpx

response = httpx.post("http://127.0.0.1:8080/v1/memories/search/", json={
    "query": "fix failing test",
    "user_id": "minisweagent",
    "top_k": 3,
})
for memory in response.json()["data"]:
    score = memory["score"]
    prefix = f"[{score:.2f}] " if score is not None else ""
    print(f"{prefix}{memory['content']}")
```
{% endtab %}
{% endtabs %}

## Errors

| Status | Reason |
|---|---|
| `400` | Missing or empty `query`, missing or empty `user_id`, `top_k` out of range, invalid JSON body. |
| `500` | Integration-level search failure. |

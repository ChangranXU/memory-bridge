---
description: Ingest messages and extract memories from them.
---

# Add Memories

{% hint style="info" %}
**POST** `/v1/memories/`
{% endhint %}

Ingest one or more conversation messages and extract memories from them. Writes are synchronous — success is returned only after the memories are persisted and immediately searchable.

## Request body

```json
{
  "messages": [
    {
      "role": "user",
      "content": "The test suite uses pytest with the -q flag for compact output.",
      "timestamp": 1724300000000
    },
    {
      "role": "assistant",
      "content": "Got it, I'll use pytest -q for running tests in this project."
    }
  ],
  "user_id": "minisweagent",
  "session_id": "django__django-16379-a1b2c3d4",
  "infer": true,
  "metadata": {
    "source": "swe-bench"
  }
}
```

### Parameters

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `messages` | `Message[]` | **Yes** | — | At least one message to ingest. |
| `user_id` | `string` | **Yes** | — | Retrieval-isolation identity. Must not be empty. |
| `request_id` | `string` | No | Auto-generated UUID | Echoed back in the response for correlation. |
| `session_id` | `string` | No | `null` | Logical session grouping (the tencentdb adapter ignores a caller-supplied value and mints its own). |
| `infer` | `boolean` | No | `true` | `true`: extract memories from messages. `false`: store messages verbatim (rejected with `400` by adapters whose engine has no verbatim insert, e.g. tencentdb — see [TencentDB Agent Memory](../integrations/tencentdb.md)). |
| `metadata` | `object` | No | `null` | Arbitrary key-value metadata attached to stored memories. |

### Message object

| Field | Type | Required | Description |
|---|---|---|---|
| `role` | `string` | **Yes** | Message role (e.g. `"user"`, `"assistant"`). Must not be empty. |
| `content` | `string` | **Yes** | Message content. Must not be empty. |
| `timestamp` | `integer` | No | Unix milliseconds. |

## Response

```json
{
  "success": true,
  "request_id": "f47ac10b58cc4372a5670e02b2c3d479",
  "user_id": "minisweagent",
  "session_id": "django__django-16379-a1b2c3d4",
  "memory_ids": ["mem_001", "mem_002"]
}
```

| Field | Type | Description |
|---|---|---|
| `success` | `boolean` | `true` when all memories are persisted and searchable. |
| `request_id` | `string` | Echoed from the request byte-for-byte; auto-generated (UUID hex) when omitted. |
| `user_id` | `string` | Echoed from the request, byte-for-byte. |
| `session_id` | `string \| null` | Echoed from the request (the tencentdb adapter deliberately returns a freshly minted session id instead — load-bearing for its synchronous-write guarantee). |
| `memory_ids` | `string[]` | IDs of the created memories. |

## Example

{% tabs %}
{% tab title="cURL" %}
```bash
curl -X POST http://127.0.0.1:8080/v1/memories/ \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "Always run tests with pytest -q in this repo."}
    ],
    "user_id": "minisweagent"
  }'
```
{% endtab %}

{% tab title="Python" %}
```python
import httpx

response = httpx.post("http://127.0.0.1:8080/v1/memories/", json={
    "messages": [
        {"role": "user", "content": "Always run tests with pytest -q in this repo."}
    ],
    "user_id": "minisweagent",
})
print(response.json())
```
{% endtab %}
{% endtabs %}

## Errors

| Status | Reason |
|---|---|
| `400` | Missing or empty `messages`, missing or empty `user_id`, invalid JSON body. |
| `500` | Integration-level persistence failure. |

Adapters may reject with `400` for integration-specific reasons beyond the generic validation above — for example, the tencentdb adapter rejects `infer: false`, roles other than `user`/`assistant`, an add carrying no `user` round, and messages over the gateway's 8192-UTF-16-unit content cap (see [TencentDB Agent Memory](../integrations/tencentdb.md)).

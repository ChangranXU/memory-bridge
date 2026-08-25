---
description: Update an existing memory's text or metadata.
---

# Update Memory

{% hint style="info" %}
**PUT** `/v1/memories/{id}`
{% endhint %}

Replace the text and/or metadata of an existing memory. The write is synchronous — success is returned only after the update is persisted.

## Path parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `id` | `string` | **Yes** | The memory ID to update. |

## Request body

```json
{
  "text": "Updated: always run tests with pytest -q --tb=short in this repo.",
  "metadata": {
    "source": "swe-bench",
    "updated_reason": "added traceback format"
  }
}
```

### Parameters

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | `string` | No | New memory text. Must not be empty if provided. At least one of `text` or `metadata` should be provided. |
| `metadata` | `object` | No | New metadata (replaces existing metadata entirely). |

### Per-integration support

| Adapter | Update support |
|---|---|
| mem0 | `text` and/or `metadata`. |
| CURE | `text` required — no standalone metadata; any request without `text` is rejected with `400`. |
| tencentdb | `text` alone — any metadata-bearing update (including `text` + `metadata`) is rejected with `400`. |

## Response

```json
{
  "success": true,
  "memory": {
    "id": "mem_001",
    "content": "Updated: always run tests with pytest -q --tb=short in this repo.",
    "score": null,
    "user_id": "minisweagent",
    "metadata": {
      "source": "swe-bench",
      "updated_reason": "added traceback format"
    },
    "created_at": "2025-08-22T10:30:00Z",
    "updated_at": "2025-08-22T11:00:00Z"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `success` | `boolean` | `true` when the update is persisted. |
| `memory` | `MemoryRecord` | The updated memory record. |

## Example

{% tabs %}
{% tab title="cURL" %}
```bash
curl -X PUT http://127.0.0.1:8080/v1/memories/mem_001 \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Run tests with pytest -q --tb=short from the repo root."
  }'
```
{% endtab %}

{% tab title="Python" %}
```python
import httpx

response = httpx.put("http://127.0.0.1:8080/v1/memories/mem_001", json={
    "text": "Run tests with pytest -q --tb=short from the repo root.",
})
print(response.json())
```
{% endtab %}
{% endtabs %}

## Errors

| Status | Reason |
|---|---|
| `400` | Empty `text` field, invalid JSON body, adapter-level rejections (mem0: neither `text` nor `metadata` provided; CURE: `text` absent; tencentdb: any `metadata` present). |
| `404` | Unknown memory ID. |
| `500` | Integration-level update failure. |

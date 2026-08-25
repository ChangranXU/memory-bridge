---
description: Delete a single memory by ID.
---

# Delete Memory

{% hint style="info" %}
**DELETE** `/v1/memories/{id}`
{% endhint %}

Delete a single memory by its ID. The deletion is synchronous — success is returned only after the deletion is persisted (the memory no longer appears in search results).

## Path parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `id` | `string` | **Yes** | The memory ID to delete. |

## Request body

No request body required.

## Response

```json
{
  "success": true,
  "memory_id": "mem_001"
}
```

| Field | Type | Description |
|---|---|---|
| `success` | `boolean` | `true` when the deletion is complete. |
| `memory_id` | `string` | The ID of the deleted memory. |

## Example

{% tabs %}
{% tab title="cURL" %}
```bash
curl -X DELETE http://127.0.0.1:8080/v1/memories/mem_001
```
{% endtab %}

{% tab title="Python" %}
```python
import httpx

response = httpx.delete("http://127.0.0.1:8080/v1/memories/mem_001")
print(response.json())
```
{% endtab %}
{% endtabs %}

## Errors

| Status | Reason |
|---|---|
| `404` | Unknown memory ID. |
| `500` | Integration-level deletion failure. |

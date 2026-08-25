---
description: Check if the memory endpoint server is running.
---

# Health Check

{% hint style="info" %}
**GET** `/health`
{% endhint %}

Returns the server status. This endpoint is unauthenticated and always available while the server is running.

## Request

No parameters or body required.

## Response

```json
{
  "status": "ok"
}
```

| Field | Type | Description |
|---|---|---|
| `status` | `string` | Always `"ok"` when the server is reachable. |

## Example

{% tabs %}
{% tab title="cURL" %}
```bash
curl http://127.0.0.1:8080/health
```
{% endtab %}

{% tab title="Python" %}
```python
import httpx

response = httpx.get("http://127.0.0.1:8080/health")
assert response.json()["status"] == "ok"
```
{% endtab %}
{% endtabs %}

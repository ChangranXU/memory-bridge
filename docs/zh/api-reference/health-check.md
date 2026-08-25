---
description: 检查记忆端点服务器是否正在运行。
---

# 健康检查

{% hint style="info" %}
**GET** `/health`
{% endhint %}

返回服务器状态。此端点无需认证，在服务器运行期间始终可用。

## 请求

无需参数或请求体。

## 响应

```json
{
  "status": "ok"
}
```

| 字段 | 类型 | 描述 |
|---|---|---|
| `status` | `string` | 服务器可达时始终为 `"ok"`。 |

## 示例

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

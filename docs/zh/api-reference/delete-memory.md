---
description: 按 ID 删除单条记忆。
---

# 删除记忆

{% hint style="info" %}
**DELETE** `/v1/memories/{id}`
{% endhint %}

按 ID 删除单条记忆。删除是同步的——仅在删除被持久化后（记忆不再出现在搜索结果中）才返回成功。

## 路径参数

| 参数 | 类型 | 必需 | 描述 |
|---|---|---|---|
| `id` | `string` | **是** | 要删除的记忆 ID。 |

## 请求体

无需请求体。

## 响应

```json
{
  "success": true,
  "memory_id": "mem_001"
}
```

| 字段 | 类型 | 描述 |
|---|---|---|
| `success` | `boolean` | 删除完成时为 `true`。 |
| `memory_id` | `string` | 已删除记忆的 ID。 |

## 示例

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

## 错误

| 状态码 | 原因 |
|---|---|
| `404` | 未知的记忆 ID。 |
| `500` | 集成级别的删除故障。 |

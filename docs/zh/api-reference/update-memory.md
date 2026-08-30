---
description: 更新已有记忆的文本或元数据。
---

# 更新记忆

{% hint style="info" %}
**PUT** `/v1/memories/{id}`
{% endhint %}

替换已有记忆的文本和/或元数据。写入是同步的——仅在更新持久化后才返回成功。

## 路径参数

| 参数 | 类型 | 必需 | 描述 |
|---|---|---|---|
| `id` | `string` | **是** | 要更新的记忆 ID。 |

## 请求体

```json
{
  "text": "Updated: always run tests with pytest -q --tb=short in this repo.",
  "metadata": {
    "source": "swe-bench",
    "updated_reason": "added traceback format"
  }
}
```

### 参数

| 字段 | 类型 | 必需 | 描述 |
|---|---|---|---|
| `text` | `string` | 否 | 新的记忆文本。如果提供则不能为空。应至少提供 `text` 或 `metadata` 之一。 |
| `metadata` | `object` | 否 | 新的元数据（完全替换现有元数据）。 |

### 各集成支持情况

| 适配器 | 更新支持 |
|---|---|
| mem0 | `text` 和/或 `metadata`。 |
| CURE | 仅接受单独的 `text`——不存储独立元数据；任何携带 `metadata` 的更新（包括 `text` + `metadata`）均以 `400` 拒绝。 |
| tencentdb | 仅接受单独的 `text`——任何携带 `metadata` 的更新（包括 `text` + `metadata`）均以 `400` 拒绝。 |

## 响应

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

| 字段 | 类型 | 描述 |
|---|---|---|
| `success` | `boolean` | 更新持久化时为 `true`。 |
| `memory` | `MemoryRecord` | 更新后的记忆记录。 |

## 示例

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

## 错误

| 状态码 | 原因 |
|---|---|
| `400` | 空的 `text` 字段，无效的 JSON body，适配器层面的拒绝（mem0：`text` 与 `metadata` 均未提供；CURE：缺少 `text` 或携带任何 `metadata`；tencentdb：携带任何 `metadata`）。 |
| `404` | 未知的记忆 ID。 |
| `500` | 集成级别的更新故障。 |

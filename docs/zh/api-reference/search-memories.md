---
description: 按语义查询搜索已存储的记忆。
---

# 搜索记忆

{% hint style="info" %}
**POST** `/v1/memories/search/`
{% endhint %}

搜索存储在特定 `user_id` 下的记忆。结果按相关度排序，上限为 `top_k`。

## 请求体

```json
{
  "query": "How to run the test suite in this project",
  "user_id": "minisweagent",
  "top_k": 5
}
```

### 参数

| 字段 | 类型 | 必需 | 默认值 | 描述 |
|---|---|---|---|---|
| `query` | `string` | **是** | — | 搜索查询。不能为空。 |
| `user_id` | `string` | **是** | — | 仅返回存储在此 `user_id` 下的记忆。 |
| `top_k` | `integer` | 否 | `10` | 返回结果的最大数量（1–1000）。 |
| `options` | `string[]` | 否 | `null` | 选择题的答案选项（仅作上下文，不用于过滤）。 |

## 响应

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

### MemoryRecord 对象

| 字段 | 类型 | 描述 |
|---|---|---|
| `id` | `string` | 唯一记忆标识符。 |
| `content` | `string` | 记忆文本。 |
| `score` | `float \| null` | 相关度分数（尺度由集成定义）。 |
| `user_id` | `string \| null` | 所属用户。 |
| `metadata` | `object \| null` | 任意元数据。 |
| `created_at` | `string \| null` | ISO 8601 创建时间戳。 |
| `updated_at` | `string \| null` | ISO 8601 最后更新时间戳。 |

全部七个字段在响应中始终存在；未设置的值序列化为 `null`。

{% hint style="warning" %}
分数尺度由集成定义，**不能跨集成比较**。CURE 的 0.8 分和 mem0 的 0.8 分没有关系。
{% endhint %}

## 示例

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

## 错误

| 状态码 | 原因 |
|---|---|
| `400` | 缺少或为空的 `query`，缺少或为空的 `user_id`，`top_k` 超出范围，无效的 JSON body。 |
| `500` | 集成级别的搜索故障。 |

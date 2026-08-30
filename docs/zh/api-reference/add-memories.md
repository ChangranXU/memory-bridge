---
description: 摄入消息并从中提取记忆。
---

# 添加记忆

{% hint style="info" %}
**POST** `/v1/memories/`
{% endhint %}

摄入一条或多条对话消息并从中提取记忆。写入是同步的——仅在记忆持久化且可立即搜索后才返回成功。

## 请求体

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

### 参数

| 字段 | 类型 | 必需 | 默认值 | 描述 |
|---|---|---|---|---|
| `messages` | `Message[]` | **是** | — | 至少一条待摄入的消息。 |
| `user_id` | `string` | **是** | — | 检索隔离身份。不能为空。 |
| `request_id` | `string` | 否 | 自动生成 UUID | 在响应中回显以供关联。 |
| `session_id` | `string` | 否 | `null` | 逻辑会话分组（tencentdb 适配器会忽略调用方提供的值，改为自行生成）。 |
| `infer` | `boolean` | 否 | `true` | `true`：从消息中提取记忆。`false`：逐字存储消息（引擎不支持逐字插入的适配器会以 `400` 拒绝，例如 tencentdb——参见 [TencentDB Agent Memory](../integrations/tencentdb.md)）。 |
| `metadata` | `object` | 否 | `null` | 附加到存储记忆的任意键值元数据。引擎没有元数据通道的适配器会以 `400` 拒绝携带元数据的 add，而非静默丢弃（tencentdb 总是拒绝；CURE 仅在 `infer: false` 时拒绝——`infer: true` 时元数据会进入提取输入）。 |

### Message 对象

| 字段 | 类型 | 必需 | 描述 |
|---|---|---|---|
| `role` | `string` | **是** | 消息角色（如 `"user"`、`"assistant"`）。不能为空。 |
| `content` | `string` | **是** | 消息内容。不能为空。 |
| `timestamp` | `integer` | 否 | Unix 毫秒时间戳。 |

## 响应

```json
{
  "success": true,
  "request_id": "f47ac10b58cc4372a5670e02b2c3d479",
  "user_id": "minisweagent",
  "session_id": "django__django-16379-a1b2c3d4",
  "memory_ids": ["mem_001", "mem_002"]
}
```

| 字段 | 类型 | 描述 |
|---|---|---|
| `success` | `boolean` | 所有记忆持久化且可搜索时为 `true`。 |
| `request_id` | `string` | 从请求中逐字节回显；省略时自动生成（UUID 十六进制）。 |
| `user_id` | `string` | 从请求中逐字节回显。 |
| `session_id` | `string \| null` | 从请求中回显（tencentdb 适配器有意改为返回新生成的 session id——这对其同步写入保证至关重要）。 |
| `memory_ids` | `string[]` | 创建的记忆的 ID。 |

## 示例

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

## 错误

| 状态码 | 原因 |
|---|---|
| `400` | 缺少或为空的 `messages`，缺少或为空的 `user_id`，无效的 JSON body。 |
| `500` | 集成级别的持久化故障。 |

除上述通用校验外，适配器还可能因集成特有的原因以 `400` 拒绝请求——例如，tencentdb 适配器拒绝 `infer: false`、`user`/`assistant` 之外的角色、不含 `user` 轮次的添加，以及超过网关 8192 个 UTF-16 码元内容上限的消息（参见 [TencentDB Agent Memory](../integrations/tencentdb.md)）。

---
description: 所有集成的标准化记忆端点契约。
---

# API 参考

`shared_bridge.endpoint` 定义了每个集成必须实现的统一记忆动作契约。该契约调和了两个公开 API：

* [Agent Memory Leaderboard](https://agentmemories.ai) 同步 Add/Search 语义
* 托管记忆平台的 v1 CRUD API

`shared_bridge.serve` 在不依赖 Web 框架的情况下将任何实现暴露为 HTTP 服务（标准库 `http.server`，pydantic 校验）。

## 基础 URL

```
http://127.0.0.1:8080
```

服务器仅供本地或可信网络使用。没有内建认证——在更广泛暴露之前，请在前端放置认证反向代理。

## 端点

| 方法 | 路径 | 描述 |
|---|---|---|
| `GET` | [`/health`](health-check.md) | 健康检查 |
| `POST` | [`/v1/memories/`](add-memories.md) | 从消息添加记忆 |
| `POST` | [`/v1/memories/search/`](search-memories.md) | 按查询搜索记忆 |
| `PUT` | [`/v1/memories/{id}`](update-memory.md) | 更新记忆 |
| `DELETE` | [`/v1/memories/{id}`](delete-memory.md) | 删除记忆 |

## 契约规则

这些规则统一适用于每个适配器：

{% hint style="warning" %}
这些是不变量——在适配器中违反它们就是 bug。
{% endhint %}

1. **写入是同步的**——`add`/`update`/`delete` 仅在写入持久化且可立即搜索后才返回成功。
2. **`user_id` 是唯一的检索隔离边界**——搜索必须只返回存储在完全相同 `user_id` 下的记录。
3. 搜索结果至少携带 `id` 和 `content`；调用者忽略任何未声明的额外字段。
4. 未知记忆 id 抛出 `MemoryEndpointError(status_code=404)`；契约违规抛出 `MemoryEndpointError(status_code=400)`。

## 错误格式

所有错误返回以下形状的 JSON body：

```json
{
  "detail": {
    "reason": "人类可读的错误描述"
  }
}
```

| 状态码 | 含义 |
|---|---|
| `400` | 无效的请求体（格式错误的 JSON、验证失败） |
| `404` | 未知的记忆 id 或未知路由 |
| `500` | 内部集成故障 |

## 服务器架构

服务器故意**单线程**运行：处理器在服务线程上执行，因此线程绑定的存储（如 SQLite）始终从创建它们的线程访问。

```python
from shared_bridge.serve import serve_in_thread

# 工厂在服务线程上调用——对 SQLite 后端安全
server = serve_in_thread(MyEndpoint, "127.0.0.1", 8080)
```

## 实现接口

```python
from shared_bridge.endpoint import (
    MemoryEndpoint,
    MemoryEndpointError,
    AddRequest, AddResponse,
    SearchRequest, SearchResponse,
    UpdateRequest, UpdateResponse,
    DeleteResponse,
)

class MyEndpoint(MemoryEndpoint):
    def add(self, request: AddRequest) -> AddResponse: ...
    def search(self, request: SearchRequest) -> SearchResponse: ...
    def update(self, memory_id: str, request: UpdateRequest,
               *, user_id: str | None = None) -> UpdateResponse: ...
    def delete(self, memory_id: str,
               *, user_id: str | None = None) -> DeleteResponse: ...
```

## 内置适配器

| 适配器 | 对接 |
|---|---|
| `cure_memory_bridge.endpoint.CureMemoryEndpoint` | CURE SQLite 存储 |
| `mem0_bridge.endpoint.Mem0Endpoint` | mem0 Platform REST 客户端 |
| `tencentdb_bridge.endpoint.TencentDBEndpoint` | MemoryCore 网关 REST 客户端 |

个别适配器会在其引擎要求时收窄统一契约——例如，tencentdb 适配器拒绝 `infer: false` 以及任何携带 `metadata` 的更新，CURE 适配器要求更新时必须提供 `text`（参见[集成概览](../integrations/overview.md)）。

所有适配器包装了与其后端相同的机制，因此端点与基准运行时的 `_search` 共享语义。

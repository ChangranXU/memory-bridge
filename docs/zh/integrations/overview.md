---
description: 记忆系统如何接入通用桥接层。
---

# 集成

memory-bridge 附带三个内置集成，并支持以 `integration/` 下的独立包形式添加新集成。

## 集成的工作方式

集成是 `integration/<name>/` 下的单个包，提供三样东西：

1. **后端**——继承 `BaseMemoryBackend` 并实现生命周期钩子（start、record、extract、recall、finalize）
2. **智能体**——继承 `MemoryAgent` 并通过 `backend_class` / `config_class` 绑定后端
3. **端点适配器**——实现 `MemoryEndpoint` 契约，提供标准化 HTTP 接口

共享层（`shared-bridge/`）从不指名任何特定集成——这一不变量由扫描共享源代码中集成名称的测试机械化地强制执行。

## 内置集成

<table data-view="cards"><thead><tr><th></th><th></th><th></th><th data-hidden data-card-target data-type="content-ref"></th></tr></thead><tbody>
<tr>
  <td><strong>CURE Memory</strong></td>
  <td>本地 SQLite 存储与专用提取 LLM。对提取过程拥有完全控制权。</td>
  <td><code>cure_memory</code></td>
  <td><a href="cure-memory.md">cure-memory</a></td>
</tr>
<tr>
  <td><strong>mem0</strong></td>
  <td>mem0 的三种部署模式：托管 Platform、每 run 自托管 OSS server 或进程内 library。任何模式下提取都不进入轨迹。</td>
  <td><code>mem0</code></td>
  <td><a href="mem0-platform.md">mem0-platform</a></td>
</tr>
<tr>
  <td><strong>TencentDB Agent Memory</strong></td>
  <td>每个 run root 一个独立 MemoryCore 容器。服务端按阈值分批提取，三个注入召回层（L1/L2/L3）加按需 L0 对话搜索。</td>
  <td><code>tencentdb</code></td>
  <td><a href="tencentdb.md">tencentdb</a></td>
</tr>
</tbody></table>

## 对比

| 特性 | CURE | mem0 | tencentdb |
|---|---|---|---|
| **存储** | 本地 SQLite | 托管平台、每 run server 容器或进程内存储（按模式） | 每 run 一个 MemoryCore 容器（SQLite + FTS5） |
| **提取** | 专用 LLM（EXTRACT lane） | 引擎侧：托管 / 容器内 / 进程内（按模式） | 服务端流水线（直连提供商上游） |
| **代理 lane** | MAIN + EXTRACT + QUERY | MAIN + MEMORY（零模型调用）+ QUERY | MAIN + MEMORY（零模型调用）+ QUERY |
| **运行隔离** | run-root SQLite 文件 | 每次运行的 `user_id`（server/library 模式下另有全新每 run 存储） | 每 run `user_id` + 全新容器数据卷 |
| **作用域支持** | 两层适用性结构（仓库级 + 通用） | 用户级（无桥接端作用域） | 原生两层（`task_id` 仓库 + team/agent 通用） |
| **端点适配器** | `CureMemoryEndpoint` | `Mem0Endpoint` | `TencentDBEndpoint` |
| **依赖** | 标准库 + pydantic | httpx（`mem0ai` 仅经可选依赖组 `mem0-library`，library 模式） | httpx + pyyaml（无上游 SDK；需 Docker） |

## 搜索语义

集成实现检索恰好两次：

1. 后端的 `_search()`——用于评测臂
2. `MemoryEndpoint.search`——用于标准化 HTTP 接口

两者必须共享一个语义（一次原生调用，相同的排序/过滤行为）。臂的测量表面是参考；端点采纳它。

{% hint style="info" %}
一个有意的例外：集成可以通过其自身的存储内部适用层缩窄臂的 `_search()`（例如 cure\_memory 在 `scope=run` 下的仓库/通用适用性结构）。由于 `SearchRequest` 不携带 project 字段，端点保持用户级语义。
{% endhint %}

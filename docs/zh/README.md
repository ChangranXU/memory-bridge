---
description: 为编程智能体设计的可插拔记忆层，具有失效封闭语义。
---

# memory-bridge

为编程智能体设计的可插拔记忆层——将记忆系统接入智能体循环，具有失效封闭语义：当记忆关闭或故障时，智能体的行为与原始版本完全一致。

目前已集成 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)，并在 [SWE-bench Verified](https://www.swebench.com/) 上进行评测；后端生命周期和端点契约在设计上与具体智能体无关。

## 亮点

<table data-view="cards"><thead><tr><th></th><th></th><th data-hidden data-card-target data-type="content-ref"></th></tr></thead><tbody>
<tr>
  <td><strong>失效封闭设计</strong></td>
  <td>记忆系统故障时退化为无追踪的原始行为。除非 <code>strict: true</code>，否则不会向智能体循环抛出异常。</td>
  <td><a href="concepts/failure-discipline.md">failure-discipline</a></td>
</tr>
<tr>
  <td><strong>通用桥接</strong></td>
  <td>共享层拥有完整的记忆生命周期，从不指名任何特定集成——由测试机械化地强制执行。</td>
  <td><a href="concepts/architecture.md">architecture</a></td>
</tr>
<tr>
  <td><strong>标准化端点</strong></td>
  <td>统一的 <code>add</code> / <code>search</code> / <code>update</code> / <code>delete</code> 契约，具备同步写入和 <code>user_id</code> 隔离。</td>
  <td><a href="api-reference/overview.md">overview</a></td>
</tr>
<tr>
  <td><strong>三个集成</strong></td>
  <td>CURE（本地 SQLite + 提取 LLM）、mem0（托管 Platform、自托管 OSS server 或进程内 library）与 TencentDB-Agent-Memory（MemoryCore 容器，服务端提取，三个注入召回层加按需对话搜索）。只需一个包即可添加自己的集成。</td>
  <td><a href="integrations/overview.md">overview</a></td>
</tr>
<tr>
  <td><strong>内建 A/B 评测</strong></td>
  <td>在相同的 SWE-bench 实例上运行基线臂和记忆臂，使用同一 Docker 评测框架打分。</td>
  <td><a href="pipeline/overview.md">overview</a></td>
</tr>
<tr>
  <td><strong>一等追踪</strong></td>
  <td>schema-v6 <code>memory_*</code> 标注事件，对每个记忆动作具有完整可观测性。</td>
  <td><a href="concepts/tracing-protocol.md">tracing-protocol</a></td>
</tr>
</tbody></table>

## 核心特性

- **查询改写器**——可选的 side-model 按可配置节奏将原始任务上下文改写为聚焦的搜索查询。
- **脏标记检索缓存**——仅当存储发生变化或查询被改写时才执行召回搜索，消除冗余的托管搜索调用。

## 相关项目

| 项目 | 角色 |
|---|---|
| [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | 驱动基准实例的预测运行器 |
| [SWE-bench](https://www.swebench.com/) | 基准测试套件与本地 Docker 评测框架 |
| [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) | 带 roster lane 与 annotate 端点的录制代理 |
| [CURE 记忆系统](https://github.com/staymylove/CURE_memory_system) | `cure_memory` 集成中内嵌的上游记忆系统 |
| [mem0](https://github.com/mem0ai/mem0) | `mem0` 集成背后的记忆系统（托管 Platform、OSS server 或进程内 library） |
| [TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | `tencentdb` 集成背后的上游记忆系统（MemoryCore 网关） |

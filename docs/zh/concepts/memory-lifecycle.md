---
description: MemoryAgent 与 BaseMemoryBackend 之间的运行时契约。
---

# 记忆生命周期

本页规定 `shared_bridge.agent`（`MemoryAgent`）与 `shared_bridge.backend`（`BaseMemoryBackend`）之间的运行时契约。涵盖每个钩子的触发时机、共享骨架拥有的职责，以及集成必须实现的内容。

## 智能体钩子外壳

`MemoryAgent` 继承 mini-swe-agent 的 `ProgressTrackingAgent`，在智能体循环的四个点挂钩。当 `agent.memory.enabled=false` 时，每个钩子都是直通的，模型可见的轨迹与基线逐字节一致。

| 钩子 | 触发时机 | 行为 |
|---|---|---|
| `run(task)` | Episode 开始和结束 | 在 `run()` 内构造后端（SQLite 连接与线程绑定），然后运行 `start()` → `set_task()` → 标准 episode → `finalize()`（在 `finally` 块中）。 |
| `add_messages(*msgs)` | 每条轨迹消息 | 将每条添加的消息传递给 `backend.record(msgs, step=n_calls)`。 |
| `step()` | 每个干净的智能体步骤后 | 调用 `backend.maybe_extract(n_calls)` 和 `backend.maybe_rewrite(n_calls)`。 |
| `query()` | 每次模型调用前 | 调用 `backend.recall_context(...)`；如果有内容返回，添加一条**瞬态**用户消息（绕过 `add_messages`），调用模型，然后在 `finally` 块中按身份移除该消息。 |

{% hint style="info" %}
标注 I/O 时间通过将智能体的 episode 开始时间后移，从继承的 wall-time 预检中排除，确保慢速录制器永远不会改变下一次基准模型调用是否执行。
{% endhint %}

## 后端骨架

基类拥有每个集成运行的控制流：

```text
start → set_task → record* → maybe_extract* → maybe_rewrite* → recall_context* → finalize → stats
                     (每条消息)   (每 N 步)      (每 M 步)        (每次模型调用)
```

### 生命周期阶段

{% stepper %}
{% step %}
### start()

关闭前一 episode 的句柄，重置计数器，解析设置（`_resolve_settings`；对预期的不可用性抛出异常），启动集成系统（`_startup`），生成唯一的 session id，并设置追踪。
{% endstep %}

{% step %}
### set_task(task)

存储任务文本并打开追踪会话。
{% endstep %}

{% step %}
### record(messages, step)

规范化每条消息（`_message_text` + `max_message_chars` 截断），过滤（`_should_store`），映射角色（`_normalize_role`），存储（`_store_message`），并馈入追踪的待处理输入列表。
{% endstep %}

{% step %}
### maybe_extract(step)

基于 `extract_every_n_steps` 的高水位桶调度；`0` 表示仅最终刷新。熔断器（`extract_max_consecutive_errors` 次连续失败）停止周期性触发；最终刷新始终运行。
{% endstep %}

{% step %}
### maybe_rewrite(step)

当 `rewrite_every_n_steps > 0` 且到达频率边界时，QUERY lane 的 side-model 改写召回查询（失效封闭：任何错误都保留之前的查询）。成功的改写会将检索缓存标记为脏。
{% endstep %}

{% step %}
### recall_context(planned_step)

由脏标记缓存前置：仅当新 episode 开始、一次提取 tick 被计数（无论成功或失败——失败的提取也可能已经写入），或召回查询被改写时才运行搜索。命中经 `recall_min_score` 过滤，然后排名填充渲染受 `max_memories`、`max_chars_per_memory` 与 `max_total_recall_chars` 约束（剩余预算 ≥ 40 字符时截断填充，无法容纳的行跳过且遍历继续）；`_hit_budget_exempt` 行在两项预算之外完整渲染，但仍占用一个 `max_memories` 名额。返回渲染块或 `None`。
{% endstep %}

{% step %}
### finalize()

运行最终提取刷新，调用集成导出（`_final_dump`），写入 `memory.json`，并关闭存储（`_close`）。finalize 后，工作面是静默的空操作。
{% endstep %}
{% endstepper %}

## 钩子契约

集成实现抽象钩子并可覆盖可选钩子。基类从不根据集成身份进行分支。

### 抽象钩子（必须实现）

| 钩子 | 职责 |
|---|---|
| `_resolve_settings()` / `_startup(settings)` | 验证配置和环境，然后构造记忆系统。 |
| `_initial_settings()` | `memory.json` 的 settings 字面量（围绕 `_core_initial_settings()` 拼接集成键）。 |
| `_store_message(role, text, step)` | 持久化一条规范化消息。 |
| `_perform_extraction(step)` | 运行一次提取周期（LLM 或平台调用、存储变更）。 |
| `_search()` | 返回当前查询的召回命中。 |
| `_render_line(hit)` | 将一个命中渲染为单行召回文本。 |
| `_recall_sections()` | 集成的召回 header 段落文本——由基类拥有的 `_recall_header()` 将其与共享策略前言组合（只组合，永不覆盖）。 |
| `_adapter_meta(...)` / `_memory_ref(...)` / `_trace_namespace()` | 追踪适配器三元组。 |
| `_final_dump()` / `_close()` | 为 `memory.json` 生成最终记忆导出；关闭句柄。 |

### 可选钩子

| 钩子 | 用途 |
|---|---|
| `_COUNTERS` | `memory.json` 的额外计数器名。 |
| `_should_store` / `_normalize_role` / `_message_text` | 录制阶段的过滤和规范化。 |
| `_hit_score(hit)` / `_hit_origin(hit)` | 命中的相关度分数和来源（安全的 `None` 默认值）。 |
| `_hit_budget_exempt(hit)` | 使命中行在两项字符预算之外完整渲染（该行仍占用一个 `max_memories` 名额）。 |
| `_snapshot_memory_state` / `_attribute_changes` | 世代变更审计。 |
| `_stats_extras` / `_memory_json_fields` | 产物扩展。 |

## `memory.json` 产物

每个实例在轨迹旁生成一个 `memory.json` 文件。基类写入下列字段；集成可通过 `_memory_json_fields()` 拼接额外的顶层字段（CURE 会加入 `project_id` / `db_path` / `cure_system_path`）：

```json
{
  "instance_id": "...",
  "scope": "run",
  "user_id": "...",
  "session_id": "...",
  "enabled": true,
  "available": true,
  "settings": { "..." : "脱敏的模型名 + URL，绝不包含密钥" },
  "counts": {
    "messages_recorded": 0,
    "extraction_calls": 0,
    "extraction_errors": 0,
    "recall_injections": 0,
    "backend_errors": 0,
    "search_errors": 0,
    "recall_cache_hits": 0,
    "rewrite_calls": 0,
    "rewrite_successes": 0,
    "rewrite_failures": 0
  },
  "events": [],
  "final_memories": []
}
```

{% hint style="success" %}
健康的记忆臂实例应显示 `enabled: true`、`available: true`、`counts.extraction_errors: 0`，以及首次批准记忆后 `counts.recall_injections > 0`。
{% endhint %}

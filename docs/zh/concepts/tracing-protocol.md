---
description: Schema-v6 memory_* 标注事件与 lane 接线方式。
---

# 追踪协议

在记忆臂运行期间，桥接层使用 schema-v6 `memory_*` 事件标注 [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) 代理的录制，使每个记忆动作作为一等关联记录落入共享轨迹中。

该协议在 `shared_bridge.backend` 中实现一次；集成通过追踪适配器钩子（`_adapter_meta` / `_memory_ref` / `_trace_namespace`）接入。

{% hint style="info" %}
追踪是纯可观测性：它永不改变模型流量、记忆状态或 `memory.json` 计数器——降级的操作只会在 `memory.json` 日志中留下一条不含凭据的 `annotation` 事件——且每种故障模式都退化为无追踪的原始行为。
{% endhint %}

## Lane 接线

每个记忆臂实例在单个 roster 代理后运行，包含三个 lane（MAIN、EXTRACT/MEMORY、QUERY）：

| Lane | cure\_memory 臂 | mem0 臂 | tencentdb 臂 | 流量 |
|---|---|---|---|---|
| **MAIN** | 基准模型 | 基准模型 | 基准模型 | 每次模型调用（录制） |
| **Secondary** | EXTRACT——CURE 的提取 LLM | MEMORY——零模型调用 | MEMORY——零模型调用 | 提取决策和标注命名空间（mem0 的提取托管在平台上——该 lane 没有模型调用，桥接层根据平台回执发布协议事件；tencentdb 的提取在容器内运行，不记录） |
| **QUERY** | 召回查询改写器 | 召回查询改写器 | 召回查询改写器 | 仅改写调用（作为原始模型流量录制）；不携带 `memory_role_bind`，不发出 `memory_*` 事件 |

桥接层按以下优先级解析每个 lane 的 annotate 端点：

1. 显式 `annotate_main_url` / `annotate_memory_url` 配置
2. `MEMORY_ANNOTATE_MAIN_URL` / `MEMORY_ANNOTATE_MEMORY_URL` 环境变量覆盖
3. 从 lane 的有效模型 base URL 推导

不匹配其 lane 推导前缀的显式 URL 会禁用该 lane 的追踪，仅记录日志——永不抛出异常。

## 事件族

| 事件 | 发出时机 | 载荷 |
|---|---|---|
| `memory_session` | 每 episode 一次 | 任务文本作为内联 `ContentRef`（文本 + SHA-256 + 大小） |
| `memory_role_bind` | 每个逻辑角色一次 | 将 `main` 和 `memory` 绑定到代理标记的 lane |
| `memory_generate_start` | 每次提取 | 精确的规范化输入（内联引用） |
| `memory_change` | 每次存储变更 | `create` / `update` / `noop` / `delete`，对照快照审计 |
| `memory_generate_end` | 提取结束 | 产生的引用、检查点和变更审计 |
| `memory_search_start` | 每次原生搜索（由缓存命中的召回不发送搜索事件） | 精确的查询文本 |
| `memory_search_end` | 每次原生搜索结束（缓存命中的召回或未追踪的 episode 不发送任何事件） | 精确的有序**已渲染**引用（floor/slice/budget 之后），携带可移植的 `matched_count`（`{value, precision}`——floor/slice/budget 之前的原始匹配数，仅完成的搜索携带；原生搜索为无界全量扫描时（如 CURE）为 `exact`，top-k/limit 限界的原生搜索为 `lower_bound`），`matched`/`selected`/`rendered` 计数随适配器扩展携带 |
| `memory_delivery` | 每个放置的召回块 | 绑定到精确的 main-lane 调用，附带放置证明 |

{% hint style="warning" %}
**Delivery 规则：** 模型调用在到达 lane 前客户端失败的召回块**不**记录 delivery。可证明的 `placed` 声明必须绑定真实的调用区间，永远不是对空区间的 `no_call`。
{% endhint %}

## 记忆身份

每个 `memory_change` 和搜索结果携带一个可移植的 `native_stable` 身份，使在一个实例中创建的记忆可以通过产物与后续实例中的 delivery 连接。

| 集成 | 方案 | 格式 |
|---|---|---|
| CURE | `cure-sqlite-row-version-v1` | `store_id:semantic_digest` |
| mem0 | 平台记忆 id | 跨 UPDATE 版本稳定 |
| tencentdb | `tencentdb-memorycore-l1-v1` | 网关行 id + 内容摘要（persona 伪命中以 `persona` 作为单一演化项） |

tencentdb 臂中由智能体发起的 L0 会话搜索仅以计数器形式观测——它们永不产生被追踪的引用。

## 传输和批处理

根据录制器的容量限制批处理 post：

* 每请求最多 **256 个事件**
* 每 body 约 **1 MiB**
* 更大的 body 获得确定性 413
* 仅对连接失败和 5xx 重试
* 客户端错误（4xx/409）永不重试

## 降级语义

| 条件 | 效果 |
|---|---|
| 启动 post 413 | 该操作不被追踪 |
| 恢复 409 | 该 episode 的 memory lane 被禁用 |
| 操作中确定性拒绝 | 该操作不再发送事件；整个会话的 memory lane 追踪被禁用 |
| main-lane cursor 不可读 | 该 delivery 被跳过 |
| 未确认/被拒绝的 delivery | 该 episode 的 delivery 停止 |
| 重试超过熔断器限制 | 该 episode 的标注 I/O 停止 |

标注 I/O 时间从智能体的 wall-time 预算中排除，因此慢速录制器永远不会改变下一次模型调用是否执行。

## 下游消费者

| 工具 | 用途 |
|---|---|
| `utils/validate_run.py` | 单个录制轨迹目录的离线验证（任意记忆臂） |
| 共享测试套件 | 通过本地捕获服务器（`shared_bridge.testing.CaptureServer`）固定协议 |

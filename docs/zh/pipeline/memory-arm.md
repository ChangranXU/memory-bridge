---
description: 使用选定的集成运行记忆臂。
---

# 记忆臂

记忆臂以记忆开启（`scope=run`）的状态通过选定的集成运行预测，然后串联合并 → 评测 → 摘要。

## 快速运行

```bash
# 使用小切片设置 run root
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt first2

# 运行记忆臂（选择一个）
./utils/run-memory-arm.sh cure_memory
# 或：
./utils/run-memory-arm.sh mem0
# 或：
./utils/run-memory-arm.sh tencentdb
```

## 工作原理

驱动器为每个实例在一个 [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) roster 代理后运行，然后串联合并、评测和摘要。

{% tabs %}
{% tab title="cure_memory" %}
**代理 lane：** MAIN（基准模型）+ EXTRACT（提取 LLM）+ QUERY（改写器）

每个实例，驱动器：
1. 启动专用 roster 代理
2. 使用 CURE 集成运行 mini-swe-agent
3. 关闭代理（SIGINT）

**产物：**
* `runs/mini-swe-agent/cure_memory.sqlite3`——共享的 run 级别 SQLite 存储
* 每实例代理产物：`proxy.log`、轨迹录制
{% endtab %}

{% tab title="mem0" %}
**代理 lane：** MAIN（基准模型）+ MEMORY（零模型调用，标注命名空间）+ QUERY（改写器）

运行隔离来自从带时间戳的 run-root 名称生成的每次运行的用户 ID。

**要求：**
* `integration/mem0/.env` 中的 `MEM0_API_KEY`
{% endtab %}

{% tab title="tencentdb" %}
**代理 lane：** MAIN（基准模型）+ MEMORY（零模型调用，标注命名空间）+ QUERY（改写器）

驱动器管理每 run root 一个 MemoryCore 容器：生成不含凭据的网关配置（`<run-root>/tdai/tdai-gateway.yaml`，机密由 `docker run -e` 环境插值），在 `127.0.0.1:8420` 启动 `agentmemory/memory-core:1.0.1-beta.1`（数据卷在 `<run-root>/tdai/data`），等待 `/health`，退出时移除容器。提取在容器内直连提供商上游（不记录进轨迹）。

**要求：**
* Docker 已安装并运行
* roster `.env` 中可选的全有或全无 embedding 四元组：`EMBEDDING_MODEL`、`EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_DIMENSIONS`（驱动器拒绝部分设置——上游会将其静默禁用）

**产物：** 另有 `<run-root>/tdai/`——网关配置、容器数据卷、`episodes.jsonl`（跨 episode 溯源边车）。
{% endtab %}
{% endtabs %}

## 恢复行为

驱动器通过跳过已有有效补丁（`preds.json` 的 `model_patch` 非空）的实例来恢复运行。

{% hint style="danger" %}
当共享存储存在且列出的实例有过期尝试（`agent.log`）但没有有效补丁时，恢复会被**拒绝**。重新运行它会召回在中断尝试中批准的记忆，污染臂。请改用新的 run root。
{% endhint %}

## 核心产物

每个集成产生相同的每实例产物集：

| 产物 | 描述 |
|---|---|
| `preds.json` | 实例补丁（从模型轨迹中提取） |
| `memory.json` | Episode 日志：设置、计数器、事件、最终记忆 |
| `<id>/<id>.traj.json` | 带 `info.memory` 统计的轨迹 |
| `<id>/trajectory/` | 带 `memory_*` 标注事件的 traj-recorder 录制 |
| `agent.log` | 完整智能体记录 |

加上 run 级别产物：`runs/mini-swe-agent/merged-preds.json`、`local-eval/`、`memory-arm.log`。

## 记忆分析

```bash
# 聚合所有 episode 的记忆行为
./utils/summarize-memory.sh [RUN_ROOT]
```

产出每 episode 表格，显示：
* 存储变化量（添加/更新/删除；tencentdb 臂的删除列打印 "-"——去重删除不可观测）
* Agent 主动的场景读取（tencentdb 臂的 L2 读观测）
* 智能体主动发起的 L0 对话搜索（tencentdb 臂的 L0 读观测：`agent_conversation_searches` 次数与 `conversation_search_chars` 观测字符数，打印为 `agent l0 searches` / `l0 search chars` 列）
* 注入计数和字符预算
* 检索缓存命中率
* 改写结果
* 来自每命中来源列表的跨 episode 召回比例

## 环境变量

驱动器每次运行时从提供商 `.env` 重新生成录制器的 `.env`。全部三个集成均声明 `ROLE3="QUERY"` 用于召回查询改写器 lane。

| 变量 | CURE 臂 | mem0 臂 | tencentdb 臂 |
|---|---|---|---|
| `API_KEY` / `BASE_URL` / `MODEL` | 必需 | 必需 | 必需 |
| `EXTRACT_*` | 不使用（驱动器基于 EXTRACT 代理 lane 按实例管理） | 不使用 | 不使用（容器直连上游） |
| `MEM0_API_KEY` | 不使用 | 必需 | 不使用 |
| `EMBEDDING_*`（四项全设或全不设） | 不使用 | 不使用 | 可选（向量 lane；部分设置被拒绝） |
| `QUERY_*` | 可选（默认为 role-1） | 可选（默认为 role-1） | 可选（默认为 role-1） |
